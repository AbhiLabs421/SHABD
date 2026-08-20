# Getting Started

## Install

SHABD is a single file with no required dependencies. Pick one:

```bash
# Option A — clone the repo
git clone https://github.com/Kumar123ips/SHABD.git
cd SHABD

# Option B — drop the single file into your project
curl -O https://raw.githubusercontent.com/Kumar123ips/SHABD/main/shabd.py
```

Requirements: Python 3.10+.

## Your first spell

```python
# server.py
from shabd import SHABD

app = SHABD("hello", secret="please-change-me-32-bytes-or-more!!", require_auth=False)

@app.spell
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b

@app.spell
def greet(name: str = "World") -> str:
    return f"Namaste, {name}!"

if __name__ == "__main__":
    app.serve(port=8765)
```

Run it:

```bash
python server.py
```

Now hit it three different ways:

### 1. Browser

Open `http://localhost:8765/dashboard`. You'll see a Playground with auto-generated forms for every spell — click, fill, run.

### 2. curl

```bash
curl -X POST http://localhost:8765/spells/add \
  -H "Content-Type: application/json" \
  -d '{"a": 5, "b": 7}'
# {"ok": true, "result": 12, "trace_id": "..."}
```

### 3. From Python

```python
from server import app
print(app.invoke("add", {"a": 5, "b": 7}))  # 12
```

## What the decorator gave you, for free

When you wrote `@app.spell`, SHABD inspected your function and generated:

* A JSON Schema for the inputs (from your type hints).
* A JSON Schema for the output.
* An MCP-compatible tool definition.
* An OpenAPI 3.1 entry under `/openapi.json`.
* A row in the dashboard's Playground.
* An entry in the Grimoire audit chain on every call.
* Validation, rate limiting, caching, and audit logging — all wired up.

## Next steps

* [Semantic Types](semantic-types.md) — make `email`, `aadhaar`, `gstin` first-class.
* [Grimoire](grimoire.md) — see the tamper-evident audit chain you just inherited.
* [AI-Native Errors](ai-native-errors.md) — make your spell server LLM-friendly.
