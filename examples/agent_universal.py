"""
Universal agent — same code, any LLM provider, zero external SDKs.

Run any of these:

    python examples/agent_universal.py                       # offline (MockBackend)
    OPENAI_API_KEY=sk-... python examples/agent_universal.py --openai
    ANTHROPIC_API_KEY=... python examples/agent_universal.py --anthropic
    GEMINI_API_KEY=... python examples/agent_universal.py --gemini
    OLLAMA_MODEL=qwen2.5:1.5b python examples/agent_universal.py --ollama

The tools, the loop, the audit chain, the error-recovery behaviour
are all identical across providers. Only the `llm=...` line changes.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shabd import SHABD, Aadhaar, Money
from shabd_agent import (
    Agent,
    AnthropicBackend,
    GeminiBackend,
    MockBackend,
    OpenAICompatBackend,
)

# ---- 1. Build a SHABD app with real spells -----------------------------
app = SHABD("universal-agent", secret="x" * 32, require_auth=False,
            grimoire_log_path="/tmp/universal-agent-audit.jsonl")


@app.spell
def transfer(from_acct: str, to_acct: str, amount: Money,
             customer_aadhaar: Aadhaar) -> dict:
    """Transfer money between two accounts. Audit-stamped."""
    return {"ok": True, "ref": f"TXN-{from_acct[-4:]}-{to_acct[-4:]}",
            "amount": str(amount)}


@app.spell
def balance(acct: str) -> dict:
    """Look up the balance of an account."""
    return {"acct": acct, "balance_inr": 100_000.0}


# ---- 2. Pick a backend by command-line flag ----------------------------
def pick_backend():
    if "--openai" in sys.argv:
        return OpenAICompatBackend(
            base_url="https://api.openai.com/v1",
            api_key=os.environ["OPENAI_API_KEY"],
            model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
        )
    if "--anthropic" in sys.argv:
        return AnthropicBackend(
            model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5"),
        )
    if "--gemini" in sys.argv:
        return GeminiBackend(
            model=os.environ.get("GEMINI_MODEL", "gemini-1.5-flash"),
        )
    if "--ollama" in sys.argv:
        return OpenAICompatBackend(
            base_url=os.environ.get("OLLAMA_URL",
                                    "http://localhost:11434/v1"),
            api_key="ollama",
            model=os.environ.get("OLLAMA_MODEL", "qwen2.5:1.5b"),
        )
    # Default: offline mock so the script just works.
    return MockBackend(plan=[
        {"tool": "balance", "args": {"acct": "A1001"}},
        {"tool": "transfer", "args": {
            "from_acct": "A1001", "to_acct": "B2002",
            "amount": "5000.00 INR",
            "customer_aadhaar": "123456789012",
        }},
        "Done. Ref: TXN-1001-2002. Balance was 100,000 INR.",
    ])


# ---- 3. Wire the agent and run it --------------------------------------
agent = Agent.from_shabd(
    app,
    llm=pick_backend(),
    system=("You are a careful Indian-banking ops assistant. "
            "Before any transfer, look up the source balance. "
            "Use exact amounts in 'AMOUNT INR' form. Audit-friendly."),
    verbose=True,
    max_steps=6,
)


if __name__ == "__main__":
    result = agent.run(
        "Transfer 5000 INR from account A1001 to B2002. "
        "Customer Aadhaar: 123456789012."
    )
    print()
    print("=" * 60)
    print(f"BACKEND   : {agent.llm.name()}")
    print(f"STOPPED   : {result.stopped_reason}")
    print(f"STEPS     : {len(result.steps)}")
    print(f"ELAPSED   : {result.total_elapsed_s:.2f}s")
    print("=" * 60)
    print(f"ANSWER    : {result.answer}")
    print("=" * 60)
    print(f"GRIMOIRE  : {app.grimoire.verify()}")
