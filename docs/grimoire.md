# Grimoire — Tamper-Evident Audit Log

The Grimoire is SHABD's signature feature: a **hash-chained, HMAC-signed,
append-only log** of every spell ever cast. It makes "what did the AI
actually do?" cryptographically verifiable.

This is genuinely unique among MCP / tool frameworks today.

## How it works

Every spell call appends one page to the chain. Each page looks like:

```
{
  "prev":        "<sha-256 of previous page's hash>",
  "seq":         42,
  "ts":          1780504357.916,
  "trace_id":    "95967beabb59…",
  "spell":       "transfer",
  "subject":     "alice",
  "args_hash":   "<sha-256 of redacted args>",
  "result_hash": "<sha-256 of redacted result>",
  "ok":          true,
  "hash":        "<sha-256 of all the fields above>",
  "sig":         "<HMAC-SHA256(secret, hash)>"
}
```

Because each page commits to the previous page's hash, **editing any past
page invalidates every page that follows**. And because each page is
HMAC-signed, an outside reader who doesn't have the secret can't forge new
pages.

## Three things you can do

### 1. Verify the whole chain

```python
app.grimoire.verify()
# {"ok": True, "pages": 42, "head": "79615f2d…"}
```

`verify()` walks the chain top to bottom and checks three invariants on
every page:

1. `prev` matches the previous page's `hash`.
2. The recomputed `hash` matches the stored `hash`.
3. The HMAC signature is valid.

If any of these fails, it returns:

```python
{"ok": False, "reason": "hash mismatch", "at_seq": 17}
```

### 2. Inspect pages

```python
app.grimoire.head()                # current tip hash
app.grimoire.pages(since_seq=0)    # all pages
app.grimoire.pages(since_seq=100, limit=50)
```

### 3. Verify over HTTP

```bash
GET /grimoire/verify   # → {"ok": true, "pages": …, "head": "…"}
GET /grimoire/head     # → {"head": "…", "pages": N}
GET /grimoire/pages?since=0&limit=100
```

An external auditor can hit `/grimoire/verify` periodically without
needing access to the app's secret — the chain's integrity is structural,
not just cryptographic.

## PII safety

When a spell argument is flagged `x-pii: true` (see
[Semantic Types](semantic-types.md)), the value is **masked before
hashing**, so the Grimoire never commits to raw PII.

Example: `Aadhaar("123456789012")` is masked to `"12********12"` before
being hashed. An auditor can verify integrity over the redacted form
without ever seeing the original number.

## Why this matters

Regulators are asking AI builders one specific question:

> *"For a given AI action, can you prove, after the fact, what input was
>  given, what output was produced, and that the log hasn't been edited?"*

Today most MCP frameworks answer "we have logs" — which is not the same as
"we have *unforgeable* logs." Grimoire answers it cryptographically.

This makes SHABD a natural fit for:

* India's DPDPA compliance
* EU AI Act (Article 12 — logging requirements)
* RBI / SEBI guidance on automated decision systems
* Healthcare audit trails
* Any system where "did the AI do this?" needs to be provable

## Configuration

By default the chain keeps the last **100,000 pages** in memory. For
long-running deployments, persist `app.grimoire.pages()` to disk on a
timer (a few MB per million calls).

## Try it

```bash
python examples/grimoire_demo.py
```
