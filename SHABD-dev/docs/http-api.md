# HTTP API Reference

When you call `app.serve()`, SHABD exposes the following endpoints.

## Identity & introspection

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Server identity, spell counts, endpoint list |
| `GET` | `/health` | Liveness probe |
| `GET` | `/manifest` | All spells, resources, and prompts as JSON |
| `GET` | `/openapi.json` | OpenAPI 3.1 spec auto-generated from spells |
| `GET` | `/dashboard` | HTML Playground (browse & call spells in the browser) |
| `GET` | `/metrics` | Counters and latency percentiles |

## Calling spells

| Method | Path | Description |
|---|---|---|
| `POST` | `/spells/{name}` | Invoke a spell; body is the args JSON |
| `POST` | `/stream/{name}` | Server-sent events from a streaming spell |
| `POST` | `/replay/{trace_id}` | Re-run a past call by trace id |

### Request shape

```http
POST /spells/add
Content-Type: application/json
Authorization: Bearer <token>          # if require_auth=True

{"a": 5, "b": 7}
```

### Response shape

```json
{"ok": true, "result": 12, "trace_id": "95f8bccd714…"}
```

### Error shape (with AI-native fields)

```json
{
  "error": {
    "code": "spell_not_found",
    "message": "no such spell: adddd",
    "hint": "Did you mean 'add'?",
    "did_you_mean": ["add"]
  }
}
```

## Resources & prompts

| Method | Path | Description |
|---|---|---|
| `GET` | `/resources` | List registered resources |
| `GET` | `/resources?uri=/docs/foo` | Read a single resource |
| `GET` | `/prompts` | List registered prompts |
| `POST` | `/prompts/{name}` | Render a prompt with arguments |

## Grimoire — audit chain

| Method | Path | Description |
|---|---|---|
| `GET` | `/grimoire/verify` | Verify the chain end-to-end |
| `GET` | `/grimoire/head` | Current head hash + page count |
| `GET` | `/grimoire/pages?since=0&limit=100` | Dump audit pages |

See [Grimoire](grimoire.md) for the semantics.

## Operations

| Method | Path | Description |
|---|---|---|
| `GET` | `/calls?n=100` | Recent in-memory call log (last N) |
| `GET` | `/cpm-config` | Generate config YAML for the CPM framework |

## WebSocket

`ws://host:port/ws` — bidirectional JSON-RPC for the same spell surface.

## MCP transports

* `app.mcp_stdio()` — stdio JSON-RPC for Claude Desktop, etc.
* `app.serve(...)` — HTTP/SSE/WebSocket for everything else.

## Status codes

| Code | Meaning |
|---|---|
| `200` | Spell succeeded |
| `400` | Validation failed |
| `401` | Missing / invalid token (`AuthError`) |
| `403` | Token valid but missing required scope (`ForbiddenError`) |
| `404` | No such spell / resource / prompt |
| `429` | Rate limit exceeded (`Retry-After` header included) |
| `500` | Spell raised, or circuit open |
