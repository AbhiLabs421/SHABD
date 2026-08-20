# 🔮 SHABD

## Spell Hub for Agentic Builders & Developers

**Turn any Python function into a spell that AI agents can cast — with built-in security, caching, rate limiting, an HTTP server, a live dashboard, and full MCP support. All in a single file. Zero dependencies.**

_New here? Start with the [Introduction](INTRODUCTION.md) to understand the idea and the "spell" metaphor, or with the [Step-by-Step Usage Book](docs/usage-book.md) to go from zero to production in ten numbered steps._

## 60-second quick start

```bash
git clone https://github.com/Kumar123ips/SHABD.git && cd SHABD
make install                    # ruff + mypy (SHABD itself has zero deps)
make test                       # 49/49 stdlib-only tests
make demo                       # http://localhost:8765/dashboard
```

A minimum server is four lines:

```python
from shabd import SHABD
app = SHABD("hello", secret="x" * 32, require_auth=False)
@app.spell
def add(a: int, b: int) -> int: return a + b
app.serve(port=8765)
```

For a real bank / trading / regulated production setup, jump to the
[Step-by-Step Usage Book](docs/usage-book.md) — it covers semantic
types, the Grimoire audit chain, `Idempotency-Key`, observability,
secret rotation, and `docker compose up`.



![Python](https://img.shields.io/badge/python-3.10+-blue.svg)
![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)
![Dependencies](https://img.shields.io/badge/dependencies-none-brightgreen.svg)
![MCP](https://img.shields.io/badge/MCP-compatible-purple.svg)
![Tests](https://img.shields.io/badge/tests-147%2F147%20passing-success.svg)

---

## What is SHABD?

SHABD lets you turn ordinary Python functions into tools that AI models (Claude, GPT, Ollama, local LLMs) can call. You write a normal function, add one decorator, and SHABD handles the rest — JSON schema generation, validation, authentication, the HTTP server, and the Model Context Protocol (MCP) wiring that connects to Claude Desktop and other clients.

```python
from shabd import SHABD

app = SHABD("my-tools")

@app.spell
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b

app.serve(port=8765)   # HTTP server + live dashboard
# or: app.mcp_stdio()  # for Claude Desktop
```

That's it. Your `add` function is now callable over HTTP, over MCP, and visible in a live dashboard — with auto-generated schema and validation.

---

## Why SHABD?

Most tool frameworks give you the AI protocol layer and stop there. In production you also need authentication, rate limiting, caching, an HTTP API for non-Python clients, and observability. SHABD bundles all of that into **one file you can read in an afternoon** and drop into any project without installing anything.

- **Single file.** Copy `shabd.py` into your project. No package manager, no virtualenv juggling.
- **Zero dependencies.** Pure Python standard library. Optional plugins (Redis, YAML config) use extra packages only if you want them.
- **Batteries included.** HMAC tokens, scopes, rate limiting, TTL cache, circuit breaker, and structured logging are built in.
- **Five transports.** HTTP, Server-Sent Events, WebSocket, stdio, and full MCP stdio for Claude Desktop.
- **Live dashboard.** A built-in web UI with a Playground for testing tools, plus metrics and a call log.

---

## Installation

SHABD is a single file, so installation is just copying it:

```bash
# Option 1 — download directly
curl -O https://raw.githubusercontent.com/Kumar123ips/SHABD/main/shabd.py

# Option 2 — clone the repo
git clone https://github.com/Kumar123ips/SHABD.git
```

Then import it:

```python
from shabd import SHABD
```

Requires Python 3.10 or newer.

---

## Core Concepts

### Spells (Tools)

A "spell" is any Python function exposed to AI. Type hints become the JSON schema automatically.

```python
@app.spell(rate_limit=20, cache_ttl=300, tags=["read"])
async def search_docs(query: str, limit: int = 10) -> dict:
    """Search the documentation index."""
    return await db.search(query, limit)
```

### Resources

Expose files, database records, or API data as MCP Resources. These appear in Claude Desktop's Resources tab.

```python
@app.resource("/docs/{slug}", mime_type="text/markdown")
def get_doc(slug: str) -> str:
    """Return a documentation page."""
    return open(f"docs/{slug}.md").read()
```

### Prompts

Reusable prompt templates that appear in Claude Desktop's Prompts tab.

```python
@app.prompt("code_review")
def review_prompt(language: str = "python") -> str:
    """A reusable code-review prompt."""
    return f"Review this {language} code for bugs and security issues."
```

### Image and File returns

Return images that render inline, or files that download.

```python
from shabd import SpellImage, SpellFile

@app.spell
def make_chart(data: list) -> SpellImage:
    """Render a chart as a PNG."""
    buf = io.BytesIO()
    plt.savefig(buf, format="png")
    return SpellImage(data=buf.getvalue(), mime_type="image/png")
```

### Spell Chains

Connect tools into a pipeline with the pipe operator. The output of one step feeds the next.

```python
app.chain("search | summarize | translate", name="smart_search")
```

### Multi-project Groups

Namespace tools for multiple projects on one server.

```python
finance = app.group("finance")

@finance.spell
def calculate_gst(amount: float) -> dict:
    return {"gst": amount * 0.18}
# Registered as: finance__calculate_gst
```

### YAML Spells

Define REST-API-backed tools in a config file with no Python at all.

```yaml
yaml_spells:
  - name: get_weather
    url: "https://wttr.in/{city}?format=j1"
    method: GET
    params:
      city: {type: string, required: true}
```

---

## Using SHABD with Claude Desktop

Add SHABD to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "my-tools": {
      "command": "python",
      "args": ["/path/to/your_server.py", "--mcp"]
    }
  }
}
```

In your server file:

```python
import sys
if "--mcp" in sys.argv:
    app.mcp_stdio()    # Claude Desktop talks over stdio
else:
    app.serve(port=8765)
```

Claude Desktop will now see your tools, resources, and prompts.

---

## Using SHABD with Ollama (local LLMs)

SHABD works with any OpenAI-compatible endpoint, including Ollama. Fetch the tool schemas from `/manifest`, pass them to the model, and route tool calls back to SHABD.

```python
import requests

# 1. Get tool definitions from your running SHABD server
manifest = requests.get("http://localhost:8765/manifest").json()
tools = [
    {"type": "function", "function": {
        "name": s["name"],
        "description": s["description"],
        "parameters": s["input_schema"],
    }}
    for s in manifest["spells"]
]

# 2. Send them to Ollama; when the model requests a tool, call SHABD
#    POST http://localhost:8765/spells/{tool_name}
```

A complete runnable example is in [`examples/ollama_demo.py`](examples/ollama_demo.py).

---

## HTTP Endpoints

When you call `app.serve()`, these endpoints become available:

| Endpoint | Description |
|----------|-------------|
| `GET /health` | Liveness check |
| `GET /manifest` | All tools, resources, and prompts as JSON |
| `GET /openapi.json` | Auto-generated OpenAPI 3.1 spec |
| `GET /dashboard` | Live web dashboard with a Playground |
| `GET /metrics` | Latency percentiles and counters |
| `POST /spells/{name}` | Call a tool |
| `POST /stream/{name}` | Stream from a streaming tool (SSE) |
| `GET /resources` | List resources |
| `GET /prompts` | List prompts |
| `POST /replay/{trace_id}` | Re-run a past call |
| `GET /grimoire/verify` | Verify the integrity of the audit chain |
| `GET /grimoire/head` | Current head hash + page count |
| `GET /grimoire/pages?since=0&limit=100` | Dump audit pages |
| `GET /cpm-config` | Generate config for the CPM framework |

---

## Security

Authentication is built in. Issue signed HMAC tokens and protect tools with scopes.

```python
app = SHABD("my-tools", secret="your-secret", require_auth=True)

token = app.issue_token("alice", scopes=["read", "admin"], ttl=3600)

@app.spell(scopes=["admin"])
def delete_everything() -> dict:
    """Only callable with the 'admin' scope."""
    ...
```

For production, set the secret via environment variable:

```bash
export SHABD_SECRET="a-long-random-string-at-least-16-bytes"
```

---

## Cross-Language Interoperability

Because SHABD speaks MCP and standard HTTP, it interoperates with services written in other languages. A .NET MCP server and a Python SHABD server can talk to each other through:

1. **Claude Desktop** as a shared client for both.
2. **SHABD's MCP client**, which can proxy any external MCP server as local tools.
3. **Plain HTTP REST**, which any language can call.

See [`examples/dotnet_bridge/`](examples/dotnet_bridge/) for a worked .NET ↔ Python example.

---

## SHABD vs FastMCP — an honest comparison

[FastMCP](https://github.com/jlowin/fastmcp) is an excellent, mature, widely used library, closely aligned with the official MCP ecosystem. **FastMCP already ships built-in auth (OAuth + Bearer), rate limiting, caching, and middleware** — anyone telling you otherwise is wrong, and we won't repeat that mistake here. If a clean MCP-first server with a large community is what you want, FastMCP is the right call.

SHABD takes a different angle. We add a thin set of things on top of MCP that *no other framework ships today*, and we keep the whole thing in **one file with zero runtime dependencies** so you can audit and copy it anywhere.

Everything in this table is verified at runtime by `tests/test_comparison.py` (you can run it yourself).

| Feature | SHABD | FastMCP |
|---|---|---|
| MCP Tools / Resources / Prompts | ✅ | ✅ |
| Built-in auth + scopes | ✅ (HMAC tokens) | ✅ (OAuth + Bearer) |
| Built-in rate limiting | ✅ | ✅ (middleware) |
| Built-in caching | ✅ (TTL) | ✅ (middleware) |
| Streamable HTTP / SSE | ✅ | ✅ |
| Maturity & community | 🟡 New | ✅ Large |
| Runtime dependencies | **0** | many (pydantic, anyio, mcp, starlette, …) |
| Single-file distribution | ✅ `shabd.py` | ➖ package |
| Built-in dashboard / playground | ✅ `/dashboard` | ➖ |
| Built-in REST endpoints + OpenAPI | ✅ `/openapi.json` | via `from_openapi` / `from_fastapi` only |
| Spell chains (`a \| b \| c` pipelines) | ✅ | ➖ |
| YAML-defined tools | ✅ | ➖ |
| **Grimoire** — hash-chained, tamper-evident, signed audit log | ✅ | ➖ |
| **Semantic types** with PII auto-redaction (Email / Aadhaar / GSTIN / IndianPhone / Money / URL) | ✅ | ➖ (use pydantic + custom validators) |
| **AI-native errors** (`did_you_mean`, `hint`, `example`) | ✅ | ➖ |

**Pick FastMCP** for a battle-tested MCP server with a large community and the official MCP project's gravity behind it.

**Pick SHABD** when you want one auditable file, no runtime deps, and the production-shaped extras above — especially the audit chain and the PII-aware semantic types.

---

## The three things only SHABD has

### 🔮 Grimoire — a tamper-evident audit log

Every spell cast appends a **hash-chained, HMAC-signed page** to a Merkle-style log. Editing any past page breaks the whole chain — an external auditor can verify integrity in O(n) without seeing raw PII (because PII args are hashed in their *redacted* form).

```python
app.invoke("transfer", {"from_acct": "A", "to_acct": "B", "amount": 5000.0})
app.grimoire.verify()
# {"ok": True, "pages": 1, "head": "79615f2d…"}
```

HTTP endpoints: `GET /grimoire/verify`, `GET /grimoire/head`, `GET /grimoire/pages?since=0&limit=100`.

### 🧾 Semantic Types — strings that mean something

```python
from shabd import Email, Aadhaar, GSTIN, IndianPhone, Money, URL

@app.spell
def onboard(email: Email, aadhaar: Aadhaar, gstin: GSTIN, payment: Money) -> dict:
    ...
```

Each type validates at the boundary, surfaces its meaning in the JSON schema (`x-semantic`, `x-pii`, `pattern`, `example`), and PII-flagged fields are auto-redacted in the Grimoire log.

### 🤖 AI-native errors — built so LLMs can self-correct

Every error response carries a `hint`, `example`, and `did_you_mean` so the calling agent can fix itself without a human:

```json
{
  "error": {
    "code": "spell_not_found",
    "message": "no such spell: serach_docs",
    "hint": "Did you mean 'search_docs'?",
    "did_you_mean": ["search_docs"]
  }
}
```

---

## Examples

The [`examples/`](examples/) directory contains:

- [`quickstart.py`](examples/quickstart.py) — the smallest working server
- [`my_spells.py`](examples/my_spells.py) — every feature demonstrated
- [`grimoire_demo.py`](examples/grimoire_demo.py) — audit chain, tamper detection, PII-safe hashing
- [`semantic_types_demo.py`](examples/semantic_types_demo.py) — Email / Aadhaar / GSTIN / Money / URL
- [`ai_native_errors_demo.py`](examples/ai_native_errors_demo.py) — `did_you_mean`, `hint`, `example`
- [`ollama_demo.py`](examples/ollama_demo.py) — local LLM tool calling

---

## Testing

```bash
# Core (31 tests, stdlib only)
python tests/test_shabd.py

# Enterprise extensions (18 tests — idempotency, persistence, prom,
# traceparent, concurrency, drain, client SDK)
python tests/test_enterprise.py

# Side-by-side comparison with FastMCP (requires `pip install fastmcp`)
python tests/test_comparison.py
```

## Production deployment

SHABD v2.2 ships every code-level production primitive a bank or
trading desk expects:

* Prometheus `/metrics` (exposition format) + Grafana panels
* W3C `traceparent` propagation (in + out)
* Kubernetes-style `/healthz`, `/readyz`, `/startupz` probes
* `SIGTERM`-aware graceful shutdown with in-flight drain
* `Idempotency-Key` header for retry-safe writes
* Per-spell `max_concurrent` semaphores
* Zero-downtime secret rotation (`additional_secrets=[old_key]`)
* On-disk audit-chain persistence (`grimoire_log_path=...`)
* SIEM webhook streaming for the audit chain
* Single-file agent SDK (`shabd_client.py`)

Ready-to-go deployment artifacts: `Dockerfile`, `docker-compose.yml`
(with Prometheus + Grafana), `deploy/k8s.yaml`, `deploy/shabd.service`,
`.github/workflows/ci.yml`.

See [`docs/usage-book.md`](docs/usage-book.md) for the end-to-end
walkthrough and [`docs/production-deployment.md`](docs/production-deployment.md)
for the deployment details.

---

## Documentation

A full usage guide is in [`docs/SHABD_Usage_Guide.pdf`](docs/). It covers every feature with examples, the HTTP API, MCP integration, security, and cross-language interoperability.

---

## Contributing

Contributions are welcome. Please read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request.

---

## License

MIT — see [LICENSE](LICENSE). Use it for anything, no warranty.

---

## Authors

Developed by **Shanti** and **Abhishek**.

Questions or feedback: **ipsabhi423@gmail.com**

If SHABD helps you, please consider giving it a ⭐ on GitHub.
