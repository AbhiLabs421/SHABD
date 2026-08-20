# Observability

The three pillars of observability — metrics, logs, traces — all wired
in. Plus an audit chain you can verify cryptographically.

## Metrics

`/metrics` serves either JSON (default for browsers / curl) or Prometheus
exposition format (when `Accept: text/plain` or `?format=prom`):

```
shabd_calls_total{spell="transfer",status="ok"} 142
shabd_calls_total{spell="transfer",status="error"} 3
shabd_calls_total{spell="transfer",status="cache_hit"} 18
shabd_call_duration_ms{spell="transfer",quantile="0.5"} 12.4
shabd_call_duration_ms{spell="transfer",quantile="0.95"} 48.1
shabd_call_duration_ms{spell="transfer",quantile="0.99"} 91.7
shabd_call_duration_ms_count{spell="transfer"} 145
```

### Scrape config

```yaml
scrape_configs:
  - job_name: shabd
    metrics_path: /metrics
    params: { format: [prom] }
    static_configs:
      - targets: ["shabd:8765"]
```

### Grafana

Suggested PromQL panels:

```promql
# RPS by spell
sum(rate(shabd_calls_total[1m])) by (spell)

# Error rate by spell
sum(rate(shabd_calls_total{status="error"}[5m])) by (spell)
  /
sum(rate(shabd_calls_total[5m])) by (spell)

# p95 latency
shabd_call_duration_ms{quantile="0.95"}

# Cache hit ratio
sum(rate(shabd_calls_total{status="cache_hit"}[5m])) by (spell)
  /
sum(rate(shabd_calls_total[5m])) by (spell)
```

## Logs

Structured JSON on stderr. Sample line:

```json
{"ts": 1780504219.99, "level": "INFO", "msg": "registered spell",
 "logger": "shabd", "spell": "transfer"}
```

Stitching keys you'll see:

| Field        | What it identifies                    |
|--------------|---------------------------------------|
| `trace_id`   | W3C trace_id (32-hex) — distributed   |
| `spell`      | Spell name                            |
| `subject`    | Authenticated subject from the token  |

Set the log level:

```bash
SHABD_LOG_LEVEL=DEBUG python server.py
```

## Distributed tracing — W3C TraceContext

SHABD parses incoming `traceparent` headers and uses them as the parent
of the current span. The same trace_id flows through:

* the structured JSON log lines
* the Grimoire audit page
* the response body (`trace_id` field)
* outgoing calls made by `SHABDClient` (which propagates `traceparent`
  automatically)

So a single regulator can pull one ID and see the whole story —
logs + traces + audit chain.

### Anatomy of a traceparent

```
00 - 0af7651916cd43dd8448eb211c80319c - b7ad6b7169203331 - 01
 │                  │                         │              │
 │                  │                         │              └── flags (sampled)
 │                  │                         └── parent span id (16-hex)
 │                  └── trace id (32-hex)
 └── version
```

### Using TraceContextCodec directly

```python
from shabd import TraceContextCodec

# Decode an inbound header
trace_id, parent_span, flags = TraceContextCodec.decode(headers.get("traceparent"))

# Encode for an outbound call
headers["traceparent"] = ctx.traceparent()   # uses the current Context
```

## Audit chain — the fourth pillar

Metrics, logs, and traces are all **observation**. The Grimoire chain is
**proof**. See [grimoire.md](grimoire.md) for the mechanics.

## End-to-end correlation

Every spell call gets:

| Identifier         | Where it appears                            |
|--------------------|---------------------------------------------|
| W3C `trace_id`     | logs, response body, Grimoire page, traceparent header |
| W3C `span_id`      | logs, outgoing traceparent                  |
| `Idempotency-Key`  | request header only (the spell never sees it directly) |
| Grimoire page hash | `/grimoire/pages` and the on-disk audit file |

You can correlate a customer complaint, a metric spike, a log line, a
trace, and an audit page using a single ID without leaving Grafana.
