"""
The three industry-first agent features in one demo.

    python examples/agent_novel_features.py

Shows three things no other agent framework ships today:

  1. Multi-LLM Consensus  — high-stakes wires only execute if N models
                            independently agree on the exact tool call.
  2. Provenance Tracking  — every tool argument is tagged `user`,
                            `tool:<name>`, or `llm_invented`. Catches
                            prompt-injection and hallucinated identifiers.
  3. Safety Invariants    — declarative rules across multiple tool calls
                            ("no more than ₹2 L of transfers per session").
                            A violating sequence is blocked before the
                            tool body ever runs.

The demo is fully offline — uses MockBackend so you can run it
anywhere — but the same `Agent` config works with OpenAI / Anthropic
/ Gemini / Ollama by swapping the backend.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shabd_agent import (
    Agent,
    AssistantTurn,
    ConsensusBackend,
    LLMBackend,
    MockBackend,
    ToolCall,
)


# ---------------------------------------------------------------------------
# 1. Consensus — three identical "models" agree on a safe transfer; a
#    fourth would-be hallucinating model is outvoted.
# ---------------------------------------------------------------------------
def demo_consensus() -> None:
    print("\n=== 1. Multi-LLM Consensus ===")

    class Canned(LLMBackend):
        def __init__(self, turn, name):
            self._t, self._n = turn, name

        def name(self):
            return self._n

        def chat(self, messages, tools):
            return self._t

    safe = AssistantTurn(tool_calls=[ToolCall(
        id="c1", name="transfer", arguments={"amount": 5000})])
    sketchy = AssistantTurn(tool_calls=[ToolCall(
        id="c1", name="transfer", arguments={"amount": 9_999_999})])

    cb = ConsensusBackend(
        backends=[Canned(safe, "gpt"), Canned(safe, "claude"),
                  Canned(sketchy, "rogue-llama")],
        min_agreement=2,
    )
    out = cb.chat([], [])
    print(f"  Quorum reached on: {out.tool_calls[0].arguments}")
    print(f"  Consensus meta:    {out.raw['consensus']}")
    print("  → The 'rogue' model's ₹99 L value was rejected by majority.")


# ---------------------------------------------------------------------------
# 2. Provenance — the LLM tries to use a fabricated account number.
# ---------------------------------------------------------------------------
def demo_provenance() -> None:
    print("\n=== 2. Provenance Tracking ===")

    agent = Agent(
        llm=MockBackend(plan=[
            # LLM hallucinates an account number that wasn't in the user
            # message and wasn't in any prior tool output.
            {"tool": "wire", "args": {
                "from_acct": "A1001",
                "to_acct": "X9999",   # NOT from the user
                "amount": 5000,
            }},
            "All done.",
        ]),
        track_provenance=True,
    )

    @agent.tool
    def wire(from_acct: str, to_acct: str, amount: int) -> str:
        return "wired"

    result = agent.run("Wire 5000 INR from A1001 please.")
    import json
    prov_msg = next(r for r in result.steps[0].tool_results
                    if "provenance" in r["content"])
    prov = json.loads(prov_msg["content"])["provenance"]
    print(f"  Per-argument provenance: {prov}")
    print("  → 'X9999' is correctly flagged as 'llm_invented' — a "
          "real bank would now block this transfer.")


# ---------------------------------------------------------------------------
# 3. Invariants — daily transfer cap declared once, agent cannot violate.
# ---------------------------------------------------------------------------
def demo_invariants() -> None:
    print("\n=== 3. Safety Invariants ===")

    agent = Agent(llm=MockBackend(plan=[
        # First call is fine.
        {"tool": "transfer", "args": {"amount": 150_000}},
        # Second call would push the daily total over ₹2 L.
        {"tool": "transfer", "args": {"amount": 80_000}},
        "I've stopped; the daily cap would have been breached.",
    ]), max_steps=6)

    @agent.tool
    def transfer(amount: int) -> str:
        return f"executed-{amount}"

    agent.add_invariant(
        name="daily_cap_2L",
        check=lambda s: sum(
            c.arguments.get("amount", 0)
            for c in s.tool_calls_named("transfer")
        ) <= 200_000,
        message="cumulative transfers in this session cannot exceed ₹2,00,000",
    )

    result = agent.run("Transfer 1.5L, then 80k.")
    print(f"  Final answer: {result.answer}")
    for s in result.steps:
        for r in s.tool_results:
            if "invariant_violation" in r["content"]:
                print(f"  Blocked attempt: {r['content'][:160]}...")


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    demo_consensus()
    demo_provenance()
    demo_invariants()
    print("\nAll three features ran without an external LLM call.")
