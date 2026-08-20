# Feature Map — What runs when

A single page that answers two questions an evaluator always asks:

1. **What does this component actually do?**
2. **Which one runs on which request, in what order?**

If you have ten minutes before a customer demo, read this page.

---

## 1. The whole stack on one diagram

```
        +-------------------+
        |  LLM Agent        |  Flowise / OpenAI / Anthropic / Ollama
        |  (uses tools)     |
        +---------+---------+
                  |
                  | HTTP  (Bearer token, traceparent, Idempotency-Key)
                  v
        +--------------------------------------------------------------+
        |  SHABD HTTP / SSE / WebSocket                                |
        |                                                              |
        |  1. parse headers (auth, traceparent, idempotency)           |
        |  2. _check_authz       <-- spell.scopes                      |
        |  3. _check_rate        <-- rate_limit + RateLimiter          |
        |  4. circuit breaker    <-- last N failures                   |
        |  5. idempotency cache  <-- Idempotency-Key replay            |
        |  6. concurrency cap    <-- max_concurrent semaphore          |
        |  7. before-hooks       <-- RBAC, SoD, sanctions block        |
        |  8. spell body         <-- YOUR Python function              |
        |  9. cache write        <-- if cache_ttl                      |
        | 10. after-hooks        <-- OTLP exporter, custom audit       |
        | 11. Grimoire append    <-- hash chain + redacted PII         |
        | 12. persistence        <-- SQLite / JSONL / Encrypted        |
        | 13. audit webhook      <-- SIEM (Kafka / HTTP)               |
        | 14. cluster replicate  <-- ClusterPeer                       |
        |                                                              |
        +--------------------------------------------------------------+
                  |
                  | ssl.SSLContext  (mTLS optional)
                  v
        +-------------------+
        |  Operating system |
        |  + HSM (optional) |
        +-------------------+
```

Every numbered step is a real, named class in the codebase. If a step
isn't wired up, it's silently skipped — no surprise behaviour.

---

## 2. Which file owns each step

| Step | Component | Where it lives |
|------|-----------|----------------|
| 1 | Header parsing | `shabd.py` — `_AsyncHttpServer._handle_call` |
| 2 | Scope check | `shabd.py` — `Conjure._check_authz` |
| 3 | Rate limit | `shabd.py` — `RateLimiter` |
| 4 | Circuit breaker | `shabd.py` — `CircuitBreaker` |
| 5 | Idempotency cache | `shabd.py` — `IdempotencyStore` |
| 6 | Concurrency cap | `shabd.py` — `Spell._semaphore` |
| 7 | Before-hooks | `shabd.py` — `Conjure._before_hooks` (RBAC etc. wire here) |
| 8 | Spell body | your code |
| 9 | Result cache | `shabd.py` — `TTLCache` |
| 10 | After-hooks | `shabd.py` — `Conjure._after_hooks` |
| 11 | Grimoire append | `shabd.py` — `Grimoire.append` |
| 12 | Persistence | `shabd.py` — `GrimoireJSONL` or `shabd_enterprise.py` — `SQLiteGrimoirePersistence` / `EncryptedGrimoireJSONL` |
| 13 | Audit webhook | `shabd.py` — `AuditWebhook` |
| 14 | Cluster replication | `shabd_enterprise.py` — `ClusterPeer` |

The split between `shabd.py` and `shabd_enterprise.py` is deliberate:
the first is the zero-dep core a security team can audit in a day;
the second is the optional sidecar a bank can opt into class-by-class.

---

## 3. When you would turn each thing on

| Thing | Turn it on when |
|-------|-----------------|
| `require_auth=True` | Always in production. |
| `scopes=[...]` on spells | The moment you have more than one client. |
| `Idempotency-Key` | Any write spell. Mandatory in banking. |
| `max_concurrent` | A spell calls a fragile downstream. |
| `cache_ttl` | A read spell is idempotent and frequently called with the same args. |
| `rate_limit` | You don't want a runaway agent to DDoS yourself. |
| Semantic types | Always. Free input validation + PII auto-redaction. |
| `grimoire_log_path` | Day 1 in production. |
| `audit_webhook_url` | Your SIEM team will not accept "the audit lives on the pod". |
| `SQLiteGrimoirePersistence` | Your DBA team wants the audit chain in a file they back up. |
| `EncryptedGrimoireJSONL` | The data classification policy says "at rest must be encrypted". |
| `RBACPolicyEngine` | More than two roles. |
| `SeparationOfDutiesPolicy` | Wire transfers, treasury ops, anything with material risk. |
| `OTLPSpanExporter` | Your platform team already runs Jaeger / Tempo. |
| `KafkaAuditStreamer` | Your SIEM is Kafka. |
| `MTLSConfig` | Zero-trust network mandate. |
| `ClusterPeer` | You need DR or HA. |
| `HSMKeyProvider` | RBI tier-1 bank checklist. |
| `X509Signer` | Auditor wants courtroom-grade non-repudiation. |

---

## 4. End-to-end example traces

### A. A bank clerk opens an account

```
HTTP POST /spells/open_account
  Authorization: Bearer <clerk-token>
  Idempotency-Key: open-acct-2025-06-04-1234
  traceparent: 00-<trace>-<span>-01

  1. parse headers           ok
  2. check scope             clerk-token has "clerk" -> ok
  3. rate limit              under 60/min -> ok
  4. circuit breaker         closed -> ok
  5. idempotency             not seen -> proceed
  6. concurrency             1 of 50 in flight -> ok
  7. before-hooks            RBAC: "clerk" allow "open_account" -> ok
                             sanctions.block_if_sanctioned("Ravi") -> ok
  8. spell body              return {"account_id": "A-1234567", ...}
  9. cache write             skipped (no cache_ttl)
 10. after-hooks             OTLP: span exported to Tempo
 11. Grimoire append         page 0xab12... appended, chain head moves
 12. persistence             SQLite: INSERT INTO grimoire_pages
 13. audit webhook           POST to SIEM with HMAC signature
 14. cluster replicate       push page to peer-2.bank.internal
```

### B. A high-value wire transfer (dual control)

```
HTTP POST /spells/wire_transfer
  Authorization: Bearer <branch-manager-token>
  body includes approver_token (a second branch-manager)

  1-6 as before
  7. before-hooks            RBAC: "branch-manager" allow wire_transfer -> ok
                             SoD: approver_token verifies, different subject -> ok
  8. spell body              return {"ref": "WIRE-1234", ...}
  ...
```

If the approver_token was issued to the same person, SoD raises a
`ForbiddenError` at step 7 and the wire never reaches step 8.

### C. A pre-trade limit breach

```
HTTP POST /spells/check_pre_trade
  Authorization: Bearer <algo-token>

  1-6 as before
  7. before-hooks            RBAC: "algo" allow check_pre_trade -> ok
  8. spell body              pretrade pack raises ConjureError(
                               "position_limit_breach",
                               hint="Would take net position to 60000;
                                     limit is +/-50000."
                             )
 11. Grimoire append         page recorded with ok=false
```

The order never reaches the exchange. The audit chain records both
the attempt and the reason — exactly what SEBI wants in a surveillance
review.

---

## 5. Cheat-sheet for an evaluator

A regulator-grade review usually asks five questions. Here's where to
point them:

| Question | Pointer |
|----------|---------|
| "How do you authenticate?" | `security.md` — HMAC tokens, scopes, rotation |
| "How do you authorise?" | `enterprise-features.md` — RBAC + SoD + scopes |
| "How is the audit log tamper-evident?" | `grimoire.md` |
| "How do you detect / replay AI actions?" | `runbook.md` + `/replay/<trace_id>` |
| "How do you handle PII?" | `semantic-types.md` (PII flag → auto-redaction) |
| "How is it deployed?" | `production-deployment.md` |

Print this page out and walk into the room. Most demos are won at this
level of detail, not at "we have 99.9% uptime".
