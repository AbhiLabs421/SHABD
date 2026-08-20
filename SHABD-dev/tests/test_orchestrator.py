"""
Tests for shabd_orchestrator — single-file multi-agent orchestrator.

Run:
    python tests/test_orchestrator.py
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shabd import SHABD  # noqa: E402
from shabd_agent import Agent, AssistantTurn, LLMBackend, MockBackend  # noqa: E402
from shabd_orchestrator import (  # noqa: E402
    CostTracker,
    EmbeddingsBackend,
    IntentClassifier,
    IntentSpec,
    LLMFallbackChain,
    Orchestrator,
    RouteDecision,
    SemanticIntentClassifier,
)


# Tiny fixed-response LLM for classifier tests.
class _FixedClassifierLLM(LLMBackend):
    def __init__(self, intent: str, conf: float = 0.9):
        self._intent = intent
        self._conf = conf

    def name(self) -> str:
        return "fixed-classifier"

    def chat(self, messages, tools):
        return AssistantTurn(
            text=f'{{"intent": "{self._intent}", "confidence": {self._conf}}}'
        )


def _build_agent(answer: str) -> Agent:
    return Agent(llm=MockBackend(plan=[answer]))


class IntentClassifierTests(unittest.TestCase):
    def test_keyword_classifier(self):
        c = IntentClassifier()
        from shabd_orchestrator import IntentSpec
        intents = [
            IntentSpec("policy", lambda d: None, keywords=["leave", "holiday"]),
            IntentSpec("aiops",  lambda d: None, keywords=["ticket", "outage"]),
            IntentSpec("fallback", lambda d: None),
        ]
        name, conf, via = c.classify("How many casual leaves are left?", intents)
        self.assertEqual(name, "policy")
        self.assertEqual(via, "keyword")
        self.assertGreater(conf, 0.5)

    def test_llm_fallback_when_no_keywords_match(self):
        c = IntentClassifier(llm=_FixedClassifierLLM("aiops", 0.95))
        from shabd_orchestrator import IntentSpec
        intents = [
            IntentSpec("policy", lambda d: None, keywords=["leave"],
                        description="HR queries"),
            IntentSpec("aiops",  lambda d: None, keywords=["ticket"],
                        description="IT support"),
            IntentSpec("fallback", lambda d: None),
        ]
        name, conf, via = c.classify("the printer is broken", intents)
        self.assertEqual(name, "aiops")
        self.assertEqual(via, "llm")

    def test_pure_fallback_when_classifier_unavailable(self):
        c = IntentClassifier()    # no LLM
        from shabd_orchestrator import IntentSpec
        intents = [IntentSpec("policy", lambda d: None, keywords=["leave"]),
                   IntentSpec("fallback", lambda d: None)]
        name, _, via = c.classify("how is the weather?", intents)
        self.assertEqual(name, "fallback")
        self.assertEqual(via, "fallback")


class CostTrackerTests(unittest.TestCase):
    def test_budget_exceeded_raises(self):
        ct = CostTracker(budget_inr=0.10)
        ct.record("gpt-4o", in_tokens=10_000, out_tokens=2_000)
        self.assertGreater(ct.spent_inr, 0.10)
        with self.assertRaises(Exception):
            ct.assert_budget()

    def test_self_hosted_costs_zero_by_default(self):
        ct = CostTracker(budget_inr=1.0)
        ct.record("qwen3", in_tokens=100_000, out_tokens=100_000)
        self.assertEqual(ct.spent_inr, 0.0)


class FallbackChainTests(unittest.TestCase):
    def test_falls_through_on_error(self):
        class Boom(LLMBackend):
            def name(self): return "boom"
            def chat(self, m, t): raise RuntimeError("rate-limited")

        class Good(LLMBackend):
            def name(self): return "good"
            def chat(self, m, t):
                return AssistantTurn(text="ok")

        chain = LLMFallbackChain([Boom(), Good()])
        out = chain.chat([], [])
        self.assertEqual(out.text, "ok")

    def test_all_failing_raises(self):
        class Boom(LLMBackend):
            def name(self): return "boom"
            def chat(self, m, t): raise RuntimeError("nope")

        chain = LLMFallbackChain([Boom(), Boom()])
        with self.assertRaises(RuntimeError):
            chain.chat([], [])


class OrchestratorRoutingTests(unittest.TestCase):
    def test_keyword_routing(self):
        orch = Orchestrator()
        orch.register_intent(
            "policy",
            lambda d: _build_agent("You have 12 leaves."),
            keywords=["leave", "policy"],
        )
        orch.register_intent(
            "aiops",
            lambda d: _build_agent("Ticket created."),
            keywords=["ticket", "outage"],
        )
        orch.register_intent("fallback",
                              lambda d: _build_agent("Sorry, no answer."))

        r = orch.run("How many casual leaves remain?")
        self.assertEqual(r.intent, "policy")
        self.assertIn("12 leaves", r.answer)

        r2 = orch.run("Raise a ticket for printer outage")
        self.assertEqual(r2.intent, "aiops")

    def test_falls_back_when_no_match(self):
        orch = Orchestrator()
        orch.register_intent(
            "policy", lambda d: _build_agent("HR answer"),
            keywords=["leave"],
        )
        orch.register_intent(
            "fallback", lambda d: _build_agent("default"),
        )
        r = orch.run("the weather is nice today")
        self.assertEqual(r.intent, "fallback")

    def test_audit_chain_populated(self):
        app = SHABD("orch-audit", secret="x" * 32, require_auth=False)
        orch = Orchestrator(audit_app=app)
        orch.register_intent("policy",
                              lambda d: _build_agent("policy-answer"),
                              keywords=["policy"])
        orch.register_intent("fallback",
                              lambda d: _build_agent("default"))
        r = orch.run("Tell me the leave policy.")
        self.assertEqual(r.intent, "policy")
        self.assertTrue(app.grimoire.verify()["ok"])
        spells = {p["spell"] for p in app.grimoire.pages()}
        self.assertIn("orchestrator:classified", spells)
        self.assertIn("orchestrator:answered", spells)
        self.assertNotEqual(r.audit_head, "")

    def test_decorator_form(self):
        orch = Orchestrator()
        marker = {"intent_run": False}

        @orch.intent("greet", keywords=["hello", "hi"])
        def _(d: RouteDecision):
            marker["intent_run"] = True
            return _build_agent("Hi!")

        orch.register_intent("fallback", lambda d: _build_agent("default"))
        r = orch.run("hello there")
        self.assertEqual(r.intent, "greet")
        self.assertTrue(marker["intent_run"])


class SemanticClassifierTests(unittest.TestCase):
    """The five stages of `SemanticIntentClassifier` in isolation."""

    def setUp(self):
        self.intents = [
            IntentSpec("policy", lambda d: None,
                        keywords=["leave", "policy"],
                        description="HR queries about leaves and policies"),
            IntentSpec("aiops", lambda d: None,
                        keywords=["ticket", "printer", "vpn", "wifi", "laptop"],
                        description="IT helpdesk: tickets, outages, devices"),
            IntentSpec("fallback", lambda d: None),
        ]

    def test_exact_keyword_still_works(self):
        clf = SemanticIntentClassifier()
        name, _, via = clf.classify("how many leaves do I have", self.intents)
        self.assertEqual(name, "policy")
        self.assertEqual(via, "synonym")

    def test_english_synonym(self):
        clf = SemanticIntentClassifier()
        name, _, via = clf.classify("vacation balance", self.intents)
        self.assertEqual(name, "policy")
        self.assertEqual(via, "synonym")

    def test_hindi_transliteration(self):
        clf = SemanticIntentClassifier()
        name, _, via = clf.classify("chuti chahiye", self.intents)
        self.assertEqual(name, "policy")

    def test_acronym(self):
        clf = SemanticIntentClassifier()
        name, _, _ = clf.classify("PTO balance batao", self.intents)
        self.assertEqual(name, "policy")

    def test_word_boundary_prevents_substring_false_match(self):
        """`laptop` must NOT match the synonym `pto` (substring of laptop)."""
        clf = SemanticIntentClassifier()
        name, _, via = clf.classify("samasya hai laptop me", self.intents)
        self.assertEqual(name, "aiops")    # not 'policy' via spurious 'pto'

    def test_typo_via_ngram(self):
        clf = SemanticIntentClassifier()
        # `tikket` shouldn't match anything exactly but should land on aiops
        # either via the keyword `raise` (also in our query) or via the
        # n-gram stage.
        name, _, _ = clf.classify("tikket kaise raise kru", self.intents)
        self.assertEqual(name, "aiops")

    def test_user_synonyms_extend_dictionary(self):
        clf = SemanticIntentClassifier(synonyms={
            "leave": ["off-day", "extra-pto"],
        })
        name, _, _ = clf.classify("can I take an extra-pto", self.intents)
        self.assertEqual(name, "policy")

    def test_unrelated_falls_back(self):
        clf = SemanticIntentClassifier()
        name, _, via = clf.classify("what is the weather", self.intents)
        self.assertEqual(name, "fallback")
        self.assertEqual(via, "fallback")

    def test_embeddings_stage(self):
        """If keyword + synonym + n-gram all miss, embeddings can route."""

        # A fake EmbeddingsBackend that gives the query a vector identical
        # to the aiops intent vector — so the aiops cosine = 1.0 and wins.
        class FixedEmbeddings(EmbeddingsBackend):
            def embed(self, texts):
                out = []
                for t in texts:
                    # The query is in the first position when called once;
                    # we route every text through the same logic for
                    # determinism.
                    if "leaves" in t or "policies" in t or "leave" in t:
                        out.append([0.0, 1.0, 0.0])     # policy direction
                    elif ("helpdesk" in t or "ticket" in t
                          or "printer" in t or "device" in t
                          or "outages" in t):
                        out.append([1.0, 0.0, 0.0])     # aiops direction
                    elif "fallback" in t.lower():
                        out.append([0.0, 0.0, 1.0])
                    else:
                        out.append([1.0, 0.0, 0.0])     # query → aiops side
                return out

        clf = SemanticIntentClassifier(embeddings=FixedEmbeddings(),
                                        embedding_threshold=0.5)
        # A query that has no overlap with any keyword or synonym, but
        # whose embedding lines up with aiops.
        name, conf, via = clf.classify("escalate the matter immediately",
                                        self.intents)
        # Either embeddings or ngram may catch it; both are acceptable
        # routes — the contract is "not fallback".
        self.assertNotEqual(name, "fallback")
        self.assertIn(via, {"embedding", "ngram"})


def main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for cls in (IntentClassifierTests, CostTrackerTests, FallbackChainTests,
                OrchestratorRoutingTests, SemanticClassifierTests):
        suite.addTests(loader.loadTestsFromTestCase(cls))
    runner = unittest.TextTestRunner(verbosity=2, stream=sys.stdout)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
