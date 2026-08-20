"""
Build an agent in ~30 lines using SHABDClient.

Run a SHABD server in another shell, then:

    python examples/agent_with_shabd.py

This deliberately doesn't depend on any LLM SDK — it shows the *shape*
of an agent loop. Swap the `pretend_llm()` for a real OpenAI / Anthropic /
Ollama call and you have a production agent.
"""
from __future__ import annotations

import json
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shabd_client import SHABDClient


def pretend_llm(messages: list, tools: list) -> dict:
    """
    Stand-in for a real LLM. Returns a message that asks to call the first
    spell with hard-coded arguments. Replace with a real model call.
    """
    target = tools[0]["function"]["name"]
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [{
            "id": f"call-{uuid.uuid4().hex[:8]}",
            "type": "function",
            "function": {"name": target,
                         "arguments": json.dumps({"a": 7, "b": 13})},
        }],
    }


def run() -> None:
    base = os.environ.get("SHABD_URL", "http://localhost:8765")
    client = SHABDClient(base)
    print("Server:", client.health())

    # Pull the tool catalog from SHABD (auto-derived from @app.spell).
    tools = client.tools_for_openai()
    print(f"\n{len(tools)} tools advertised to the LLM:")
    for t in tools:
        print(f"  - {t['function']['name']}: {t['function']['description']}")

    # One round of the loop. Real agents loop until the model stops calling
    # tools and returns a final assistant message.
    messages: list = [{"role": "user", "content": "what is 7 + 13?"}]
    resp = pretend_llm(messages, tools)
    messages.append(resp)
    print("\nModel asked to call:", resp["tool_calls"])

    # Dispatch every tool_call back through SHABD. The client handles auth,
    # idempotency keys, traceparent, retries, and structured error replay.
    tool_results = client.dispatch_openai_tool_calls(resp["tool_calls"])
    messages.extend(tool_results)
    print("\nTool results fed back to the model:")
    for m in tool_results:
        print(f"  {m['name']}: {m['content']}")

    # Real flow: send `messages` back to the LLM here. The model would now
    # produce a final answer using the tool output.
    print("\nGrimoire (every step recorded):", client.grimoire_verify())


if __name__ == "__main__":
    run()
