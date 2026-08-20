# Security & Auth

## The secret

Everything signed by SHABD — tokens, Grimoire pages — uses one symmetric
secret. **Provide it via environment variable in production**:

```bash
export SHABD_SECRET="a-long-random-string-at-least-32-bytes-please"
```

```python
app = SHABD("prod", require_auth=True)   # picks up SHABD_SECRET
```

If you don't provide one, SHABD generates an ephemeral key per process
and prints a warning. Tokens issued by one process won't be accepted by
another, and Grimoire chains won't verify across restarts.

## HMAC tokens

```python
token = app.issue_token("alice", scopes=["read", "admin"], ttl=3600)
```

A token is a `body.signature` pair (URL-safe base64). The body is JSON:

```json
{"sub": "alice", "scopes": ["read", "admin"],
 "iat": 1780500000, "exp": 1780503600,
 "jti": "ab12cd34ef567890"}
```

The signature is `HMAC-SHA256(secret, body)`. Verification checks the
signature, the expiry, and a replay-cache (`jti`) so the same token can't
be reused after a `verify()` call.

## Scopes

```python
@app.spell(scopes=["admin"])
def delete_everything() -> dict:
    ...
```

Calls without an `admin` scope are rejected with HTTP 403 and:

```json
{"error": {"code": "forbidden", "message": "missing scope: admin"}}
```

The special scope `*` grants access to everything — use sparingly.

## Rate limiting

```python
@app.spell(rate_limit=20, rate_window=60.0)   # 20 calls / minute per subject
def expensive_op() -> dict:
    ...
```

Excess calls raise `RateLimitError` (HTTP 429) with a `retry_after` field.
A `RedisPlugin` is available if you need distributed limiting across
multiple SHABD instances.

## Circuit breaker

After 5 consecutive failures, a spell's circuit opens for 30 seconds and
returns:

```json
{"error": {"code": "internal_error",
           "message": "circuit open for 'flaky_op'",
           "retry_after": 30}}
```

This prevents one broken downstream from being hammered by a confused LLM.

## Tamper-evident audit log

Every call, success or failure, also appends to the [Grimoire](grimoire.md)
chain — so even an admin who can edit logs can't edit the audit trail
without leaving a detectable break.

## CORS

```python
app = SHABD("api", cors_origin="https://app.example.com")
```

Default is `*`. Tighten this in production.

## What SHABD does NOT do

* **OAuth 2.1 / OIDC** — FastMCP has this built-in; SHABD does not. If
  you need OAuth, either put SHABD behind an OAuth gateway, or use
  FastMCP for the auth layer and SHABD for compliance/audit.
* **Field-level encryption** — Grimoire hashes redact PII, but if you
  need stored payloads encrypted at rest, do it in your spell body.
* **Mutual TLS** — terminate it at your reverse proxy.

## Minimal production config

```python
import os
from shabd import SHABD

app = SHABD(
    "prod-api",
    secret=os.environ["SHABD_SECRET"],
    require_auth=True,
    default_timeout=10.0,
    default_rate=60,
    cors_origin="https://app.example.com",
)
```
