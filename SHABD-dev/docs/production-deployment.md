# Production Deployment

This chapter walks you from "single Python file" to "production-grade
deployment" — Docker, Kubernetes, systemd, secrets, secret rotation,
graceful shutdown, and the health checks orchestrators understand.

> **Honest scope.** SHABD v2.2 ships every code-level production
> primitive listed below, but it is *new software*. A full security
> audit, real load testing, and battle-time-in-production are still on
> the v3.0 roadmap. For low- to medium-stakes use today, the steps below
> are sufficient; for bank / fintech / healthcare workloads, treat them
> as a starting point, not a finish line.

## 1. Configure secrets correctly

In production, **never** let SHABD auto-generate its secret — tokens
will invalidate at every restart, and the Grimoire audit chain will not
verify across processes.

```bash
export SHABD_SECRET="$(openssl rand -hex 32)"
```

For systemd, drop this in `/etc/shabd/env` with `chmod 600`. For
Kubernetes, use a `Secret` resource (see `deploy/k8s.yaml`).

### Zero-downtime secret rotation

Pass the old secret(s) alongside the new one so tokens issued before
rotation keep working:

```python
app = SHABD(
    "prod",
    secret=os.environ["SHABD_SECRET"],           # current — used for signing
    additional_secrets=[                          # accepted on verify only
        os.environ.get("SHABD_SECRET_OLD", "").encode(),
    ],
)
```

Rotation procedure:

1. Generate a new secret.
2. Roll your fleet with `SHABD_SECRET=<new>` and `SHABD_SECRET_OLD=<old>`.
3. Once your token TTL has expired (default 1h), drop `SHABD_SECRET_OLD`.

## 2. Persist the audit chain to disk

The Grimoire chain is in-memory by default. For real deployments,
point it at a JSONL file on a persistent volume:

```python
app = SHABD("prod",
            secret=os.environ["SHABD_SECRET"],
            grimoire_log_path="/var/lib/shabd/audit.jsonl")
```

The file is append-only and `fsync`'d on every page, so a crash loses at
most the in-flight call. Recovery is automatic — at startup, SHABD
re-loads the chain and verifies it.

Object-storage mirroring (S3, GCS, MinIO) gives you cheap, immutable
off-host archives — a `find` + `aws s3 cp` cron is plenty.

## 3. Stream the audit chain into your SIEM

```python
app = SHABD("prod",
            secret=os.environ["SHABD_SECRET"],
            grimoire_log_path="/var/lib/shabd/audit.jsonl",
            audit_webhook_url="https://siem.example.com/ingest",
            audit_webhook_secret=os.environ["SIEM_HMAC_SECRET"])
```

Every Grimoire page is HMAC-signed and POSTed asynchronously to the
webhook URL. Failures are logged and never block the call path.

## 4. Health checks for orchestrators

| Endpoint     | When to use                                       |
|--------------|---------------------------------------------------|
| `/healthz`   | Liveness probe — "is the process alive?"          |
| `/readyz`    | Readiness probe — "should traffic be routed here?" |
| `/startupz`  | Startup probe — "did the warmup finish?"          |

`/readyz` returns **503** during graceful shutdown (see §5), so load
balancers stop routing to a draining pod before in-flight requests are
killed.

The Kubernetes manifest in `deploy/k8s.yaml` wires all three probes
correctly, including `terminationGracePeriodSeconds: 45` and a
`PodDisruptionBudget` so rolling deploys don't take you below quorum.

## 5. Graceful shutdown

SHABD installs a `SIGTERM` handler that:

1. Flips `/readyz` to 503 immediately.
2. Stops accepting new spell calls (`shutting_down` error).
3. Waits up to `shutdown_grace_s` (default 30 s) for in-flight calls to
   finish.
4. Closes the Grimoire log file cleanly.

This makes `kubectl rollout restart` and `docker compose down` lossless.

## 6. Metrics — Prometheus + Grafana

The `/metrics` endpoint speaks Prometheus exposition format when the
client advertises it via `Accept: text/plain; version=0.0.4` (or
`?format=prom`):

```
# HELP shabd_calls_total Total spell invocations
# TYPE shabd_calls_total counter
shabd_calls_total{spell="transfer",status="ok"} 142
shabd_calls_total{spell="transfer",status="error"} 3

# HELP shabd_call_duration_ms Spell duration (ms) quantiles
# TYPE shabd_call_duration_ms summary
shabd_call_duration_ms{spell="transfer",quantile="0.5"}  12.4
shabd_call_duration_ms{spell="transfer",quantile="0.95"} 48.1
shabd_call_duration_ms{spell="transfer",quantile="0.99"} 91.7
```

The bundled `docker-compose.yml` brings up Prometheus + Grafana already
wired to scrape SHABD.

## 7. Distributed tracing — W3C TraceContext

SHABD parses incoming `traceparent` headers and uses the supplied
`trace_id`, recording its own `span_id` so each request stitches into a
larger distributed trace.

The bundled `SHABDClient` propagates `traceparent` automatically on every
outgoing call.

## 8. Idempotency — banking-grade retry safety

Pass `Idempotency-Key: <uuid>` on writes:

* First call with that key → executes the spell, records the response.
* Subsequent calls with the same key + same body → return the cached
  response (success or failure).
* Same key + different body → 400 `idempotency_conflict` so callers
  notice the bug.

`SHABDClient.cast(spell, body, idempotency_key=...)` handles the wire
format for you.

## 9. Per-spell concurrency caps

When a spell calls a fragile downstream (a slow database, a third-party
API), cap its in-flight count without taking the whole server down:

```python
@app.spell(max_concurrent=20, rate_limit=200)
def settle_payment(...): ...
```

`max_concurrent` is a semaphore — calls beyond the cap wait their turn,
not refused.

## 10. Container & supply-chain hygiene

The bundled `Dockerfile`:

* Runs as a non-root user (`UID 1000`).
* Uses `python:3.12-slim` (small, predictable, official).
* Mounts a `/data` volume for the audit log.
* Wires a Docker healthcheck against `/readyz`.

Zero runtime dependencies mean nothing to patch on CVE Tuesday besides
Python itself.

## 11. Logging

Logs are structured JSON on `stderr`:

```json
{"ts": 1780534242.85, "level": "INFO", "msg": "shabd listening",
 "host": "0.0.0.0", "port": 8765}
```

Pipe to Loki, OpenSearch, or your favourite log store. The `trace_id`
field appears on every spell-related line so log searches stitch with
your traces.

## 12. CI / CD

`.github/workflows/ci.yml` runs:

* `ruff` lint
* `mypy` light type check
* The full `tests/test_shabd.py` and `tests/test_enterprise.py` suites
  across Python 3.10 / 3.11 / 3.12 / 3.13
* `tests/test_comparison.py` against a live FastMCP install
* A Docker build + smoke-test against `/healthz`

## Quick deployment checklists

### Single VM (systemd)

```bash
sudo useradd -r -s /usr/sbin/nologin shabd
sudo mkdir -p /opt/shabd /var/lib/shabd /etc/shabd
sudo cp shabd.py shabd_client.py /opt/shabd/
sudo cp my_server.py /opt/shabd/server.py
sudo cp deploy/shabd.service /etc/systemd/system/
echo "SHABD_SECRET=$(openssl rand -hex 32)" | sudo tee /etc/shabd/env
sudo chmod 600 /etc/shabd/env
sudo systemctl daemon-reload && sudo systemctl enable --now shabd
```

### Docker

```bash
docker build -t shabd:2.2 .
docker run -d --name shabd -p 8765:8765 \
  -e SHABD_SECRET=$(openssl rand -hex 32) \
  -v $PWD/audit:/data \
  shabd:2.2
```

### Kubernetes

```bash
# Edit deploy/k8s.yaml — replace SHABD_SECRET with a real one
kubectl apply -f deploy/k8s.yaml
```

That's it. Live, drained on rolling deploys, audit-chain-backed.
