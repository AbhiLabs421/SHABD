# Flowise Integration — Step-by-Step

[Flowise](https://flowiseai.com/) is a visual drag-and-drop LLM agent
builder. You wire nodes together — LLM, memory, prompt, tools — and
end up with a chat endpoint. SHABD plugs in as the **Tools** layer:
every `@app.spell` becomes a Flowise "Custom Tool" or imports as one
"OpenAPI Tool".

This chapter walks you through both, with an actual demo to copy.

---

## Why pair Flowise with SHABD?

| Concern | Flowise alone | Flowise + SHABD |
|--|--|--|
| Easy to build an agent | ✅ | ✅ |
| Tool versioning | ❌ | ✅ via the SHABD manifest |
| Auth on tool calls | weak | ✅ HMAC tokens with scopes |
| Audit trail of what the model did | ❌ | ✅ Grimoire (signed, hash-chained) |
| Tool input validation | basic | ✅ semantic types (Email, Aadhaar, GSTIN, …) |
| Idempotent writes | ❌ | ✅ `Idempotency-Key` |
| Bank / regulated deployment | hard | ✅ designed for it |

Pattern: **Flowise builds the agent UX; SHABD enforces the rules and
records the receipts.**

---

## Setup — three pieces

You'll run:

1. **SHABD server** on port 8765.
2. **Flowise** on port 3000 (or wherever).
3. **An LLM** (OpenAI, Anthropic, Ollama, internal — any provider Flowise
   supports).

### Step 1: start SHABD

```bash
python examples/flowise_integration.py
```

The example registers three spells (`add`, `search_docs`, `lookup_kyc`)
and exposes them at `http://localhost:8765/spells/<name>`. Verify in
your browser:

```
http://localhost:8765/manifest    -> JSON list of spells
http://localhost:8765/openapi.json -> OpenAPI 3.1 spec
http://localhost:8765/dashboard    -> live Playground
```

### Step 2: install Flowise

```bash
npx flowise start
# open http://localhost:3000
```

### Step 3: build an agent

Create a new "Chatflow" and drop in these nodes:

```
ChatOpenAI / Ollama / Anthropic    ─┐
                                    │
Buffer Memory                       ├──>  Agent (Tool Calling Agent)  ──> Chat
                                    │
SHABD tools (see below)            ─┘
```

Now you have two ways to add the tools.

---

## Approach 1 — Import the OpenAPI spec (60 seconds, recommended)

Flowise has a built-in "OpenAPI Toolkit" node.

1. Drag **"OpenAPI Toolkit"** into the canvas.
2. Set **Specification URL** to `http://localhost:8765/openapi.json`.
3. Wire its output into the Agent node's **Tools** input.
4. *(Optional)* Set **Headers** to:
   ```json
   {
     "Authorization": "Bearer <SHABD-TOKEN>",
     "Idempotency-Key": "{{ uuid }}"
   }
   ```

That's it. Every SHABD spell is now a tool, with the correct schema,
description, and parameters. The Agent can call them by name.

---

## Approach 2 — One Custom Tool per spell (more control)

Use this when you want per-tool prompt hints or different auth headers
per tool.

For each spell, drag a **"Custom Tool"** node:

| Field | Value |
|--|--|
| **Tool Name** | `add` (must match the spell name) |
| **Tool Description** | `"Add two numbers. Use this when the user asks for a sum."` |
| **JavaScript Function** | _(see below)_ |

JavaScript function body:

```javascript
const fetch = require('node-fetch');
const body = JSON.stringify({ a: $a, b: $b });
const r = await fetch('http://localhost:8765/spells/add', {
  method: 'POST',
  headers: {
    'Content-Type': 'application/json',
    'Authorization': `Bearer ${process.env.SHABD_TOKEN || ''}`,
    'Idempotency-Key': require('crypto').randomUUID(),
  },
  body,
});
const data = await r.json();
if (data.error) {
  // SHABD's structured errors round-trip cleanly — let the agent
  // self-correct using did_you_mean / hint / example.
  return JSON.stringify(data.error);
}
return JSON.stringify(data.result);
```

Repeat for every spell. The Schema fields (`$a`, `$b`, etc.) come from
the spell's input schema — pull them from `/manifest`.

---

## A worked example: KYC agent

Goal: a chat that helps support staff look up KYC status by customer
ID, and politely refuses if the spell raises.

1. **System prompt** in Flowise:
   > "You are a KYC assistant. Use `lookup_kyc` for any customer
   > lookup. If the tool returns an error, explain it in plain English
   > to the agent."

2. **Add tools** via Approach 1.

3. **Try it**: chat with the agent — *"Is customer C-9001 KYC-verified?"*

   The agent calls `lookup_kyc({customer_id: "C-9001"})`, which goes
   over HTTP to SHABD, which:
   * validates the input,
   * runs the spell,
   * stamps the Grimoire chain,
   * returns the dict.

4. **Verify**: hit `http://localhost:8765/grimoire/verify` — every
   tool call your Flowise agent made is in the chain.

---

## Production wiring

When you go live:

1. Put SHABD behind your reverse proxy with TLS.
2. Set `require_auth=True` on SHABD; mint a Flowise-specific token
   with scopes for only the spells Flowise should call:
   ```python
   token = app.issue_token("flowise-prod",
                           scopes=["kyc-read", "report-generate"],
                           ttl=86400)
   ```
3. Put that token in Flowise's per-tool **Headers** field.
4. Enable mTLS via `MTLSConfig` if Flowise runs on a different node.
5. Wire **OTLP traces** so Flowise's trace IDs flow through every
   spell call (Flowise already emits W3C `traceparent`).
6. Wire **`audit_webhook_url`** so every spell call also lands in your
   SIEM, independent of the SHABD audit chain.

---

## Walkthrough script

```bash
# Terminal 1 — start SHABD
python examples/flowise_integration.py

# Terminal 2 — see the Flowise-ready shapes
python examples/flowise_integration.py --client
```

The client prints both the Custom-Tool JSON shapes (Approach 2) and
the OpenAPI URL (Approach 1) you'd paste into Flowise.

---

## Common pitfalls

| Symptom | Fix |
|--|--|
| Agent calls a spell that doesn't exist | The OpenAPI spec is cached — re-import or restart the Flowise node. |
| `403 forbidden` from SHABD | The token's scopes don't cover the spell. Mint with broader scopes or fix the RBAC. |
| `idempotency_conflict` | Flowise reused the same UUID for a different body. Make sure your tool function generates a new UUID per call. |
| Latency spikes | SHABD's overhead is ~5-15 ms per call. If your spell body is fast, that's the floor. Behind nginx with HTTP keep-alive it's much lower. |
| Audit chain breaks | An external process edited the JSONL file. Investigate immediately (see `docs/runbook.md`). |
