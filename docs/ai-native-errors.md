# AI-Native Errors

Every error SHABD raises is shaped so that **a calling LLM can self-correct
without a human in the loop**. Three structured fields show up across the
board:

* **`hint`** — a short human-/LLM-readable instruction on what to fix.
* **`example`** — a known-good value the LLM can copy the shape of.
* **`did_you_mean`** — the closest matches, when the issue looks like a typo.

## Example 1 — Spell-name typo

```python
app.invoke("serach_docs", {"query": "mcp"})
```

```json
{
  "error": {
    "code": "spell_not_found",
    "message": "no such spell: serach_docs",
    "hint": "Did you mean 'search_docs'?",
    "did_you_mean": ["search_docs"]
  }
}
```

The LLM doesn't need to ask the user — it can just retry with the
suggested name.

## Example 2 — Bad semantic value

```python
app.invoke("send_email", {"to": "alice-at-example", "subject": "Hi"})
```

```json
{
  "error": {
    "code": "validation_failed",
    "message": "to does not match expected format",
    "hint": "user@domain.tld",
    "example": "alice@example.com"
  }
}
```

The LLM sees both the expected shape and a copy-pasteable example.

## Example 3 — Missing required arg

```python
app.invoke("send_email", {"to": "alice@example.com"})
```

```json
{
  "error": {
    "code": "validation_failed",
    "message": "value: missing required 'subject'"
  }
}
```

The error names the missing field directly, not a stack trace.

## How spell-name suggestions are computed

SHABD uses `difflib.get_close_matches` over all registered spell names
with `cutoff=0.5` and `n=3`. It's a stdlib function — no model call, no
dependency.

## How to author your own AI-native errors

Any `ConjureError` subclass accepts `hint`, `did_you_mean`, and `example`
as keyword arguments:

```python
from shabd import ValidationError

raise ValidationError(
    "amount must be positive",
    hint="amount is in INR; must be > 0",
    example=100.0,
)
```

These fields propagate through HTTP, MCP, and local invocation paths
identically.

## Try it

```bash
python examples/ai_native_errors_demo.py
```
