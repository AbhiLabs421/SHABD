# SHABD Documentation

Chapter-by-chapter guide. Start with **[the Step-by-Step Usage Book](usage-book.md)** if
you've never used SHABD before — it walks you from zero to production in
10 numbered steps.

| # | Chapter | What you'll learn |
|---|---|---|
| 0 | [Step-by-Step Usage Book](usage-book.md) | Install → first spell → production, in 10 numbered steps |
| 1 | [Getting Started](getting-started.md) | Install, write your first spell, run the server |
| 2 | [Semantic Types](semantic-types.md) | `Email`, `Aadhaar`, `GSTIN`, `IndianPhone`, `Money`, `URL` |
| 3 | [Grimoire — Audit Log](grimoire.md) | Hash-chained, HMAC-signed, tamper-evident audit chain |
| 4 | [AI-Native Errors](ai-native-errors.md) | `did_you_mean`, `hint`, `example` |
| 5 | [Security & Auth](security.md) | HMAC tokens, scopes, rate limiting, secret rotation |
| 6 | [HTTP API Reference](http-api.md) | Every endpoint, every payload |
| 7 | [MCP Integration](mcp-integration.md) | Claude Desktop, Ollama, cross-language |
| 8 | [SHABD vs FastMCP](vs-fastmcp.md) | Honest comparison — when to use which |
| 9 | [Production Deployment](production-deployment.md) | Docker, K8s, systemd, persistence, rotation, drain |
| 10 | [Observability](observability.md) | Prometheus metrics, structured logs, W3C tracing |
| 11 | [Agent SDK](agent-sdk.md) | Build an agent in 5 lines with `SHABDClient` |
| 12 | [Operations Runbook](runbook.md) | On-call symptom → signal → fix |
| 13 | [Enterprise Features](enterprise-features.md) | HSM, RBAC, SoD, mTLS, SQLite chain, clustering, OTLP |
| 14 | [Business Packs](business-packs.md) | Sanctions, RegTech, Pre-trade, CCIL — revenue-shaped imports |
| 15 | [Feature Map](feature-map.md) | What runs when — one-page mental model |
| 16 | [Flowise Integration](flowise-integration.md) | Drag-and-drop LLM agents on top of SHABD |
| 17 | [External MCP Servers](external-mcp-servers.md) | Proxy a .NET / Java / Node MCP server through SHABD with Bearer auth |
| 18 | [Why not beat OpenAI / Anthropic](why-not-beat-openai.md) | Honest market read on where SHABD can and can't win |
| 19 | [Production UI](ui.md) | No-code web dashboard with Keycloak SSO, RBAC, audit |

Operator utilities (no docs page, but worth knowing):

| Tool | Purpose |
|------|---------|
| [`scripts/verify_audit.py`](../scripts/verify_audit.py) | Standalone verifier for an on-disk Grimoire JSONL file (auditor's tool) |
| [`scripts/rotate_secret.py`](../scripts/rotate_secret.py) | Generate a fresh secret for zero-downtime rotation |
| [`scripts/dump_grimoire.py`](../scripts/dump_grimoire.py) | Pretty-print audit pages |
| [`scripts/smoke.sh`](../scripts/smoke.sh) | End-to-end smoke test for a deployment gate |
| [`bench/run.py`](../bench/run.py) | Local throughput / latency benchmark |

There is also a legacy [`SHABD_Usage_Guide.pdf`](SHABD_Usage_Guide.pdf) which
covers up to v2.0; for v2.1+ features (Grimoire, semantic types, AI-native
errors, idempotency, observability, persistence, deployment) use the
markdown chapters above — they are the source of truth.
