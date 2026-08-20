"""
Orchestrator demo — the "Main Orchestrator → intent → sub-agent" pattern
in one file, fully offline, with all the SHABD safety primitives wired
in.

This mirrors the shape every enterprise AI deployment ends up at —
PolicyBuddy / AiOps / HR Helpdesk / generic fallback — but does it in a
single zero-dep Python file you can drop into a restricted-network DC.

Run:
    python examples/orchestrator_demo.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shabd import SHABD
from shabd_agent import Agent, MockBackend
from shabd_orchestrator import (
    CostTracker,
    Orchestrator,
    RouteDecision,
    TokenPriceTable,
)

# 1) Shared audit + cost. Every intent's calls land in the same chain.
app = SHABD("acme-orchestrator", secret="x" * 32, require_auth=False,
            grimoire_log_path="/tmp/orchestrator-audit.jsonl")
prices = TokenPriceTable()
cost = CostTracker(budget_inr=10.0, prices=prices)   # ₹10 hard ceiling

orch = Orchestrator(
    audit_app=app,
    budget_inr=10.0,
    track_provenance=True,
)


# 2) Three intents — Policy / AiOps / Fallback. Each picks its own LLM
#    (here a deterministic MockBackend so the demo runs offline).

@orch.intent("policy",
              keywords=["leave", "policy", "holiday", "casual", "sick"],
              description="HR policy questions: leaves, attendance, benefits.")
def policy_builder(d: RouteDecision) -> Agent:
    return Agent(
        llm=MockBackend(plan=[
            {"tool": "lookup_leave_balance",
             "args": {"employee_id": "E1001"}},
            "You have 12 casual leaves and 5 sick leaves remaining.",
        ]),
        system=("You are PolicyBuddy. Answer HR policy questions only. "
                "Refuse anything else politely."),
    )


@orch.intent("aiops",
              keywords=["ticket", "incident", "outage", "vpn", "wifi",
                        "laptop", "printer"],
              description="IT helpdesk: raise incidents, check status.")
def aiops_builder(d: RouteDecision) -> Agent:
    return Agent(
        llm=MockBackend(plan=[
            {"tool": "create_incident",
             "args": {"summary": "printer outage floor 5",
                       "severity": "P3"}},
            "Incident INC-0042 created. ETA 2 hours.",
        ]),
        system="You are the AiOps agent connected to ServiceNow.",
    )


@orch.intent("fallback")
def fallback_builder(d: RouteDecision) -> Agent:
    return Agent(
        llm=MockBackend(plan=[
            "I can answer HR policy queries or raise IT tickets. "
            "Could you rephrase?",
        ]),
        system="You are a generic fallback assistant.",
    )


# Register the tools (here as agent-local stubs; in production they'd
# be SHABD spells with Aadhaar / Money / etc. semantic types).
for agent in (policy_builder(RouteDecision(intent="x", query="", confidence=1,
                                              cost_tracker=cost)),
              aiops_builder(RouteDecision(intent="x", query="", confidence=1,
                                            cost_tracker=cost))):
    @agent.tool
    def lookup_leave_balance(employee_id: str) -> dict:
        return {"casual": 12, "sick": 5, "earned": 21}

    @agent.tool
    def create_incident(summary: str, severity: str) -> dict:
        return {"ok": True, "ref": "INC-0042"}


# 3) Drive it.
queries = [
    "How many casual leaves do I have?",
    "Raise a ticket for the printer outage on floor 5",
    "What's the weather today?",
]

for q in queries:
    print("-" * 70)
    print(f"USER : {q}")
    res = orch.run(q, subject="amit.developer")
    print(f"       intent={res.intent}  "
          f"via={res.classifier_used}  "
          f"confidence={res.confidence:.2f}")
    print(f"BOT  : {res.answer}")
    print(f"       cost so far: ₹{res.cost['spent_inr']}")

print()
print("=" * 70)
print(f"Shared Grimoire audit  : {app.grimoire.verify()}")
print(f"Total cost in session  : ₹{cost.spent_inr:.4f}")
print(f"Remaining budget       : ₹{cost.remaining_inr:.4f}")
print()
print("All three intents landed in ONE audit chain, ONE budget,")
print("ONE provenance tracker — and the whole orchestrator is a single")
print("Python file with zero runtime dependencies.")
