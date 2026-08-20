"""
Local-LLM tool calling: SHABD + Ollama (or any OpenAI-compatible endpoint).

    # In one terminal:
    python examples/ollama_demo.py --serve

    # In another:
    OLLAMA_URL=http://localhost:11434/v1 OLLAMA_MODEL=llama3.1 \
        python examples/ollama_demo.py --client

What this shows:
  1. SHABD exposes a /manifest endpoint with OpenAI-compatible tool schemas.
  2. The client fetches them, gives them to Ollama as `tools=[...]`.
  3. When the model decides to call a tool, we POST to /spells/<name>
     and feed the result back into the conversation.
"""
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shabd import SHABD

app = SHABD("ollama-demo", secret="x" * 32, require_auth=False)


@app.spell
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


@app.spell
def lookup_user(user_id: int) -> dict:
    """Look up a user by id (stub)."""
    return {"id": user_id, "name": f"user_{user_id}"}


def serve() -> None:
    app.serve(port=8765)


def client() -> None:
    base = "http://localhost:8765"
    manifest = json.loads(urllib.request.urlopen(f"{base}/manifest").read())
    tools = [
        {"type": "function", "function": {
            "name": s["name"],
            "description": s["description"],
            "parameters": s["input_schema"],
        }}
        for s in manifest["spells"]
    ]
    print("Tools advertised to the LLM:")
    print(json.dumps(tools, indent=2))

    ollama = os.environ.get("OLLAMA_URL", "http://localhost:11434/v1")
    model = os.environ.get("OLLAMA_MODEL", "llama3.1")

    # Minimal chat-completions request
    req_body = {
        "model": model,
        "messages": [{"role": "user", "content": "What is 2 + 3?"}],
        "tools": tools,
        "tool_choice": "auto",
    }
    req = urllib.request.Request(
        f"{ollama}/chat/completions",
        data=json.dumps(req_body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as r:
        resp = json.loads(r.read())
    msg = resp["choices"][0]["message"]
    print("\nModel response:", json.dumps(msg, indent=2))

    for call in msg.get("tool_calls", []) or []:
        name = call["function"]["name"]
        args = json.loads(call["function"]["arguments"])
        spell_req = urllib.request.Request(
            f"{base}/spells/{name}",
            data=json.dumps(args).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(spell_req) as r:
            result = json.loads(r.read())
        print(f"\n→ {name}({args}) returned:", result)


if __name__ == "__main__":
    if "--serve" in sys.argv:
        serve()
    elif "--client" in sys.argv:
        client()
    else:
        print(__doc__)
