# SHABD — The Step-by-Step Usage Book

This is the single page you should read end-to-end if you've never used
SHABD before. Each step is small and self-contained; by Step 9 you have
a production-grade SHABD service with metrics, tracing, audit, and a
Kubernetes-ready container.

---

## Step 1 — Install (30 seconds)

SHABD is a single file with no runtime dependencies. Pick one:

```bash
# Clone the repo
git clone https://github.com/Kumar123ips/SHABD.git
cd SHABD

# Or drop the two files into your existing project
curl -O https://raw.githubusercontent.com/Kumar123ips/SHABD/main/shabd.py
curl -O https://raw.githubusercontent.com/Kumar123ips/SHABD/main/shabd_client.py
```

Requirements: Python 3.10+. Verify:

```bash
python tests/test_shabd.py        # 31 tests
python tests/test_enterprise.py   # 18 tests
```

---

## Step 2 — Your first spell (2 minutes)

```python
# server.py
from shabd import SHABD

app = SHABD("hello", secret="x" * 32, require_auth=False)

@app.spell
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b

app.serve(port=8765)
```

Run, then open `http://localhost:8765/dashboard`. Click `add`, fill in
the form, hit Run. That's the whole loop.

---

## Step 3 — Add semantic types (5 minutes)

Strings that mean something — Email, Aadhaar, GSTIN, IndianPhone, Money,
URL. Each validates at the boundary, surfaces meaning in the schema,
and PII-flagged ones are auto-redacted in the audit log.

```python
from shabd import SHABD, Email, Aadhaar, GSTIN, Money

app = SHABD("kyc", secret="x" * 32, require_auth=False)

@app.spell
def onboard(email: Email, aadhaar: Aadhaar, gstin: GSTIN,
            amount: Money) -> dict:
    return {"verified": True, "domain": str(email).split("@")[1]}

print(app.invoke("onboard", {
    "email": "shop@example.com",
    "aadhaar": "123456789012",
    "gstin": "27AAPFU0939F1ZV",
    "amount": "1500.00 INR",
}))
```

Invalid values produce LLM-readable errors with `hint` + `example`. See
[semantic-types.md](semantic-types.md) for the full catalog and how to
roll your own.

---

## Step 4 — The Grimoire audit chain (3 minutes)

Every call appends a hash-chained, HMAC-signed page. Verify integrity
in O(n); raw PII never enters the chain.

```python
app.invoke("onboard", {...})
app.invoke("onboard", {...})

app.grimoire.verify()
# {"ok": True, "pages": 2, "head": "ca1e7d50…"}
```

Persist it across restarts with one extra arg:

```python
app = SHABD("kyc", secret="x" * 32,
            grimoire_log_path="/var/lib/shabd/audit.jsonl")
```

See [grimoire.md](grimoire.md) for the cryptographic details.

---

## Step 5 — Turn on auth (3 minutes)

```python
import os
app = SHABD("api",
            secret=os.environ["SHABD_SECRET"],   # 32+ random bytes
            require_auth=True)

@app.spell(scopes=["admin"])
def dangerous_op() -> dict:
    return {"ok": True}

# Issue a token
token = app.issue_token("alice", scopes=["admin"], ttl=3600)
print("Bearer:", token)
```

Calls now need `Authorization: Bearer <token>`. See
[security.md](security.md) for scopes, rate limits, the circuit breaker,
and zero-downtime secret rotation.

---

## Step 6 — Banking-grade safe retries (3 minutes)

Send `Idempotency-Key` on writes — SHABD stores the response for 24 h
and replays it on repeat requests:

```python
from shabd_client import SHABDClient
import uuid

client = SHABDClient("http://localhost:8765", token=TOKEN)
ide = f"transfer-{uuid.uuid4()}"

# First call: executes
r1 = client.cast("transfer", {...}, idempotency_key=ide)

# Network retry: returns the *same* response, no double-charge
r2 = client.cast("transfer", {...}, idempotency_key=ide)
assert r1 == r2

# Same key + different body raises a structured error
```

See `examples/bank_transfer.py` for a complete worked example.

---

## Step 7 — Observability (5 minutes)

Three pillars + an audit chain, all on by default:

* **Metrics** — `GET /metrics?format=prom` → Prometheus exposition
* **Logs** — JSON to stderr; every line tagged with `trace_id` + `spell`
* **Traces** — W3C `traceparent` parsed on input, propagated on output
* **Audit chain** — `GET /grimoire/verify`

```bash
curl -s 'http://localhost:8765/metrics?format=prom' | head
```

The Prometheus + Grafana stack is wired in `docker-compose.yml`:

```bash
docker compose up -d
# Grafana at http://localhost:3000 (admin/admin)
```

See [observability.md](observability.md) for sample PromQL panels.

---

## Step 8 — Build an agent in 5 lines (5 minutes)

```python
from shabd_client import SHABDClient

c = SHABDClient("http://localhost:8765", token=TOKEN)
tools = c.tools_for_openai()                # or .tools_for_anthropic()
result = c.cast("transfer", body, idempotency_key=ide)
```

The client auto-handles auth, trace propagation, retries, structured
errors, and `Idempotency-Key`. To dispatch a model's `tool_calls` in one
shot:

```python
messages.extend(c.dispatch_openai_tool_calls(resp.tool_calls))
```

See [agent-sdk.md](agent-sdk.md) for the full surface, plus a real
agent loop with OpenAI and Anthropic.

---

## Step 9 — Ship to production (10 minutes)

Three deployment artifacts ship in the repo:

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

The manifest includes startup / liveness / readiness probes, a
`PodDisruptionBudget`, `terminationGracePeriodSeconds`, and a
`runAsNonRoot` security context.

### systemd

```bash
sudo cp deploy/shabd.service /etc/systemd/system/
echo "SHABD_SECRET=$(openssl rand -hex 32)" | sudo tee /etc/shabd/env
sudo chmod 600 /etc/shabd/env
sudo systemctl daemon-reload && sudo systemctl enable --now shabd
```

See [production-deployment.md](production-deployment.md) for the rest:
secret rotation, audit-chain persistence, SIEM streaming, graceful
shutdown.

---

## Step 10 — Operate it (5 minutes)

`docs/runbook.md` is a single page you can hand to an on-call engineer.
Each common symptom (audit chain broken, latency spike, idempotency
conflict, disk full, etc.) maps to a SHABD-specific signal and a
concrete next step.

---

## Quick links — when you're stuck

| Question                         | Doc                                  |
|----------------------------------|--------------------------------------|
| How do I install / first server  | [getting-started.md](getting-started.md) |
| What semantic types ship         | [semantic-types.md](semantic-types.md) |
| How does the audit chain work    | [grimoire.md](grimoire.md)           |
| How are errors structured        | [ai-native-errors.md](ai-native-errors.md) |
| Auth, scopes, rate limit         | [security.md](security.md)           |
| Every HTTP endpoint              | [http-api.md](http-api.md)           |
| Claude Desktop / Ollama          | [mcp-integration.md](mcp-integration.md) |
| When to pick which framework     | [vs-fastmcp.md](vs-fastmcp.md)       |
| Production deployment            | [production-deployment.md](production-deployment.md) |
| Metrics + logs + traces          | [observability.md](observability.md) |
| Build an agent                   | [agent-sdk.md](agent-sdk.md)         |
| On-call runbook                  | [runbook.md](runbook.md)             |
