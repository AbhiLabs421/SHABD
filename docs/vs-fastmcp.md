# SHABD vs FastMCP — Honest Comparison

[FastMCP](https://github.com/jlowin/fastmcp) is the mature, widely used,
ecosystem-leading MCP server library. It is closely aligned with the
official MCP project. **Anyone telling you FastMCP lacks auth, caching,
or rate limiting is wrong** — these all ship in `fastmcp.server.middleware`
in FastMCP 3.x. The comparison below is honest about that.

The matrix below is verified at runtime by `tests/test_comparison.py`.

## Feature matrix

| Feature | SHABD | FastMCP |
|---|---|---|
| MCP Tools / Resources / Prompts | ✅ | ✅ |
| Built-in auth + scopes | ✅ HMAC tokens | ✅ OAuth 2.1 + Bearer |
| Built-in rate limiting | ✅ | ✅ middleware |
| Built-in caching | ✅ TTL | ✅ middleware |
| Streamable HTTP / SSE | ✅ | ✅ |
| Maturity & community | 🟡 New (2026) | ✅ Large, multi-year |
| Official MCP alignment | Compatible, independent | ✅ Closely aligned |
| Runtime dependencies | **0** | many (pydantic, anyio, mcp, starlette, …) |
| Single-file distribution | ✅ `shabd.py` | ➖ package |
| Built-in dashboard / playground | ✅ `/dashboard` | ➖ |
| Built-in REST + OpenAPI | ✅ `/openapi.json` | via `from_openapi` / `from_fastapi` |
| Spell chains (`a \| b \| c`) | ✅ | ➖ |
| YAML-defined tools | ✅ | ➖ |
| Multi-project namespacing | ✅ `app.group()` | `import_server` / `mount` (different model) |
| **Hash-chained tamper-evident audit log** | ✅ Grimoire | ➖ |
| **Semantic types with PII auto-redaction** | ✅ | ➖ (pydantic + custom) |
| **AI-native errors** (`did_you_mean` / `hint` / `example`) | ✅ | ➖ |

## When to pick which

### Pick FastMCP if…

* You want a battle-tested MCP server with a large community.
* You're already deep in the Anthropic / official MCP ecosystem.
* You need OAuth 2.1 / OIDC.
* Spec-alignment with future MCP changes matters more than self-contained code.

### Pick SHABD if…

* You're shipping into a **regulated environment** — DPDPA, EU AI Act,
  RBI, HIPAA — and need cryptographic audit proofs ("Grimoire").
* You work with **Indian** PII / identifiers (Aadhaar, GSTIN, IndianPhone).
* Your security team requires **one auditable file** and zero runtime deps.
* You want the production extras (dashboard, REST/OpenAPI, audit chain)
  out of the box without assembling middleware.
* You want errors LLMs can self-correct from without a human.

### Pick *both*

Nothing stops you from running both. A useful pattern:

* FastMCP at the edge (OAuth + ecosystem alignment).
* SHABD behind it for the compliance, audit chain, and Indian semantic
  types.

## How to verify this matrix yourself

```bash
pip install fastmcp
python tests/test_comparison.py
```

The script registers identical tools on both servers, runs them, and
prints the matrix above with results computed at runtime — so the table
can't drift from reality.
