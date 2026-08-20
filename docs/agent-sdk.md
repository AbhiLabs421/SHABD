# Agent SDK — `shabd_client.py`

`shabd_client.py` is a single-file, zero-dependency Python client. Use it
to build an agent in **five lines**:

```python
from shabd_client import SHABDClient

client = SHABDClient("http://localhost:8765", token=TOKEN)
tools  = client.tools_for_openai()                  # or .tools_for_anthropic()
result = client.cast("transfer", {"from_acct": "A100", "to_acct": "B200",
                                  "amount": "5000.00 INR",
                                  "customer_aadhaar": "123456789012"})
```

## What the client does for you

| Feature                          | How |
|----------------------------------|-----|
| Bearer auth                      | `token=` constructor arg |
| W3C trace propagation            | auto-generated `traceparent` on every request |
| Banking-grade safe retry         | `idempotency_key=` arg → `Idempotency-Key` header |
| Network retries (429 / 5xx)      | exponential backoff, configurable via `retries=` |
| Structured errors                | `SHABDClientError` carries `hint`, `did_you_mean`, `example` |
| LLM tool format                  | `tools_for_openai()` and `tools_for_anthropic()` |
| Tool-call dispatch               | `dispatch_openai_tool_calls(...)` |

## Build a real agent — three-step loop

```python
import openai
from shabd_client import SHABDClient

shabd = SHABDClient("http://localhost:8765", token=os.environ["SHABD_TOKEN"])
tools = shabd.tools_for_openai()
messages = [{"role": "user", "content": "Move ₹5000 from A100 to B200."}]

while True:
    resp = openai.chat.completions.create(
        model="gpt-4o", messages=messages, tools=tools, tool_choice="auto",
    )
    msg = resp.choices[0].message.model_dump()
    messages.append(msg)
    if not msg.get("tool_calls"):
        print(msg["content"])
        break

    # SHABDClient handles auth, traceparent, idempotency, retries, errors.
    messages.extend(shabd.dispatch_openai_tool_calls(msg["tool_calls"]))
```

## Anthropic Messages API

```python
import anthropic
from shabd_client import SHABDClient

shabd = SHABDClient("http://localhost:8765")
tools = shabd.tools_for_anthropic()

client = anthropic.Anthropic()
resp = client.messages.create(
    model="claude-sonnet-4-6",
    tools=tools,
    max_tokens=1024,
    messages=[{"role": "user", "content": "Quote me 100 RELIANCE."}],
)

for block in resp.content:
    if block.type == "tool_use":
        result = shabd.cast(block.name, dict(block.input))
        # feed result back into another `messages.create(...)` round
```

## Error handling for agents

`SHABDClientError` carries the structured fields SHABD adds so the
agent can self-correct without a human:

```python
from shabd_client import SHABDClient, SHABDClientError

try:
    shabd.cast("send_email", {"to": "alice-at-example", "subject": "Hi"})
except SHABDClientError as e:
    print(e.code)         # "validation_failed"
    print(e.hint)         # "user@domain.tld"
    print(e.example)      # "alice@example.com"
    print(e.did_you_mean) # None
    print(e.trace_id)     # to grep logs / traces / Grimoire by
```

## Safe retries

```python
import uuid

ide = f"transfer-{uuid.uuid4()}"
result = shabd.cast("transfer", body, idempotency_key=ide)

# Network blip? Retry with the *same* key — never double-charges.
result2 = shabd.cast("transfer", body, idempotency_key=ide)
assert result == result2
```

## Verifying the audit chain from an agent

```python
v = shabd.grimoire_verify()
assert v["ok"], f"audit chain broken: {v}"
print(f"Verified {v['pages']} pages; head = {v['head']}")
```

## Customising the client

```python
shabd = SHABDClient(
    "https://shabd.internal.bank",
    token=os.environ["SHABD_TOKEN"],
    timeout=10.0,    # per-request timeout (s)
    retries=3,       # exponential backoff on 429/5xx and network errors
    user_agent="trading-bot/1.4",
)
```

## When NOT to use this client

* If your agent is Node / Go / Rust — call SHABD's HTTP endpoints
  directly. The client adds nothing the wire format doesn't already.
* If you need streaming spell output, use `POST /stream/{name}` directly;
  the client's `cast()` is request/response only.
