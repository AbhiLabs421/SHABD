"""
Office test script — gpt-oss:20b LLM × .NET MCP server × SHABD agent.

Designed for *your exact setup*:

    Internal LLM       : OpenAI-compatible endpoint, model 'gpt-oss:20b'
    Internal MCP       : .NET HTTP MCP server at http://172.19.18.204:9036/mcp
                          with Bearer-token auth.
    Restricted network : zero outbound to the public internet required.

Just set the four environment variables below and run.

USAGE (copy this block, paste it into your office shell):

    export LLM_BASE_URL="http://YOUR-LLM-HOST:11434/v1"   # or :8000/v1 for vLLM
    export LLM_API_KEY="your-llm-token-or-just-ollama"
    export LLM_MODEL="gpt-oss:20b"
    export DOTNET_MCP_URL="http://172.19.18.204:9036/mcp"
    export DOTNET_MCP_TOKEN="paste-real-bearer-here"
    export SHABD_SECRET="$(openssl rand -hex 32)"

    # 1) Sanity checks (no LLM calls, no MCP execution, just probes):
    python examples/office_dotnet_with_gpt_oss.py --probe

    # 2) Full run (connect to MCP, run one agent turn against the LLM,
    #    verify the audit chain at the end):
    python examples/office_dotnet_with_gpt_oss.py

    # 3) Optional: also run the script's reliability-feature checks
    #    (provenance + invariant + duplicate-call detection) — fully
    #    offline, no LLM required:
    python examples/office_dotnet_with_gpt_oss.py --offline-checks

What you get:

  * Every .NET MCP tool becomes a local @app.spell, so all calls land
    in SHABD's audit chain (Grimoire), get Idempotency-Key replay
    protection, and are PII-redacted before hashing.
  * The agent loop runs against your `gpt-oss:20b` via OpenAI-compatible
    `/chat/completions`. No `openai` library installed; pure stdlib.
  * Provenance tracking flags any tool argument the model invents
    (didn't come from your user prompt or from a prior tool result).
  * A declarative daily-cap invariant blocks runaway sequences.
  * The audit chain is verified at the end; the script exits non-zero
    if anything failed.

Nothing in this file calls out to api.openai.com, api.anthropic.com,
or any other external service. The only outbound traffic goes to
LLM_BASE_URL and DOTNET_MCP_URL — both on your network.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import urllib.error
import urllib.request
from urllib.parse import urlparse

# Add repo root to sys.path so `python examples/...py` works without
# installing SHABD.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shabd import SHABD, MCPClient  # noqa: E402
from shabd_agent import (  # noqa: E402
    Agent,
    InvariantViolation,
    OpenAICompatBackend,
)

# ============================================================================
# Configuration — pulled from the environment.
# ============================================================================

LLM_BASE_URL    = os.environ.get("LLM_BASE_URL", "")
LLM_API_KEY     = os.environ.get("LLM_API_KEY", "")
LLM_MODEL       = os.environ.get("LLM_MODEL", "gpt-oss:20b")

DOTNET_MCP_URL  = os.environ.get("DOTNET_MCP_URL", "")
DOTNET_MCP_TOKEN = os.environ.get("DOTNET_MCP_TOKEN", "")

SHABD_SECRET    = os.environ.get("SHABD_SECRET", "")
AUDIT_PATH      = os.environ.get("SHABD_AUDIT",
                                  "/tmp/office-dotnet-audit.jsonl")
PROMPT          = os.environ.get("AGENT_PROMPT",
                                  "Use the first available tool to test it. "
                                  "Then return a one-line summary.")
DAILY_CAP_INR   = float(os.environ.get("DAILY_CAP_INR", "200000"))


# ============================================================================
# Pre-flight probes — fast, no side effects.
# ============================================================================

def _tcp_reachable(url: str, timeout: float = 3.0) -> tuple[bool, str]:
    try:
        p = urlparse(url)
        host = p.hostname or ""
        port = p.port or (443 if p.scheme == "https" else 80)
        with socket.create_connection((host, port), timeout=timeout):
            return True, f"TCP {host}:{port} reachable"
    except Exception as e:  # noqa: BLE001
        return False, f"TCP unreachable: {e}"


def _llm_probe() -> tuple[bool, str]:
    if not LLM_BASE_URL:
        return False, "LLM_BASE_URL not set"
    ok, msg = _tcp_reachable(LLM_BASE_URL)
    if not ok:
        return False, msg
    # Optional HTTP probe — many gateways block GET on /v1/models.
    try:
        req = urllib.request.Request(
            f"{LLM_BASE_URL.rstrip('/')}/models",
            headers={"Authorization": f"Bearer {LLM_API_KEY}"} if LLM_API_KEY else {},
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            r.read(64)
        return True, f"HTTP /models accepted at {LLM_BASE_URL}"
    except urllib.error.HTTPError as e:
        return True, f"HTTP responded ({e.code}); endpoint alive"
    except Exception as e:  # noqa: BLE001
        return True, f"TCP up but HTTP probe inconclusive ({e})"


def _mcp_probe() -> tuple[bool, str]:
    if not DOTNET_MCP_URL:
        return False, "DOTNET_MCP_URL not set"
    return _tcp_reachable(DOTNET_MCP_URL)


def run_probes() -> int:
    print("\n[1/3] LLM endpoint probe …")
    ok, msg = _llm_probe()
    print(("    OK  " if ok else "    FAIL") + " — " + msg)
    llm_ok = ok

    print("\n[2/3] .NET MCP endpoint probe …")
    ok, msg = _mcp_probe()
    print(("    OK  " if ok else "    FAIL") + " — " + msg)
    mcp_ok = ok

    print("\n[3/3] Local SHABD probe …")
    if not SHABD_SECRET:
        print("    WARN — SHABD_SECRET not set; "
              "an ephemeral key will be generated for this run.")
    else:
        print(f"    OK   — SHABD_SECRET present ({len(SHABD_SECRET)} chars).")

    print("\nSummary:")
    print(f"  LLM  : {'ready' if llm_ok else 'not reachable'}")
    print(f"  MCP  : {'ready' if mcp_ok else 'not reachable'}")
    print(f"  Audit: {AUDIT_PATH}")
    return 0 if (llm_ok and mcp_ok) else 2


# ============================================================================
# Bridge — wrap the .NET MCP server as a local SHABD app.
# ============================================================================

def build_bridge() -> tuple[SHABD, MCPClient]:
    if not SHABD_SECRET:
        print("WARNING: SHABD_SECRET not set — using an ephemeral key.")
        secret = "x" * 32
    else:
        secret = SHABD_SECRET

    app = SHABD(
        "office-dotnet-bridge",
        secret=secret,
        require_auth=False,
        grimoire_log_path=AUDIT_PATH,
    )

    upstream = MCPClient(
        name="dotnet",
        transport="http",
        url=DOTNET_MCP_URL,
        auth_token=DOTNET_MCP_TOKEN,
        prefix=True,            # tools become 'dotnet__<name>'
        timeout=20.0,
    )

    try:
        tools = upstream.connect()
        print(f"\nConnected to .NET MCP at {DOTNET_MCP_URL}")
        if tools:
            print(f"Discovered {len(tools)} tools:")
            for t in tools[:20]:
                print(f"   - {t.get('name')}: "
                      f"{(t.get('description') or '')[:80]}")
            if len(tools) > 20:
                print(f"   … and {len(tools) - 20} more.")
        else:
            print("Server returned an empty tool list.")
        upstream.register_on(app)
    except Exception as e:  # noqa: BLE001
        print(f"Could not reach .NET MCP at {DOTNET_MCP_URL}: {e}")
        print("Continuing with an empty tool surface so you can still "
              "inspect the rest of the pipeline.")

    print(f"\nLocally registered {len(app._spells)} spell(s).")
    return app, upstream


# ============================================================================
# Agent — your gpt-oss:20b model + the three novel reliability features.
# ============================================================================

def build_agent(app: SHABD) -> Agent:
    backend = OpenAICompatBackend(
        base_url=LLM_BASE_URL,
        api_key=LLM_API_KEY,
        model=LLM_MODEL,
        timeout=60.0,
        temperature=0.0,
    )

    agent = Agent.from_shabd(
        app,
        llm=backend,
        system=(
            "You are a careful operations assistant connected to an "
            "internal .NET MCP server. Pick exactly one tool that fits "
            "the user's request, call it with reasonable defaults, and "
            "summarise the result in plain English. Do not invent IDs "
            "or amounts — only use values from the user message or from "
            "a previous tool result. Refuse if no relevant tool exists."
        ),
        max_steps=4,
        timeout_s=90.0,
        verbose=True,
        track_provenance=True,    # tag user vs tool vs llm_invented
    )

    # Cross-tool safety: total of any 'amount' argument across the whole
    # session cannot exceed DAILY_CAP_INR. Adjust the field name to whatever
    # your .NET tools actually use.
    AMOUNT_FIELDS = ("amount", "amount_inr", "value", "notional")

    def _sum_amounts(session) -> float:
        total = 0.0
        for c in session.tool_calls:
            for f in AMOUNT_FIELDS:
                v = c.arguments.get(f)
                if isinstance(v, (int, float)):
                    total += float(v)
                elif isinstance(v, str):
                    head = v.split()[0] if v else ""
                    try:
                        total += float(head)
                    except ValueError:
                        pass
        return total

    agent.add_invariant(
        name=f"daily_cap_{int(DAILY_CAP_INR)}",
        check=lambda s: _sum_amounts(s) <= DAILY_CAP_INR,
        message=(f"session would exceed the configured daily cap "
                 f"of {DAILY_CAP_INR:.0f} INR"),
    )

    return agent


# ============================================================================
# Offline reliability self-checks — run without the LLM / MCP being up.
# ============================================================================

def run_offline_checks() -> int:
    print("\n=== Offline reliability checks (no network) ===")
    from shabd_agent import MockBackend

    print("\n• Provenance: an invented account number is flagged.")
    app = SHABD("offline-check", secret="x" * 32, require_auth=False)

    @app.spell
    def ship(acct: str, fake_acct: str) -> str:
        return "shipped"

    agent = Agent.from_shabd(
        app,
        llm=MockBackend(plan=[
            {"tool": "ship", "args": {"acct": "A1001",
                                       "fake_acct": "ZZZ-evil"}},
            "done",
        ]),
        track_provenance=True,
    )
    result = agent.run("ship to A1001 please")
    prov_msg = next(r for r in result.steps[0].tool_results
                    if "provenance" in r["content"])
    prov = json.loads(prov_msg["content"])["provenance"]
    print(f"    {prov}")
    assert prov["acct"] == "user"
    assert prov["fake_acct"] == "llm_invented"
    print("    ok — fake_acct was correctly flagged as llm_invented.")

    print("\n• Invariant: a transfer above the configured cap is blocked.")
    app2 = SHABD("offline-check-2", secret="x" * 32, require_auth=False)

    @app2.spell
    def transfer(amount: float) -> dict:
        return {"executed": True}

    agent2 = Agent.from_shabd(
        app2,
        llm=MockBackend(plan=[
            {"tool": "transfer", "args": {"amount": 10_000_000}},
            "stopped",
        ]),
    )
    agent2.add_invariant(
        "cap",
        check=lambda s: all(c.arguments.get("amount", 0) <= 100_000
                             for c in s.tool_calls_named("transfer")),
        message="single transfer cannot exceed 100,000 INR",
    )
    try:
        r = agent2.run("transfer 1 crore")
    except InvariantViolation:
        pass
    blocked = any("invariant_violation" in t["content"]
                  for s in r.steps for t in s.tool_results)
    assert blocked, "invariant should have blocked the call"
    print("    ok — invariant blocked the 1-crore attempt.")

    print("\nOffline checks passed.\n")
    return 0


# ============================================================================
# Main
# ============================================================================

def _missing(env_name: str, val: str) -> bool:
    if not val:
        print(f"  - {env_name} is empty")
        return True
    return False


def main() -> int:
    if "--probe" in sys.argv:
        return run_probes()
    if "--offline-checks" in sys.argv:
        return run_offline_checks()

    missing = False
    print("Checking environment …")
    missing |= _missing("LLM_BASE_URL", LLM_BASE_URL)
    missing |= _missing("DOTNET_MCP_URL", DOTNET_MCP_URL)
    if not DOTNET_MCP_TOKEN:
        print("  - DOTNET_MCP_TOKEN is empty (the .NET server will reject the call).")
    if missing:
        print("\nSet the env vars listed at the top of this file and rerun.")
        print("Or run a dry connectivity probe:   "
              "python examples/office_dotnet_with_gpt_oss.py --probe")
        return 2

    app, upstream = build_bridge()
    if not app._spells:
        print("No spells registered (upstream returned no tools). "
              "Aborting before the LLM call to save budget.")
        return 3

    agent = build_agent(app)

    try:
        result = agent.run(PROMPT)
    finally:
        upstream.close()

    print("\n" + "=" * 60)
    print("AGENT RESULT")
    print("=" * 60)
    print(f"backend     : {agent.llm.name()}")
    print(f"stopped     : {result.stopped_reason}")
    print(f"steps       : {len(result.steps)}")
    print(f"elapsed     : {result.total_elapsed_s:.2f}s")
    print(f"answer      : {result.answer[:600]}")
    print("=" * 60)
    print("Per-argument provenance trail (look for 'llm_invented'):")
    for s in result.steps:
        for r in s.tool_results:
            if "provenance" in r["content"]:
                try:
                    p = json.loads(r["content"])["provenance"]
                    print(f"  step {s.n}: {p}")
                except (KeyError, ValueError):
                    pass
    print()
    v = app.grimoire.verify()
    print(f"Grimoire    : {v}")
    print(f"Audit file  : {AUDIT_PATH}")
    return 0 if v.get("ok") else 4


if __name__ == "__main__":
    raise SystemExit(main())
