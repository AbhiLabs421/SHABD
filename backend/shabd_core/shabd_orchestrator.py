r"""
shabd_orchestrator.py — Single-file, zero-dependency multi-agent
orchestrator.

The "Main Orchestrator" pattern (intent → sub-agent → response) is the
default shape of every serious enterprise AI deployment today. Most
teams build it on LangGraph / LangChain / LlamaIndex, paying the
200-dep tax and the LangSmith subscription on the way.

This file does the same job in one Python file with no third-party
imports. It builds on the SHABD primitives:

  * shabd_agent.Agent          — the per-intent loop
  * shabd_agent.ConsensusBackend, ProvenanceTracker, Invariants
                                — the three industry-first features
  * shabd.Grimoire             — the shared cryptographic audit chain

Anatomy:

    +-----------------------------+
    |  Orchestrator               |
    |    Classifier   --(intent)--> sub-Agent #1 (PolicyBuddy)
    |                            \-> sub-Agent #2 (AiOps)
    |                            \-> sub-Agent #3 (...)
    |                            \-> Fallback agent
    |  Shared Grimoire (one audit chain across every intent)
    |  Shared cost tracker (one ₹ budget across every intent)
    |  Shared LLM fallback chain (GPT-4o -> QWEN3 if rate-limited)
    +-----------------------------+

Usage in three lines (matches the TCS / enterprise pattern):

    orch = Orchestrator(classifier=OpenAICompatBackend(...))
    orch.register_intent("policy", build_policy_agent, keywords=["leave", "policy"])
    orch.register_intent("aiops",  build_aiops_agent,  keywords=["ticket", "incident"])
    result = orch.run("How many casual leaves do I have?")

What makes this orchestrator different from LangGraph:

  1. Single file, zero runtime dependencies. Drop it into an
     air-gapped bank or a TCS-Ultimatix-style DMZ. No `pip install
     langgraph` or `npm i @langchain/core`.
  2. Shared Grimoire audit. Every classification, sub-agent call,
     and tool execution lands in one tamper-evident chain.
  3. LLMFallbackChain. Production LLM endpoints rate-limit. The
     orchestrator can hot-swap to the next model in the chain
     mid-conversation without rewriting the agent.
  4. CostTracker. Token cost in INR per intent, per session,
     per day. Hard-stop at budget rather than discover overspend
     in the next month's bill.
  5. The three SHABD-native features (consensus, provenance,
     invariants) carry through — declare them once at the
     Orchestrator and every sub-agent inherits them.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import typing as t
from dataclasses import dataclass, field

from shabd_agent import (
    Agent,
    AgentResult,
    Invariant,
    LLMBackend,
    ToolError,
)

log = logging.getLogger("shabd.orchestrator")

__all__ = [
    "Orchestrator",
    "IntentSpec",
    "RouteDecision",
    "LLMFallbackChain",
    "CostTracker",
    "TokenPriceTable",
    "IntentClassifier",
    "SemanticIntentClassifier",
    "EmbeddingsBackend",
    "OpenAICompatEmbeddings",
    "ENTERPRISE_SYNONYMS",
]


# ============================================================================
# Cost tracking
# ============================================================================

@dataclass
class TokenPriceTable:
    """Indicative ₹ cost per 1k tokens. Override per model in your
    procurement contract; the defaults are conservative.

    The orchestrator does not call any pricing API — these numbers are
    yours to keep up to date. The point is to *enforce a budget*, not
    to be a billing system."""

    inr_per_1k_input: dict[str, float] = field(default_factory=lambda: {
        "gpt-4o":         0.42,
        "gpt-4o-mini":    0.013,
        "gpt-oss:20b":    0.0,        # self-hosted
        "qwen3":          0.0,
        "claude-sonnet":  0.25,
        "default":        0.1,
    })
    inr_per_1k_output: dict[str, float] = field(default_factory=lambda: {
        "gpt-4o":         1.67,
        "gpt-4o-mini":    0.05,
        "gpt-oss:20b":    0.0,
        "qwen3":          0.0,
        "claude-sonnet":  1.25,
        "default":        0.4,
    })

    def cost_inr(self, model: str, in_tokens: int, out_tokens: int) -> float:
        in_rate = self.inr_per_1k_input.get(model,
                                             self.inr_per_1k_input["default"])
        out_rate = self.inr_per_1k_output.get(model,
                                                self.inr_per_1k_output["default"])
        return (in_tokens / 1000.0) * in_rate + (out_tokens / 1000.0) * out_rate


class CostTracker:
    """Per-session running cost. Crossing `budget_inr` triggers
    `BudgetExceeded` from the very next chat call."""

    def __init__(self, budget_inr: float = 0.0,
                 prices: TokenPriceTable | None = None):
        self.budget_inr = float(budget_inr)
        self.prices = prices or TokenPriceTable()
        self._spent: float = 0.0
        self._lock = threading.Lock()
        self._history: list = []

    def record(self, model: str, in_tokens: int, out_tokens: int) -> float:
        cost = self.prices.cost_inr(model, in_tokens, out_tokens)
        with self._lock:
            self._spent += cost
            self._history.append({
                "ts": time.time(), "model": model,
                "in": in_tokens, "out": out_tokens, "inr": cost,
            })
        return cost

    @property
    def spent_inr(self) -> float:
        return self._spent

    @property
    def remaining_inr(self) -> float:
        return max(0.0, self.budget_inr - self._spent) if self.budget_inr > 0 else float("inf")

    def assert_budget(self) -> None:
        if 0 < self.budget_inr < self._spent:
            raise BudgetExceeded(
                f"session budget ₹{self.budget_inr:.2f} exceeded "
                f"(spent ₹{self._spent:.2f})"
            )

    def snapshot(self) -> dict:
        return {
            "budget_inr": self.budget_inr,
            "spent_inr": round(self._spent, 4),
            "remaining_inr": round(self.remaining_inr, 4),
            "calls": len(self._history),
        }


class BudgetExceeded(ToolError):
    def __init__(self, message: str):
        super().__init__("budget_exceeded", message,
                          hint="Raise the orchestrator budget or pick a "
                               "cheaper model.")


# ============================================================================
# LLM fallback chain — pull the rip-cord when the primary rate-limits
# ============================================================================

class LLMFallbackChain(LLMBackend):
    """Wraps several backends and tries them in order. The next one is
    used as soon as the current raises (rate limit, network blip,
    HTTP 5xx).

    Combine with CostTracker for the classic enterprise pattern:
    'try GPT-4o first; fall back to the self-hosted QWEN3 if OpenAI
    refuses or we're out of budget'."""

    def __init__(self, backends: t.Sequence[LLMBackend], *,
                 cost: CostTracker | None = None,
                 budget_check: bool = True):
        if not backends:
            raise ValueError("FallbackChain needs at least one backend")
        self.backends = list(backends)
        self.cost = cost
        self.budget_check = budget_check

    def name(self) -> str:
        return "fallback(" + ">".join(b.name() for b in self.backends) + ")"

    def chat(self, messages, tools):
        last_exc: Exception | None = None
        for b in self.backends:
            if self.budget_check and self.cost:
                self.cost.assert_budget()
            try:
                out = b.chat(messages, tools)
                # Best-effort token accounting from OpenAI-shaped responses
                # (usage dict). Self-hosted backends without `usage` cost
                # zero in the default table anyway, so no surprises.
                if self.cost and isinstance(out.raw, dict):
                    usage = (out.raw.get("usage")
                              or (out.raw.get("response_metadata") or {})
                                    .get("usage")
                              or {})
                    in_tok = int(usage.get("prompt_tokens", 0))
                    out_tok = int(usage.get("completion_tokens", 0))
                    model_name = b.name().split("(")[-1].rstrip(")")
                    self.cost.record(model_name, in_tok, out_tok)
                return out
            except Exception as e:  # noqa: BLE001
                last_exc = e
                log.warning("backend %s failed (%s); trying next",
                            b.name(), e)
        raise RuntimeError(
            f"every fallback backend failed; last error: {last_exc}")


# ============================================================================
# Intent specification and classifier
# ============================================================================

@dataclass
class IntentSpec:
    name: str
    builder: t.Callable[[RouteDecision], Agent]
    keywords: list[str] = field(default_factory=list)
    description: str = ""


@dataclass
class RouteDecision:
    """What the orchestrator decided about a query, handed to the
    sub-agent builder."""
    intent: str
    query: str
    confidence: float
    cost_tracker: CostTracker
    classifier_used: str = ""
    metadata: dict = field(default_factory=dict)


class IntentClassifier:
    """Two-stage classifier that's *robust by design*:

      1. Try every intent's keyword list (cheap, deterministic).
      2. If nothing matches strongly, fall back to the LLM with the
         intent descriptions in-prompt.

    This survives LLM outages — if the LLM endpoint is down, the
    keyword stage still routes the request to a reasonable place.
    Critical for restricted-network deployments."""

    def __init__(self, llm: LLMBackend | None = None,
                 keyword_confidence: float = 0.6,
                 fallback_intent: str = "fallback"):
        self.llm = llm
        self.keyword_confidence = keyword_confidence
        self.fallback_intent = fallback_intent

    def classify(self, query: str,
                 intents: t.Sequence[IntentSpec]) -> tuple[str, float, str]:
        # Stage 1: keyword.
        q = query.lower()
        scores: dict[str, int] = {}
        for spec in intents:
            score = sum(1 for kw in spec.keywords
                         if kw.lower() in q)
            if score:
                scores[spec.name] = score
        if scores:
            top = max(scores, key=lambda k: scores[k])
            return top, min(0.5 + 0.1 * scores[top], 1.0), "keyword"

        # Stage 2: LLM.
        if self.llm is not None:
            try:
                catalogue = "\n".join(
                    f"- {s.name}: {s.description or 'no description'}"
                    for s in intents
                )
                prompt = (
                    "Classify the user query into exactly one intent name "
                    "from this list. Reply with JSON of the form "
                    "{\"intent\": \"<name>\", \"confidence\": <0..1>}.\n\n"
                    f"Intents:\n{catalogue}\n\nQuery: {query}"
                )
                turn = self.llm.chat(
                    [{"role": "system",
                      "content": "You are an intent classifier."},
                     {"role": "user", "content": prompt}],
                    [],
                )
                data = _parse_json_block(turn.text)
                intent_name = str(data.get("intent", "")).strip()
                conf = float(data.get("confidence", 0.7))
                names = [s.name for s in intents]
                if intent_name in names:
                    return intent_name, conf, "llm"
            except Exception:
                log.exception("LLM classifier failed")
        return self.fallback_intent, 0.1, "fallback"


def _parse_json_block(text: str) -> dict:
    """Pull the first JSON object out of a chunk of text — LLMs often
    return one inside narrative."""
    if not text:
        return {}
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return {}
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return {}


# ============================================================================
# Orchestrator
# ============================================================================

@dataclass
class OrchestrationResult:
    intent: str
    confidence: float
    classifier_used: str
    answer: str
    elapsed_s: float
    cost: dict
    agent_result: AgentResult | None = None
    audit_head: str = ""


class Orchestrator:
    """The Main Orchestrator. Anyone, anywhere, any LLM.

    Constructor args:
      * `classifier`     — an `IntentClassifier`, or a `LLMBackend`
                           which we'll wrap in one for you.
      * `audit_app`      — a SHABD `app` whose Grimoire chain receives
                           every classification and sub-agent step.
                           Pass `None` to skip audit.
      * `budget_inr`     — hard ₹ ceiling per session.
      * `prices`         — `TokenPriceTable` override.
      * `invariants`     — `[Invariant(...)]` applied to every sub-agent.
      * `track_provenance` — propagated to every sub-agent.

    Register intents with `register_intent(name, builder, keywords=...)`.
    The `builder` is a function `(RouteDecision) -> Agent` so each
    intent can pick its own LLM, system prompt and tools.
    """

    def __init__(self, *,
                 classifier: t.Union[IntentClassifier, LLMBackend, None] = None,
                 audit_app: t.Any = None,
                 budget_inr: float = 0.0,
                 prices: TokenPriceTable | None = None,
                 invariants: list[Invariant] | None = None,
                 track_provenance: bool = False,
                 fallback_intent: str = "fallback"):
        if classifier is None:
            self.classifier = IntentClassifier()
        elif isinstance(classifier, IntentClassifier):
            self.classifier = classifier
        else:
            self.classifier = IntentClassifier(llm=classifier)
        self._intents: dict[str, IntentSpec] = {}
        self.audit_app = audit_app
        self.cost = CostTracker(budget_inr=budget_inr, prices=prices)
        self.invariants = list(invariants or [])
        self.track_provenance = track_provenance
        self.fallback_intent = fallback_intent

    # ---- registration ----

    def register_intent(self, name: str,
                        builder: t.Callable[[RouteDecision], Agent],
                        *, keywords: t.Iterable[str] = (),
                        description: str = "") -> None:
        self._intents[name] = IntentSpec(
            name=name, builder=builder,
            keywords=list(keywords), description=description,
        )

    def intent(self, name: str, *, keywords: t.Iterable[str] = (),
               description: str = ""):
        """Decorator form."""
        def deco(fn: t.Callable[[RouteDecision], Agent]):
            self.register_intent(name, fn, keywords=keywords,
                                  description=description)
            return fn
        return deco

    # ---- audit helper ----

    def _audit(self, kind: str, payload: dict) -> str:
        if self.audit_app is None:
            return ""
        try:
            page = self.audit_app.grimoire.append(
                trace_id=payload.get("trace_id", ""),
                spell=f"orchestrator:{kind}",
                subject=payload.get("subject", "anonymous"),
                args=payload, result={"ok": True}, ok=True,
            )
            return page["hash"]
        except Exception:
            log.exception("orchestrator audit append failed")
            return ""

    # ---- run ----

    def run(self, query: str, *,
            subject: str = "anonymous",
            extra_context: dict | None = None,
            trace_id: str | None = None) -> OrchestrationResult:
        if not self._intents:
            raise ValueError("Register at least one intent with "
                             "register_intent(...) before calling run().")
        trace_id = trace_id or _new_trace_id()
        started = time.time()

        # 1) Classify
        intent_name, conf, classifier_used = self.classifier.classify(
            query, list(self._intents.values())
        )
        if intent_name not in self._intents:
            intent_name = self.fallback_intent
        if intent_name not in self._intents:
            # No fallback registered either — surface a clean error
            return OrchestrationResult(
                intent="", confidence=0.0, classifier_used=classifier_used,
                answer="Sorry — no agent is registered for this kind of query.",
                elapsed_s=time.time() - started,
                cost=self.cost.snapshot(),
            )

        head_after_classify = self._audit("classified", {
            "trace_id": trace_id, "subject": subject,
            "query": query, "intent": intent_name,
            "confidence": conf, "classifier": classifier_used,
        })

        # 2) Build the sub-agent for this intent
        decision = RouteDecision(
            intent=intent_name, query=query, confidence=conf,
            cost_tracker=self.cost, classifier_used=classifier_used,
            metadata=extra_context or {},
        )
        agent = self._intents[intent_name].builder(decision)

        # 3) Carry orchestrator-level invariants and provenance into the
        #    sub-agent. The user could have set these on the sub-agent
        #    too — we don't overwrite, we extend.
        for inv in self.invariants:
            try:
                agent.add_invariant(inv.name, inv.check, inv.message)
            except Exception:
                pass
        if self.track_provenance and not getattr(agent, "_provenance", None):
            from shabd_agent import ProvenanceTracker
            agent._provenance = ProvenanceTracker()
            if agent.system:
                agent._provenance.absorb_system(agent.system)

        # 4) Run the sub-agent
        try:
            agent_result = agent.run(query)
        except BudgetExceeded as be:
            return OrchestrationResult(
                intent=intent_name, confidence=conf,
                classifier_used=classifier_used,
                answer=f"Budget exceeded: {be}",
                elapsed_s=time.time() - started,
                cost=self.cost.snapshot(),
                audit_head=head_after_classify,
            )

        head_after_agent = self._audit("answered", {
            "trace_id": trace_id, "subject": subject,
            "intent": intent_name,
            "stopped": agent_result.stopped_reason,
            "answer_len": len(agent_result.answer),
            "steps": len(agent_result.steps),
        }) or head_after_classify

        return OrchestrationResult(
            intent=intent_name, confidence=conf,
            classifier_used=classifier_used,
            answer=agent_result.answer,
            elapsed_s=time.time() - started,
            cost=self.cost.snapshot(),
            agent_result=agent_result,
            audit_head=head_after_agent,
        )


def _new_trace_id() -> str:
    import secrets
    return secrets.token_hex(16)


# ============================================================================
# Semantic intent classifier — 5-stage fallback
# ============================================================================
#
# Why this exists: a naive substring match misses everything a real user
# actually types. "I want to take leave" matches, but "chuti chahiye",
# "vacation request", "PTO", "off day" — none of them do. Real enterprise
# users say all of these.
#
# The semantic classifier escalates through cheaper-to-costlier stages:
#
#   Stage 1: exact substring against the spec keywords            (free)
#   Stage 2: substring against an expanded synonym list           (free)
#   Stage 3: character n-gram cosine over keywords + description  (free, stdlib)
#   Stage 4: embedding cosine — opt-in, uses any /v1/embeddings   (one HTTP call)
#   Stage 5: LLM classifier with the catalogue in-prompt          (one LLM call)
#
# The first three are pure standard library, so a deployment with no
# embeddings endpoint or LLM still gets useful semantic routing.


# A starter dictionary that catches what Indian enterprise users
# actually type — Hindi-Latin transliterations, common acronyms,
# tool-vendor language. Extend per-domain in production via the
# `synonyms=` constructor arg.
ENTERPRISE_SYNONYMS: dict[str, list[str]] = {
    # HR / policy
    "leave":       ["vacation", "holiday", "pto", "off", "absent",
                    "chuti", "leaves", "leaving", "wfh", "wfh-leave"],
    "sick":        ["medical", "ill", "unwell", "bimaar", "sickness"],
    "policy":      ["rules", "guideline", "guidelines", "niti", "niyam",
                    "regulation", "policies"],
    "salary":      ["pay", "compensation", "vetan", "remuneration",
                    "payslip", "ctc", "package"],
    "appraisal":   ["review", "rating", "promotion", "increment", "hike"],
    "timesheet":   ["timesheets", "ts", "weekly-ts", "logged hours"],
    # IT helpdesk / AiOps
    "ticket":      ["incident", "issue", "case", "complaint", "samasya",
                    "request", "raise", "raising"],
    "outage":      ["down", "broken", "failing", "not working", "khrab",
                    "kharab", "fail", "failure"],
    "vpn":         ["network", "remote access", "connection", "tunnel"],
    "wifi":        ["wireless", "internet", "connection", "wifi-network"],
    "laptop":      ["computer", "system", "machine", "pc", "device"],
    "printer":     ["xerox", "print", "printout", "scanner"],
    "password":    ["pwd", "credential", "credentials", "login", "passcode"],
    "email":       ["mail", "outlook", "inbox"],
    "access":      ["permission", "permissions", "right", "rights", "auth"],
    # Generic enterprise verbs
    "meeting":     ["call", "sync", "discussion", "huddle", "1on1"],
    "approve":     ["approval", "sign-off", "ok", "permit"],
    "deploy":      ["deployment", "release", "rollout", "ship"],
    "cancel":      ["abort", "cancellation", "stop", "void"],
}


def _ngrams(text: str, n: int) -> dict:
    """Multiset of character n-grams. Returns {ngram: count}."""
    text = text.lower()
    if len(text) < n:
        return {text: 1} if text else {}
    out: dict[str, int] = {}
    for i in range(len(text) - n + 1):
        g = text[i:i + n]
        out[g] = out.get(g, 0) + 1
    return out


def _ngram_cosine(a: str, b: str, n: int = 3) -> float:
    """Cosine similarity over character n-grams. 0..1.

    Robust to plurals (`leave` vs `leaves`), typos (`tikket` vs `ticket`),
    and word boundaries (`vpn-issue` vs `vpn issue`)."""
    ga, gb = _ngrams(a, n), _ngrams(b, n)
    if not ga or not gb:
        return 0.0
    common = sum(min(ga[k], gb[k]) for k in ga.keys() & gb.keys())
    norm_a = sum(v * v for v in ga.values()) ** 0.5
    norm_b = sum(v * v for v in gb.values()) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return common / (norm_a * norm_b)


def _vec_cosine(a: t.Sequence[float], b: t.Sequence[float]) -> float:
    if not a or not b:
        return 0.0
    s = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    return s / (na * nb) if (na and nb) else 0.0


class EmbeddingsBackend:
    """Pluggable embeddings provider. Implement `embed(texts) -> [[float]]`."""

    def embed(self, texts: t.Sequence[str]) -> list[list[float]]:
        raise NotImplementedError

    def name(self) -> str:
        return type(self).__name__


class OpenAICompatEmbeddings(EmbeddingsBackend):
    """Calls `/v1/embeddings` — works with OpenAI, Ollama, vLLM, LM Studio,
    anything that follows OpenAI's embeddings shape."""

    def __init__(self, *, base_url: str, model: str,
                 api_key: str = "", timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout

    def name(self) -> str:
        return f"openai-embeddings({self.model})"

    def embed(self, texts: t.Sequence[str]) -> list[list[float]]:
        import urllib.request as _ur
        body = json.dumps({"model": self.model, "input": list(texts)}).encode()
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = _ur.Request(f"{self.base_url}/embeddings", data=body,
                          method="POST", headers=headers)
        with _ur.urlopen(req, timeout=self.timeout) as r:
            resp = json.loads(r.read())
        return [d["embedding"] for d in resp.get("data", [])]


class SemanticIntentClassifier(IntentClassifier):
    """Five-stage classifier. Each stage is free or one HTTP call.

        clf = SemanticIntentClassifier(
            llm=OpenAICompatBackend(...),                       # stage 5
            embeddings=OpenAICompatEmbeddings(...),             # stage 4
            synonyms={"leave": ["off-day"]},                    # stage 2 extra
            ngram_size=3,
            ngram_threshold=0.22,
            embedding_threshold=0.55,
        )
        orch = Orchestrator(classifier=clf)
    """

    def __init__(self, *,
                 llm: LLMBackend | None = None,
                 embeddings: EmbeddingsBackend | None = None,
                 synonyms: dict[str, list[str]] | None = None,
                 ngram_size: int = 3,
                 ngram_threshold: float = 0.22,
                 embedding_threshold: float = 0.55,
                 fallback_intent: str = "fallback"):
        super().__init__(llm=llm, fallback_intent=fallback_intent)
        self.embeddings = embeddings
        # Merge user-supplied synonyms on top of the bundled ones.
        merged = {k: list(v) for k, v in ENTERPRISE_SYNONYMS.items()}
        if synonyms:
            for k, v in synonyms.items():
                merged.setdefault(k.lower(), []).extend(v)
        self.synonyms = merged
        self.ngram_size = ngram_size
        self.ngram_threshold = ngram_threshold
        self.embedding_threshold = embedding_threshold
        self._embedding_cache: dict = {}

    # ---- helpers ----

    def _expanded(self, spec) -> set[str]:
        out = {kw.lower() for kw in spec.keywords}
        for kw in list(out):
            for syn in self.synonyms.get(kw, []):
                out.add(syn.lower())
        return out

    def _intent_text(self, spec) -> str:
        return " ".join([spec.description or "",
                          *sorted(self._expanded(spec))]).strip()

    def _intent_vectors(self, intents) -> list[list[float]]:
        if self.embeddings is None:
            return []
        key = tuple((s.name, self._intent_text(s)) for s in intents)
        cached = self._embedding_cache.get(key)
        if cached is not None:
            return cached
        try:
            vecs = self.embeddings.embed([self._intent_text(s)
                                           for s in intents])
        except Exception:
            log.exception("intent embedding failed")
            return []
        self._embedding_cache[key] = vecs
        return vecs

    # ---- main API ----

    def classify(self, query: str,
                 intents: t.Sequence[IntentSpec]) -> tuple[str, float, str]:
        q = (query or "").lower()
        if not q or not intents:
            return self.fallback_intent, 0.0, "fallback"

        # Stage 1+2: word-boundary match against the expanded keyword set.
        # We deliberately don't use plain `in` here — "laptop" would match
        # synonym "pto" via substring, which silently routes the query to
        # the wrong intent. Word boundaries kill that whole class of bug.
        import re as _re
        sub_scores: dict[str, int] = {}
        for spec in intents:
            kws = self._expanded(spec)
            hits = 0
            for kw in kws:
                if not kw:
                    continue
                # Allow internal hyphens / spaces in multi-word keywords.
                pattern = r"(?:^|\W)" + _re.escape(kw) + r"(?:\W|$)"
                if _re.search(pattern, q):
                    hits += 1
            if hits:
                sub_scores[spec.name] = hits
        if sub_scores:
            top = max(sub_scores, key=lambda k: sub_scores[k])
            return top, min(0.55 + 0.1 * sub_scores[top], 0.99), "synonym"

        # Stage 3: character n-gram cosine.
        ng_scores: dict[str, float] = {}
        for spec in intents:
            sim = _ngram_cosine(q, self._intent_text(spec), self.ngram_size)
            if sim >= self.ngram_threshold:
                ng_scores[spec.name] = sim
        if ng_scores:
            top = max(ng_scores, key=lambda k: ng_scores[k])
            return top, ng_scores[top], "ngram"

        # Stage 4: embeddings cosine, if a backend was provided.
        if self.embeddings is not None:
            intent_vecs = self._intent_vectors(intents)
            if intent_vecs:
                try:
                    q_vec = self.embeddings.embed([query])[0]
                except Exception:
                    log.exception("query embedding failed")
                    q_vec = []
                if q_vec:
                    emb_scores = {
                        spec.name: _vec_cosine(q_vec, v)
                        for spec, v in zip(intents, intent_vecs)
                    }
                    top = max(emb_scores, key=lambda k: emb_scores[k])
                    if emb_scores[top] >= self.embedding_threshold:
                        return top, emb_scores[top], "embedding"

        # Stage 5: LLM fallback (inherited).
        return super().classify(query, intents)
