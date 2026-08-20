"""
Proxy an existing external MCP server (e.g. a .NET service) through
SHABD, then drive it from a local agent loop.

Your real-world scenario looks like this:

    +----------------+  bearer token   +-------------------+
    | .NET MCP       |  on every RPC   | SHABD proxy       |
    | http://...:9036|<----------------|  (this script)    |
    +----------------+                 +-------------------+
                                                 |
                                  local agent loop, any LLM
                                  (OpenAI / Anthropic / Ollama /
                                   internal Llama / Mistral)

What this file does:

  1. Connects to the external MCP server with a Bearer token.
  2. Imports every tool the .NET server advertises into the local
     SHABD app — they become `app.spell`s automatically, so they
     inherit Grimoire audit, idempotency, RBAC, semantic-type
     validation, rate limits.
  3. Exposes a small agent runner that uses any LLM (the example uses
     a 'pretend' LLM but the shape mirrors OpenAI / Anthropic).
  4. Every external MCP call is recorded in the local hash-chained
     audit log — so even if the .NET server lies about what happened,
     your SHABD audit is the source of truth on the client side.

Usage:
    DOTNET_MCP_URL=http://172.19.18.204:9036/mcp \\
    DOTNET_MCP_TOKEN=eyJhbGciOi...real-bearer... \\
    python examples/proxy_dotnet_mcp.py
"""
from __future__ import annotations

import json
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shabd import SHABD, MCPClient

# 1. Configure
DOTNET_URL = os.environ.get("DOTNET_MCP_URL", "http://172.19.18.204:9036/mcp")
DOTNET_TOKEN = os.environ.get("DOTNET_MCP_TOKEN", "")
SHABD_SECRET = os.environ.get("SHABD_SECRET", "x" * 32)


def build_app() -> SHABD:
    """Build the SHABD proxy app and import every .NET tool into it."""
    app = SHABD("dotnet-bridge", secret=SHABD_SECRET, require_auth=False,
                grimoire_log_path="/tmp/dotnet-bridge-audit.jsonl")

    # 2. Connect to the external .NET MCP server
    upstream = MCPClient(
        name="dotnet",
        transport="http",
        url=DOTNET_URL,
        auth_token=DOTNET_TOKEN,
        prefix=True,        # tools become 'dotnet__toolName' locally
        timeout=15.0,
    )

    if not DOTNET_TOKEN:
        print("WARNING: DOTNET_MCP_TOKEN is empty — every call will fail "
              "with 401. Set it before running for real.")

    tools: list = []
    try:
        tools = upstream.connect()
        print(f"Connected to .NET MCP at {DOTNET_URL}")
        print(f"Discovered {len(tools)} upstream tools:")
        for t in tools:
            print(f"  - {t.get('name')}: {t.get('description', '')[:60]}")
        # Register every upstream tool as a local SHABD spell. Every call
        # now lands in the local Grimoire chain, gets idempotency, rate
        # limiting, RBAC etc. — without the .NET team changing anything.
        upstream.register_on(app)
    except Exception as e:
        print(f"Could not reach .NET MCP at {DOTNET_URL}: {e}")
        print("Continuing with an empty tool list — fix the URL/token "
              "and rerun.")

    print(f"\nLocally registered {len(app._spells)} spells")
    return app, upstream


# ---------------------------------------------------------------------------
# 4. A tiny agent runner. The 'LLM' is a placeholder; swap in OpenAI /
#    Anthropic / Ollama / your internal model. The contract is the
#    standard OpenAI tool-calling shape, which Anthropic / Ollama also
#    expose.
# ---------------------------------------------------------------------------
def pretend_llm(messages: list, tools: list) -> dict:
    """Stand-in for a real LLM. In production, paste your provider here."""
    if not tools:
        return {"role": "assistant",
                "content": "No tools advertised; nothing to call."}
    target = tools[0]["function"]["name"]
    return {
        "role": "assistant", "content": None,
        "tool_calls": [{
            "id": f"call-{uuid.uuid4().hex[:8]}",
            "type": "function",
            "function": {"name": target, "arguments": "{}"},
        }],
    }


def run_agent_once(app: SHABD) -> None:
    """One round of agent <-> tools, all routed through SHABD."""
    from shabd_client import SHABDClient
    base = "http://localhost:8765"

    client = SHABDClient(base)
    tools = client.tools_for_openai()
    print(f"\nAgent sees {len(tools)} tools (proxied from .NET).")

    messages = [
        {"role": "system",
         "content": "You can call .NET tools through SHABD. "
                    "Every call is audited locally."},
        {"role": "user",
         "content": "Pick the first available tool and call it."},
    ]
    resp = pretend_llm(messages, tools)
    print("LLM said:", json.dumps(resp, indent=2))

    if "tool_calls" in resp:
        tool_results = client.dispatch_openai_tool_calls(resp["tool_calls"])
        for tr in tool_results:
            print("Tool result:", tr)

    print("\nGrimoire integrity:", client.grimoire_verify())


# ---------------------------------------------------------------------------
# 5. Wire it together
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    app, upstream = build_app()

    if "--client" in sys.argv:
        run_agent_once(app)
    else:
        print("\nServing on http://localhost:8765")
        print("In another shell run:  "
              "python examples/proxy_dotnet_mcp.py --client")
        try:
            app.serve(port=8765)
        finally:
            upstream.close()
