# Runbook — Operating SHABD in production

A page you can hand to an on-call engineer. Every symptom maps to a
SHABD-specific signal and a concrete next step.

## On-call quick reference

| Symptom                            | First thing to check                     |
|------------------------------------|-------------------------------------------|
| Pod won't become Ready             | `GET /readyz` — is it draining?           |
| Latency p95 spike                  | `shabd_call_duration_ms{quantile="0.95"}` per-spell |
| Sudden error wave                  | `shabd_calls_total{status="error"}` per spell |
| Auth failures                      | grep logs for `"unauthorized"`            |
| Audit chain broken                 | `GET /grimoire/verify` returns `ok: false`|
| Disk filling                       | `du -sh /var/lib/shabd/audit.jsonl`       |
| Suspicious traffic from one user   | `shabd_calls_total` by `subject` (label TBD) |

## Endpoints to know

```
GET /healthz                 always-200 liveness
GET /readyz                  503 during shutdown
GET /startupz                synonym for /readyz (warmup hook)
GET /metrics?format=prom     Prometheus text exposition
GET /grimoire/verify         Verify the audit chain integrity
GET /grimoire/head           Current head hash + page count
GET /grimoire/pages          Dump pages for compliance
GET /manifest                All spells and their schemas
GET /dashboard               Browser playground
POST /replay/<trace_id>      Re-run a past call (debug)
```

## Runbook entries

### "Service is up but `/readyz` returns 503"

The service received `SIGTERM` (deploy, OOM kill, `docker stop`) and is
draining. Look at `kubectl describe pod` or `journalctl -u shabd` for
the shutdown trigger. The pod will exit once in-flight calls finish or
the grace period (`shutdown_grace_s`) expires.

### "Grimoire verify failed"

```bash
curl -fsS http://localhost:8765/grimoire/verify
# {"ok": false, "reason": "hash mismatch", "at_seq": 17}
```

**What this means.** Someone (or something) edited a past audit page.
This is supposed to be impossible without the secret — investigate
**immediately**.

Steps:

1. Snapshot `/var/lib/shabd/audit.jsonl` to read-only storage right now.
2. Check filesystem audit logs (`auditd` / `falco`) for writes to that
   file from anything other than the SHABD process.
3. Pull the last good page hash via `GET /grimoire/pages?since=<seq-1>&limit=2`
   on a peer instance, if you have one.
4. Treat as a P1 security incident.

### "Latency p95 spike on a specific spell"

1. Open the Grafana panel filtered by `spell=` label.
2. Check the upstream the spell calls — `kubectl top pods` / DB latency
   dashboard.
3. If you can't immediately fix the upstream, lower `max_concurrent` on
   that spell and roll. Excess calls will queue, not pile on the broken
   downstream.

### "Idempotency conflicts in logs"

You're seeing `idempotency_conflict` errors. That means a client is
sending the same `Idempotency-Key` with different request bodies — that
is a client bug, not a server bug. The error envelope tells the caller
where to look:

```json
{"error": {
   "code": "idempotency_conflict",
   "message": "idempotency key reused with a different request body",
   "hint": "Send a fresh Idempotency-Key for a different request, ..."
}}
```

### "Tokens stopped working after a deploy"

You forgot `additional_secrets=[old_key]` during rotation. The old
tokens were signed with a key the new pods no longer know.

Roll forward by adding the old secret back in `additional_secrets`,
then plan the rotation properly (see
[security.md](security.md) — Zero-downtime secret rotation).

### "Disk full from audit log"

The Grimoire JSONL is append-only. Options:

1. **Rotate** — copy + truncate is **not** safe; it breaks the chain.
   Instead, use a *new* log path after a clean shutdown:
   ```python
   app = SHABD(..., grimoire_log_path="/var/lib/shabd/audit-2025-Q4.jsonl")
   ```
   Archive the old file to immutable storage (S3 Object Lock / GCS
   Bucket Lock).
2. **Off-host streaming** — wire `audit_webhook_url` so the chain also
   lands in your SIEM; locally you can keep only the last N days.

### "Memory growing"

The Grimoire keeps the last `100_000` pages in memory. At ~500 B/page
that's ~50 MB — bounded. The TTL cache evicts on `cache_ttl`. If memory
keeps growing past that, suspect a spell holding references — profile
with `tracemalloc`.

## Backups & DR

* **Audit log** — `audit.jsonl` is the source of truth for compliance.
  Replicate to immutable storage continuously.
* **Tokens** — there is no token database; tokens are self-contained
  HMAC tokens. Backing up the `SHABD_SECRET` is enough.
* **State** — SHABD itself is stateless apart from the audit log; pods
  can come and go freely.

## Rolling upgrade

```bash
kubectl set image deploy/shabd -n shabd shabd=shabd:2.3
kubectl rollout status deploy/shabd -n shabd
```

The Deployment uses `maxUnavailable: 0` and `terminationGracePeriodSeconds:
45`, so:

* New pods start with `/startupz` failing until ready.
* Old pods drain on SIGTERM, return 503 from `/readyz`, finish in-flight.
* Service balances to ready pods only.

You should see zero error-rate change in Grafana.

## Useful one-liners

```bash
# How busy is each spell right now?
curl -s 'http://shabd:8765/metrics?format=prom' \
  | grep '^shabd_calls_total' | sort

# Audit chain integrity, programmatically.
test "$(curl -s http://shabd:8765/grimoire/verify | jq -r .ok)" = "true"

# Tail the structured logs into a human format.
journalctl -u shabd -o json | jq '.MESSAGE | fromjson'
```
