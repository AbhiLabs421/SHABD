# Security Policy

## Reporting a vulnerability

If you believe you've found a security issue in SHABD, please email
**ipsabhi423@gmail.com** with the subject line `SECURITY: SHABD`.

* Encrypt sensitive details with our PGP key if possible (request it in
  a first contact email).
* Please include a minimal reproduction.
* We will acknowledge within **72 hours** and aim to provide a fix or
  mitigation within **14 days** for high-severity issues.

We ask that you do **not** open a public GitHub issue for security
problems until a fix is released.

## Supported versions

Only the latest minor version receives security fixes.

| Version | Supported |
|---------|-----------|
| 2.2.x   | ✅        |
| 2.1.x   | ✅ (until 2.3) |
| ≤ 2.0   | ❌        |

## Threat model

SHABD is designed to be *one auditable file* (`shabd.py`) plus an
optional client (`shabd_client.py`). Both are pure-Python, stdlib-only,
so the supply-chain surface is small.

### Trust boundaries

| Surface              | Trust level | Notes |
|----------------------|-------------|-------|
| HTTP request body    | Untrusted   | Validated against the spell's JSON schema. |
| Token bearer         | Untrusted   | HMAC-verified; replay-protected via `jti`. |
| `traceparent` header | Untrusted   | Parsed defensively (regex match or ignored). |
| `Idempotency-Key`    | Untrusted   | Fingerprint compared in constant time. |
| Audit chain on disk  | Trusted (write); Untrusted (read) | Verifiable via signature + hash chain. |
| Spell function body  | **Fully trusted** — runs in-process. Don't deserialize user data into Python objects naïvely. |

### What SHABD protects against

* **Token forgery** — HMAC-SHA256 with constant-time compare.
* **Token replay** — `jti` cache rejects reuse within the TTL window.
* **Audit-log tampering** — Grimoire chain breaks on any past-page edit.
* **PII leakage to audit** — semantic types flag PII; values are masked
  before hashing.
* **Idempotent-write double-execution** — `Idempotency-Key` cache.
* **Validation bypass via missing fields** — schema requires explicit
  `required` lists; `additionalProperties: false` is the default.

### What SHABD does **not** protect against

* **A compromised host** — if an attacker can run code as the SHABD
  process, they can do anything that process can.
* **A leaked secret** — `SHABD_SECRET` rotation is supported
  (`additional_secrets=[old]`), but a leaked secret should be rotated
  immediately.
* **A malicious spell author** — anyone with commit access to your
  spell code has full server access.
* **Denial-of-service from upstream** — use `max_concurrent` and rate
  limits to cap blast radius; consider a CDN / WAF at the edge.

### Known cryptographic choices

* HMAC-SHA256 for tokens and Grimoire signatures.
* SHA-256 for the audit hash chain.
* `secrets.token_bytes` / `secrets.token_hex` for ID generation.
* `hmac.compare_digest` for every secret-bearing comparison.

If you believe any of these are inappropriate for your threat model,
please open a discussion before deploying SHABD for sensitive workloads.

## Hardening checklist for production

* [ ] `SHABD_SECRET` provided via environment, never on the command line.
* [ ] `require_auth=True`.
* [ ] Specific `cors_origin=` (not `"*"`).
* [ ] Spells use `scopes=` so least-privilege tokens are possible.
* [ ] `Idempotency-Key` required on every write endpoint.
* [ ] `grimoire_log_path=` set; the log is mirrored off-host.
* [ ] `audit_webhook_url=` streams the audit chain to a SIEM.
* [ ] Reverse proxy (nginx / Envoy / Caddy) terminates TLS.
* [ ] Container runs `runAsNonRoot: true` (the bundled image does).
* [ ] `terminationGracePeriodSeconds: 45` (or your `shutdown_grace_s` + 15s).
