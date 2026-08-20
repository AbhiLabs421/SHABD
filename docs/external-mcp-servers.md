# Proxying External MCP Servers (`.NET`, Java, Node, …)

The Model Context Protocol is language-agnostic. A bank's existing
.NET MCP service, a Java team's tool server, a Node.js MCP plugin —
all can be **proxied through SHABD in one screenful** so your AI
agents only ever talk to your audited, policy-enforced façade.

This chapter walks through the canonical pattern: external HTTP MCP
server with a Bearer token (e.g. `http://172.19.18.204:9036/mcp`).

---

## 1. The 12-line bridge

```python
from shabd import MCPClient, SHABD

app = SHABD("dotnet-bridge", secret=os.environ["SHABD_SECRET"],
            grimoire_log_path="/var/lib/shabd/dotnet-audit.jsonl")

upstream = MCPClient(
    name="dotnet",
    transport="http",
    url="http://172.19.18.204:9036/mcp",
    auth_token=os.environ["DOTNET_MCP_TOKEN"],   # Bearer prefix is added if missing
    prefix=True,                                  # tools become 'dotnet__*'
    timeout=15.0,
)
upstream.connect()
upstream.register_on(app)

app.serve(port=8765)
```

That is the whole bridge. Every tool the .NET server advertised is now
an `app.spell` on your SHABD app, **with all of SHABD's enforcement
layered on top**:

| Layer | What it does to the external call |
|-------|-----------------------------------|
| Scopes / RBAC | "Only the `dealer` role can call `dotnet__book_repo`." |
| `Idempotency-Key` | The .NET server is called *once* even when the agent retries. |
| `max_concurrent` | Caps concurrent calls into the .NET server so you don't DDoS it. |
| Semantic types | Aadhaar / GSTIN / Money are validated *before* the upstream is called. |
| Grimoire | Every call appends a tamper-evident page on YOUR side. |
| OTLP / Prometheus | Latency and error rate of the .NET server, in your existing Grafana. |
| Audit webhook | Stream every interaction to your SIEM. |

The .NET team doesn't have to change anything. You add governance from
the outside.

---

## 2. From a Python agent

```python
from shabd_client import SHABDClient

c = SHABDClient("http://localhost:8765",
                token=os.environ["SHABD_TOKEN"])

# 1) Pull the manifest (advertised by SHABD; mirrors the .NET surface)
tools = c.tools_for_openai()

# 2) Hand `tools=tools` to OpenAI / Anthropic / Ollama / your LLM
resp = openai.chat.completions.create(model="gpt-4o",
                                      messages=[...],
                                      tools=tools,
                                      tool_choice="auto")

# 3) Route every tool call back through SHABD
messages.extend(c.dispatch_openai_tool_calls(resp.choices[0].message.tool_calls))
```

The agent never knows that the underlying tool runs on .NET. It sees
SHABD's clean surface, with `did_you_mean` errors, idempotency, and
the audit chain.

---

## 3. Worked example

`examples/proxy_dotnet_mcp.py` is the full file. Run it:

```bash
DOTNET_MCP_URL=http://172.19.18.204:9036/mcp \
DOTNET_MCP_TOKEN=$(cat /etc/secrets/dotnet-token) \
python examples/proxy_dotnet_mcp.py

# In another shell:
python examples/proxy_dotnet_mcp.py --client
```

If the upstream is unreachable the bridge still comes up with zero
spells, so you can develop against a stub locally and let ops fix the
network on their side.

---

## 4. What happens on every call

```
[ Agent / LLM ]
      │ tool_call("dotnet__book_repo", {...})
      ▼
[ SHABDClient ]                            Bearer (your SHABD token)
      │ POST /spells/dotnet__book_repo     traceparent: 00-...-01
      │                                    Idempotency-Key: <uuid>
      ▼
[ SHABD proxy app ]
      │  1. parse auth + traceparent
      │  2. RBAC: caller has 'dealer' role?
      │  3. Idempotency cache: already seen?  -> return cached
      │  4. Semaphore: under max_concurrent?
      │  5. Semantic types: arguments validate?
      │  6. spell body --> MCPClient.call_tool("book_repo", ...)
      ▼
[ MCPClient HTTP ]                         Authorization: Bearer <DOTNET_MCP_TOKEN>
      │ JSON-RPC over POST                 Content-Type: application/json
      ▼
[ .NET MCP server ]
      │ runs the actual tool
      ▲
      │ result
      │
[ SHABD ]
      │  7. spell returns
      │  8. Grimoire append (signed + chained, redacted PII)
      │  9. SQLite / JSONL persist
      │ 10. OTLP span exported
      │ 11. audit webhook fired (SIEM)
      ▼
[ Agent / LLM ]  gets the result
```

Every numbered step is in the source. Read [feature-map.md](feature-map.md)
for the exact file and class.

---

## 5. Configuration cheat sheet

| Concern | Where |
|---|---|
| Change Bearer token | `MCPClient(auth_token=...)` (Bearer prefix added if missing) |
| Custom CA / mTLS to upstream | Set Python's `SSL_CERT_FILE` env or wrap `MCPClient._http_rpc` |
| Tool name conflicts | `MCPClient(prefix=True)` adds a namespace; `False` keeps the raw names |
| Per-tool RBAC | Add `RBACPolicyEngine` rules against `dotnet__<toolname>` |
| Per-tool concurrency | Use `app.spell(max_concurrent=N)` if you re-decorate (advanced) |
| Per-tool idempotency | Send `Idempotency-Key` on the client side; SHABD's cache absorbs retries |

---

## 6. Common pitfalls

| Symptom | Fix |
|---|---|
| `urlopen error` connection refused | URL wrong, or network policy blocks the IP |
| `401 Unauthorized` | Bearer token wrong or expired |
| `404 Not Found` | URL path is `…/mcp`, not the server root |
| `JSON decode` errors | Upstream isn't speaking MCP JSON-RPC; verify with curl first |
| Hangs at startup | Increase `MCPClient(timeout=…)`; some .NET servers are slow to initialize |
| Tools appear with weird names | Set `prefix=False` *only* if you trust no upstream name clashes |
