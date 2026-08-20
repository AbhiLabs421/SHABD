"""
AI-native errors — every error returns a `hint`, `example`, and a
`did_you_mean` suggestion so the calling LLM can self-correct without
asking a human.

    python examples/ai_native_errors_demo.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shabd import SHABD, Email

app = SHABD("errors", secret="x" * 32, require_auth=False)


@app.spell
def search_docs(query: str, limit: int = 10) -> dict:
    return {"hits": []}


@app.spell
def send_email(to: Email, subject: str) -> dict:
    return {"queued": True}


def show(label, fn):
    print("─" * 60)
    print(label)
    print("─" * 60)
    try:
        fn()
    except Exception as e:
        if hasattr(e, "to_dict"):
            print(json.dumps(e.to_dict(), indent=2))
        else:
            print(e)
    print()


show("1. Typo in the spell name — `serach_docs` instead of `search_docs`",
     lambda: app.invoke("serach_docs", {"query": "mcp"}))

show("2. Bad value for a semantic-typed field",
     lambda: app.invoke("send_email", {"to": "alice-at-example",
                                       "subject": "Hi"}))

show("3. Missing required arg",
     lambda: app.invoke("send_email", {"to": "alice@example.com"}))

show("4. Wrong type",
     lambda: app.invoke("search_docs", {"query": 42}))
