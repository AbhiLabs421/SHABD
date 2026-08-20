# SHABD — Production Readiness & Enterprise Hardening Plan

> **Goal.** Make SHABD a *complete, sellable* product an enterprise (bank /
> clearing corp / regulated fintech) can buy and run in production — strong on
> security and operations — **without giving up the core promise**: pure Python,
> zero required dependencies, no Docker, no downloaded images, air-gap friendly.
>
> **The organising idea (read this first).** Every production dependency an
> enterprise normally installs — an identity provider (Keycloak), a cache /
> coordination store (Redis), a database — becomes a **pluggable provider** in
> SHABD with **two interchangeable backends**, chosen in `config.yaml`:
>
> 1. **Built-in (default):** a pure-Python implementation SHABD ships as code —
>    nothing to download, runs as part of / alongside SHABD.
> 2. **External (opt-in):** point at the customer's existing real service
>    (their Keycloak URL, their Redis, their Postgres) via config.
>
> So a customer with nothing gets a working, secure stack out of one Python
> tree; a customer who already runs Keycloak/Redis just flips a config key and
> uses theirs. **Same SHABD, their choice.**

---

## Table of contents

1. [The pluggable-provider pattern](#1-the-pluggable-provider-pattern)
2. [What SHABD already has (build on this)](#2-what-shabd-already-has-build-on-this)
3. [`config.yaml` — the single control surface](#3-configyaml--the-single-control-surface)
4. [Component A — Built-in Identity Server (“Praman”)](#4-component-a--built-in-identity-server-praman)
5. [Component B — Built-in Cache / Coordination (“Smriti”)](#5-component-b--built-in-cache--coordination-smriti)
6. [Component C — Persistence](#6-component-c--persistence)
7. [Honest security position (the “more secure than Keycloak” question)](#7-honest-security-position-the-more-secure-than-keycloak-question)
8. [Security hardening checklist to be “sellable”](#8-security-hardening-checklist-to-be-sellable)
9. [Phased roadmap & effort](#9-phased-roadmap--effort)
10. [Decisions to confirm](#10-decisions-to-confirm)

---

## 1. The pluggable-provider pattern

One interface, two backends, selected by config. This already works for cache
(`ConjurePlugin` → `TTLCache` in-process vs `RedisPlugin` external). We
generalise it to **identity** and formalise it everywhere:

```
              ┌─────────────────────────────────────────────┐
              │             SHABD app / UI / API             │
              │   uses an interface, never a concrete impl    │
              └───────────────┬─────────────────────────────┘
                              │  config.yaml picks the backend
        ┌─────────────────────┼──────────────────────┐
        ▼                     ▼                        ▼
   IdentityProvider      CacheProvider          PersistenceProvider
   ├ builtin: Praman     ├ builtin: Smriti      ├ builtin: JSONL / SQLite
   └ external: Keycloak  └ external: Redis      └ external: Postgres
```

Rule: **the app depends only on the interface.** Swapping builtin↔external must
never touch business code — only `config.yaml`.

---

## 2. What SHABD already has (build on this)

We are NOT starting from zero. Inventory of production-relevant pieces that
already exist:

| Area | Already in code | Where |
|------|-----------------|-------|
| Token issue/verify | `TokenManager` — HMAC-SHA256 signed tokens, `jti` replay protection, multi-key rotation | `shabd.py` |
| Local users | `UserStore` — scrypt password hashing, roles (superuser/admin/user) | `shabd_users.py` |
| External Keycloak | `KeycloakConfig` — OIDC password-grant, stores access/refresh, reads `realm_access.roles` | `shabd_ui.py` |
| Enterprise auth | `LDAPAuthProvider`, `SAMLAuthProvider`, `SSOTokenExchanger`, `RBACPolicyEngine`, `SeparationOfDutiesPolicy` | `shabd_enterprise.py` |
| Secrets | `EnvKeyProvider`, `FileKeyProvider`, `HSMKeyProvider` | `shabd_enterprise.py` |
| Cache / rate-limit | `TTLCache` (builtin) + `RedisPlugin` (external), wired from config | `shabd.py` |
| Persistence | JSONL Grimoire (builtin) + `SQLiteGrimoirePersistence` + `PostgresGrimoirePersistence` + `EncryptedGrimoireJSONL` | `shabd_enterprise.py` |
| Transport security | TLS in `_AsyncHttpServer`; `MTLSConfig`, `X509Signer` | `shabd.py`, `shabd_enterprise.py` |
| Audit | Grimoire hash-chain + `AgentNotary` cross-org proofs | `shabd.py`, `shabd_notary.py` |
| Config | `ConfigLoader` / `from_config(path)` (YAML) | `shabd.py` |

**Gap analysis — what's genuinely new work:**

- ❌ **A built-in, standards-compatible Identity *Server*** (not just a token
  signer / external client). This is Component A — the main new build.
- ❌ **A built-in Redis-*like server*** that separate processes can share (today
  the builtin cache is in-process only; sharing needs external Redis). Component B.
- ⚠️ **Unifying** all the above under one `config.yaml` provider-selection block.
- ⚠️ **A security hardening pass** (§8) so it survives an enterprise review.

---

## 3. `config.yaml` — the single control surface

One file decides the entire production posture. Proposed shape:

```yaml
shabd:
  name: ccil-control-plane
  secret_source: { provider: file, path: /etc/shabd/secret.key }   # or env / hsm

identity:
  provider: builtin            # builtin | keycloak | ldap | saml | none
  builtin:
    issuer: https://shabd.ccil.internal
    token_alg: RS256           # RS256 (own RSA, JWKS-verifiable) | HS256 (HMAC)
    access_ttl: 900
    refresh_ttl: 28800
    password_policy: { min_len: 12, require_mixed: true, breach_check: true }
    mfa: { enabled: true, type: totp }        # RFC-6238 TOTP, pure stdlib
    lockout: { max_attempts: 5, window_s: 300 }
  keycloak:                    # used only if provider: keycloak
    server_url: https://keycloak.ccil.internal
    realm: ccil
    client_id: shabd
    client_secret_source: { provider: env, key: KC_CLIENT_SECRET }

cache:
  provider: builtin            # builtin (in-proc) | smriti (own server) | redis
  smriti: { bind: 127.0.0.1, port: 6390, resp_compatible: true }
  redis:  { url: redis://redis.ccil.internal:6379 }

persistence:
  provider: jsonl              # jsonl | sqlite | postgres
  encrypted: true              # EncryptedGrimoireJSONL when jsonl
  postgres: { dsn_source: { provider: env, key: PG_DSN } }

server:
  tls: { cert: /etc/shabd/tls.crt, key: /etc/shabd/tls.key }
  mtls: { enabled: false, ca: /etc/shabd/ca.crt }
  security_headers: true
  cors: { allow_origins: [] }
```

Secrets are **never inline** — always via a `*_source` provider
(env / file / hsm), reusing the existing `KeyProvider` classes.

---

## 4. Component A — Built-in Identity Server (“Praman”)

> **Praman** (प्रमाण = proof / authority / credential) — fits the Grimoire /
> Notary naming. This is SHABD's own, dependency-free identity provider.

### What it is
A minimal but **standards-compatible OAuth2 / OIDC authorization server** written
in pure Python, so:
- SHABD itself uses it for login, sessions, tokens, RBAC.
- **Other apps can trust it** the same way they'd trust Keycloak — because it
  exposes the standard endpoints and a **JWKS** so any service can verify tokens
  offline.

### Endpoints (the OIDC surface)
```
GET  /.well-known/openid-configuration   discovery document
GET  /praman/jwks                        public keys (for token verification)
POST /praman/token                       OAuth2 token endpoint
                                         (password, refresh_token, client_credentials)
GET  /praman/userinfo                    claims for an access token
GET  /praman/authorize                   auth-code flow (browser SSO)  [phase 2]
POST /praman/introspect                  RFC-7662 token introspection
POST /praman/revoke                      RFC-7009 revocation
```

### The crypto question (important, and solvable)
Standard OIDC clients verify tokens with **RS256** (RSA) via JWKS. Python's
standard library has HMAC/SHA but **no built-in RSA**. Two honest options:

- **RS256 (recommended for interop):** implement RSA sign/verify in pure Python.
  RSA is modular exponentiation, and Python ints are arbitrary-precision, so
  `sign = pow(hash, d, n)` / `verify = pow(sig, e, n)` is a few lines. Key
  generation (prime search via Miller-Rabin) is a one-time, few-hundred-ms cost.
  This gives us a real, JWKS-publishable public key **with zero libraries** and
  full third-party interop. *We must implement PKCS#1 v1.5 / PSS padding
  carefully and get it reviewed* (see §7).
- **HS256 (simplest, internal-only):** reuse today's HMAC `TokenManager`. Fine
  when only SHABD verifies its own tokens, but external services can't verify
  without sharing the secret. Offer as a config option for the simplest deploys.

`config.identity.builtin.token_alg` picks between them.

### Feature set (to match/*exceed* Keycloak on the essentials)
- Users, groups, roles, client apps (this is the DB-backed realm).
- Password grant + refresh + client-credentials; auth-code + PKCE in phase 2.
- **Password policy** (length, complexity, optional k-anonymity breach check
  against a local list — no external call).
- **MFA / TOTP** (RFC-6238) — pure `hmac`/`hashlib`, works offline; QR as SVG.
- **Brute-force lockout**, session limits, idle + absolute timeouts.
- **Every auth event written to the Grimoire** — login, token issue, role
  change, revocation — so identity actions are *tamper-evident*, which stock
  Keycloak does **not** give you. **This is our real differentiator.**

### External Keycloak option
Unchanged and kept first-class: `identity.provider: keycloak` routes login to the
customer's Keycloak (existing `KeycloakConfig` path), maps `realm_access.roles`
→ SHABD roles. Nothing to build — just surface it in `config.yaml`.

---

## 5. Component B — Built-in Cache / Coordination (“Smriti”)

> **Smriti** (स्मृति = memory). The pure-Python stand-in for Redis when you need
> **shared** state (rate-limits, sessions, idempotency, cache) across more than
> one SHABD process — without installing Redis.

- Today: `TTLCache` (in-process, single instance) and `RedisPlugin` (external).
- New: an optional **standalone Smriti server** (asyncio, stdlib sockets) that
  speaks a small command set (GET/SET/EXPIRE/INCR/DEL) — optionally
  **RESP-wire-compatible** so real `redis-py` clients and tooling work against it
  too. Persists to disk (append-only) for restart safety.
- `cache.provider: builtin | smriti | redis` selects in-process / own-server /
  external. The app code (`cache_get`/`cache_set`/`check_rate_limit`) does not
  change — only the provider behind the interface.

Scope honestly: Smriti targets SHABD's needs (cache, counters, locks,
rate-limit), **not** full Redis parity. That's enough for production SHABD and
keeps it small.

---

## 6. Component C — Persistence

Mostly done — just wire into `config.yaml`:
- `jsonl` (builtin, optionally `EncryptedGrimoireJSONL`) — default, zero-dep.
- `sqlite` — single-file, still zero external service.
- `postgres` — for HA / large volume (existing `PostgresGrimoirePersistence`).

No new engine needed; the work is config plumbing + migration/verify helpers.

---

## 7. Honest security position (the “more secure than Keycloak” question)

You asked for our identity server to be *“more secure than the original
Keycloak.”* I have to be straight with you, because selling to a bank depends on
this being *true*, not just claimed:

- **“Custom” does not automatically mean “more secure.”** Keycloak is
  battle-tested and pen-tested by thousands of deployments. A fresh
  implementation carries the classic *roll-your-own-auth* risk. Claiming
  “more secure than Keycloak” out of the box would not survive a security
  review and could cost credibility.
- **What we *can* honestly claim, and defend:**
  - **Smaller attack surface** — no Java stack, no bundled admin console CVEs, no
    container supply chain. Fewer moving parts = fewer holes.
  - **Air-gap native** — no external calls, no image pulls; critical for
    restricted bank networks where running Keycloak is itself painful.
  - **Tamper-evident identity audit** — every auth event chained into the
    Grimoire and anchorable via the Notary. Keycloak's event log is a plain DB
    table an admin can edit; ours is cryptographically verifiable. **This is a
    genuine security advantage**, and the honest headline for the pitch.
  - **Radically simpler to operate securely** — one config file, sane secure
    defaults, no 200-page hardening guide.
- **Non-negotiables before we sell it as the auth of record:**
  1. Implement standards exactly (OAuth2/OIDC, PKCS#1, TOTP) — no shortcuts.
  2. Constant-time comparisons, proper token expiry/rotation/revocation (we
     already do `hmac.compare_digest` + `jti` replay — keep that discipline).
  3. **Independent security review + pen-test** of Praman before go-live, and
     keep HS256/external-Keycloak as the fallback for customers who require a
     certified IdP.

**Recommended framing for mentor/customer:** *“A dependency-free, air-gap-ready
identity provider with tamper-evident, regulator-grade audit — simpler and
smaller than Keycloak, and able to defer to your Keycloak when you have one.”*
That is both compelling and true.

---

## 8. Security hardening checklist to be “sellable”

Product-level items an enterprise review will ask for:

- [ ] Secure-by-default config (TLS on, secure cookies `HttpOnly`+`Secure`+`SameSite`, CSRF on — already present in UI).
- [ ] Security headers (HSTS, CSP, X-Content-Type-Options, frame-deny).
- [ ] Secrets only via provider (env/file/hsm), never in code or logs; log redaction (already have `_redact_for_audit`).
- [ ] Password hashing = scrypt/argon-class (have scrypt); tune work factors.
- [ ] Full token lifecycle: short access TTL, rotating refresh, revocation list, `jti` replay guard (mostly present).
- [ ] RBAC + separation-of-duties enforced centrally (have `RBACPolicyEngine`, `SeparationOfDutiesPolicy`) — wire to every route.
- [ ] Rate-limiting / lockout on auth endpoints.
- [ ] Grimoire audit on every privileged action (spell create, role change, token issue, config change).
- [ ] Input validation everywhere (schema validation exists; extend to admin APIs).
- [ ] Supply-chain: zero runtime deps = near-zero CVE surface — make this a selling point, document it.
- [ ] Data at rest: `EncryptedGrimoireJSONL` / encrypted secrets.
- [ ] Backup/restore + key rotation runbook.
- [ ] `SECURITY.md`, threat model, and a pen-test report before GA.

---

## 9. Phased roadmap & effort

Ordered by dependency and value. Rough sizes (S/M/L).

| Phase | Deliverable | Size | Status |
|------:|-------------|:----:|--------|
| **0** | `config.yaml` provider-selection framework + `IdentityProvider` interface (`identity_from_config`); wire existing pieces behind it | **M** | ✅ done — `shabd_config.py` (`ProductionConfig`, zero-dep YAML subset parser, secret resolver, `config.example.yaml`); 17 tests |
| **1** | **Praman** core: realm store (users/roles/clients), `/token` (password+refresh+client-creds), `/userinfo`, HS256 JWT; Grimoire audit hook; standalone `PramanServer` | **L** | ✅ done — auth events wired into the Grimoire (`grimoire_audit_bridge`), tamper-evidence proven by test; auto-wired via `build_identity(app=…)` |
| **2** | Praman **RS256 + JWKS** (pure-Python RSA), `/.well-known`, `/introspect`, `/revoke` → real OIDC interop | **M** | ✅ done — `shabd_rsa.py`; third-party JWKS verify + alg-substitution defense tested |
| **3** | **MFA (TOTP)** + password policy + lockout + session hardening | **M** | ✅ TOTP + policy + lockout done; session hardening pending |
| **4** | **Smriti** cache/coordination server (+ optional RESP) and `cache.provider` wiring | **M** | ✅ done — `shabd_smriti.py` (RESP server+client, AOF, `SmritiCache` plugin, config selection); 18 tests |
| **5** | Auth-code + PKCE browser SSO; external-Keycloak/LDAP/SAML all selectable in config | **M** | Full SSO story + customer’s existing IdP |
| **6** | Security hardening pass (§8), `SECURITY.md`, threat model; prep for pen-test | **M** | ⏳ security headers (CSP/HSTS/Permissions-Policy, config-toggle) + `SECURITY.md` enterprise-auth section done; RBAC-everywhere + pen-test pending |

Each phase ships independently, stays test-covered, and does not break existing
features (the standing constraint).

---

## 10. Decisions to confirm

Before Phase 0/1 code starts, confirm:

1. **Token algorithm priority** — start HS256 (internal, fast) and add RS256/JWKS
   in Phase 2 *(recommended)*, or go straight to RS256?
2. **Naming** — OK to use **Praman** (identity) and **Smriti** (cache), matching
   the Grimoire/Notary theme? Or keep plain names (`shabd_identity`, `shabd_cache`)?
3. **Scope of “sell-ready”** — target an internal POC-grade bar first, then
   harden (§8) toward a real pen-tested GA? *(recommended)*
4. **Security review** — do you have access to a security team / pen-test for
   Praman before it becomes the auth of record? (Gates the “more secure” claim.)

---

### Change log
| Date | Author | Change |
|------|--------|--------|
| 2026-07-15 | Abhishek | Initial production-readiness & enterprise-auth plan: pluggable providers, built-in Identity Server (Praman) with external-Keycloak option, built-in cache (Smriti) with external-Redis option, honest security position, hardening checklist, phased roadmap. |
