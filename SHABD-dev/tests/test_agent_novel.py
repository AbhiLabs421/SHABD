"""
Tests for the three industry-first agent features (v2.6):

  1. Multi-LLM consensus  — ConsensusBackend
  2. Provenance tracking  — ProvenanceTracker, llm_invented detection
  3. Safety invariants    — declarative cross-tool rules

Run:
    python tests/test_agent_novel.py
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shabd_agent import (  # noqa: E402
    Agent,
    AssistantTurn,
    ConsensusBackend,
    ConsensusError,
    LLMBackend,
    MockBackend,
    ProvenanceTracker,
    ToolCall,
)


# ---------------------------------------------------------------------------
# Fake backend that always returns one canned turn
# ---------------------------------------------------------------------------
class _CannedBackend(LLMBackend):
    def __init__(self, turn: AssistantTurn, name: str = "canned"):
        self._turn = turn
        self._name = name

    def name(self) -> str:
        return self._name

    def chat(self, messages, tools):
        return self._turn


def _tool_turn(tool: str, args: dict) -> AssistantTurn:
    return AssistantTurn(text="", tool_calls=[ToolCall(
        id="c1", name=tool, arguments=args,
    )])


# ---------------------------------------------------------------------------
# 1) Consensus
# ---------------------------------------------------------------------------
class ConsensusTests(unittest.TestCase):
    def test_unanimous_tool_call_passes_through(self):
        same = _tool_turn("transfer", {"amount": 5000})
        cb = ConsensusBackend(
            [_CannedBackend(same, "a"), _CannedBackend(same, "b"),
             _CannedBackend(same, "c")],
            min_agreement=3,
        )
        out = cb.chat([], [])
        self.assertEqual(out.tool_calls[0].name, "transfer")
        self.assertEqual(out.raw["consensus"]["agreement"], 3)

    def test_majority_of_three_wins(self):
        same = _tool_turn("transfer", {"amount": 5000})
        diff = _tool_turn("transfer", {"amount": 9_999_999})  # outlier
        cb = ConsensusBackend(
            [_CannedBackend(same, "a"), _CannedBackend(same, "b"),
             _CannedBackend(diff, "c")],
            min_agreement=2,
        )
        out = cb.chat([], [])
        self.assertEqual(out.tool_calls[0].arguments["amount"], 5000)

    def test_no_quorum_raises_consensus_error(self):
        a = _tool_turn("transfer", {"amount": 5000})
        b = _tool_turn("transfer", {"amount": 9_999_999})
        cb = ConsensusBackend(
            [_CannedBackend(a, "a"), _CannedBackend(b, "b")],
            min_agreement=2,
        )
        with self.assertRaises(ConsensusError):
            cb.chat([], [])

    def test_text_only_turn_passes_without_consensus_by_default(self):
        a = AssistantTurn(text="The answer is 12.")
        b = AssistantTurn(text="The result is twelve.")
        cb = ConsensusBackend(
            [_CannedBackend(a, "a"), _CannedBackend(b, "b")],
            min_agreement=2,
        )
        # also_consensus_on_text=False (default) → first usable answer wins
        out = cb.chat([], [])
        self.assertTrue(out.text.startswith("The"))

    def test_consensus_failure_routed_back_to_llm_via_agent(self):
        """When consensus fails inside an agent loop, the agent feeds a
        structured error to the LLM so it can replan, instead of
        crashing."""
        # First call: disagree → ConsensusError.
        # Second call: model gives a final answer (after the error
        # message lands in its messages).
        from collections import deque
        plan = deque([
            "ConsensusError",            # marker handled below
            AssistantTurn(text="OK, I'll ask the user."),
        ])

        a = _tool_turn("delete_db", {})
        b = _tool_turn("write_report", {})

        class _Step(LLMBackend):
            def __init__(self, n):
                self.n = n
                self._inner = ConsensusBackend(
                    [_CannedBackend(a, "a"), _CannedBackend(b, "b")],
                    min_agreement=2,
                )

            def chat(self, messages, tools):
                step = plan.popleft()
                if isinstance(step, str) and step == "ConsensusError":
                    return self._inner.chat(messages, tools)
                return step

        agent = Agent(llm=_Step(0), max_steps=5)
        result = agent.run("do the thing")
        self.assertEqual(result.stopped_reason, "final")
        self.assertIn("ask the user", result.answer)


# ---------------------------------------------------------------------------
# 2) Provenance
# ---------------------------------------------------------------------------
class ProvenanceTests(unittest.TestCase):
    def test_user_value_is_recognised(self):
        p = ProvenanceTracker()
        p.absorb_user("Please transfer to account A1001 amount 5000 INR.")
        self.assertEqual(p.classify("A1001").tag, "user")
        self.assertEqual(p.classify(5000).tag, "user")

    def test_tool_output_value_is_tagged_to_that_tool(self):
        p = ProvenanceTracker()
        p.absorb_tool_output("lookup_customer",
                              {"aadhaar": "123456789012"}, step=0)
        self.assertEqual(p.classify("123456789012").tag,
                         "tool:lookup_customer")

    def test_invented_value_flagged(self):
        p = ProvenanceTracker()
        p.absorb_user("Hello world")
        self.assertEqual(p.classify("999999999999").tag, "llm_invented")
        self.assertEqual(p.classify(9_999_999).tag, "llm_invented")

    def test_provenance_appears_in_agent_step_trace(self):
        agent = Agent(
            llm=MockBackend(plan=[
                {"tool": "ship", "args": {"acct": "A1001",
                                            "fake_acct": "ZZZ-evil"}},
                "done",
            ]),
            track_provenance=True,
        )

        @agent.tool
        def ship(acct: str, fake_acct: str) -> str:
            return "shipped"

        result = agent.run("ship to A1001 please")
        prov_msg = next(r for r in result.steps[0].tool_results
                        if "provenance" in r["content"])
        import json as _j
        prov = _j.loads(prov_msg["content"])["provenance"]
        self.assertEqual(prov["acct"], "user")
        self.assertEqual(prov["fake_acct"], "llm_invented")


# ---------------------------------------------------------------------------
# 3) Invariants
# ---------------------------------------------------------------------------
class InvariantTests(unittest.TestCase):
    def test_invariant_blocks_tool_call(self):
        agent = Agent(llm=MockBackend(plan=[
            {"tool": "transfer", "args": {"amount": 1_000_000}},
            "rethought",
        ]))

        @agent.tool
        def transfer(amount: float) -> str:
            return "executed"

        agent.add_invariant(
            "no_transfer_above_1L",
            check=lambda s: all(
                c.arguments.get("amount", 0) <= 100_000
                for c in s.tool_calls_named("transfer")
            ),
            message="single transfer cannot exceed ₹1,00,000",
        )

        result = agent.run("transfer 10L please")
        # The tool body should NOT have run (we'd see "executed" in
        # the trace). Instead the error round-trips.
        last_tool = result.steps[0].tool_results[-1]
        self.assertIn("invariant_violation", last_tool["content"])
        self.assertIn("1,00,000", last_tool["content"])

    def test_decorator_form(self):
        agent = Agent(llm=MockBackend(plan=[
            {"tool": "f", "args": {}},
            "done",
        ]))

        @agent.tool
        def f() -> str:
            return "ran"

        called = {"n": 0}

        @agent.invariant("nothing")
        def _(session):
            called["n"] += 1
            return True

        agent.run("...")
        self.assertEqual(called["n"], 1)


# ---------------------------------------------------------------------------
# 4) The three features compose
# ---------------------------------------------------------------------------
class CompositionTests(unittest.TestCase):
    def test_consensus_provenance_invariant_in_one_agent(self):
        # Three "models" all agree.
        same = _tool_turn("transfer", {
            "amount_inr": 50_000,
            "to_acct": "B2002",
        })

        canned = [_CannedBackend(same, "g"),
                   _CannedBackend(same, "a"),
                   _CannedBackend(same, "l")]
        consensus = ConsensusBackend(canned, min_agreement=3)

        # Second turn: a final answer (also unanimous).
        final = AssistantTurn(text="Transfer completed.")
        final_backends = [_CannedBackend(final, b.name())
                          for b in canned]
        final_consensus = ConsensusBackend(final_backends,
                                            min_agreement=3)

        # Cycle two turns through the same chat callable.
        from collections import deque
        turns = deque([consensus, final_consensus])

        class _Cycle(LLMBackend):
            def name(self) -> str:
                return "cycle"

            def chat(self, messages, tools):
                return turns.popleft().chat(messages, tools)

        agent = Agent(llm=_Cycle(), track_provenance=True)

        @agent.tool
        def transfer(amount_inr: int, to_acct: str) -> dict:
            return {"ok": True, "ref": "T-1"}

        agent.add_invariant(
            "daily_cap_2L",
            check=lambda s: sum(
                c.arguments.get("amount_inr", 0)
                for c in s.tool_calls_named("transfer")
            ) <= 200_000,
        )

        result = agent.run(
            "Please transfer 50000 INR to account B2002."
        )
        self.assertEqual(result.answer, "Transfer completed.")
        # The tool ran -> we should see {"ok": true, "ref": "T-1"} in
        # the trace, alongside a provenance annotation tagging both
        # amount and account as user-origin.
        trace = "".join(r["content"]
                        for r in result.steps[0].tool_results)
        self.assertIn("user", trace)
        self.assertIn("\"ok\":", trace.replace(" ", ""))


def main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for cls in (ConsensusTests, ProvenanceTests, InvariantTests,
                CompositionTests):
        suite.addTests(loader.loadTestsFromTestCase(cls))
    runner = unittest.TextTestRunner(verbosity=2, stream=sys.stdout)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
