# FastAPI front-end (optional)

The default SHABD UI is pure standard library — zero dependencies, the
right choice for restricted / air-gapped networks. On the public
internet you may want FastAPI's async speed, automatic Swagger UI, and
ecosystem. `shabd_fastapi.py` gives you that **without changing
anything else**.

## Does it affect my existing project, concept or accuracy?

No.

* **Concept (single-file, zero-dependency) is intact.** The stdlib UI
  (`python -m shabd_ui`) keeps working exactly as before with no extra
  packages. FastAPI is strictly opt-in.
* **Accuracy is identical.** Every call still goes through the same
  `app.invoke(...)` pipeline — same schema validation, same scope
  checks, same idempotency, same Grimoire audit chain. FastAPI is only
  a different *transport* in front of the same logic.
* **The UI is untouched.** No UI page, route, or behaviour changed.
* **Spells you build in the UI appear in FastAPI live.** The routes are
  dynamic catch-alls, so a spell created at `/builder` is immediately
  callable at `POST /spells/<name>` on the FastAPI port — no restart.

If FastAPI isn't installed, importing the module gives a clear message
and the rest of SHABD is unaffected.

## Install

```bash
pip install "shabd[fastapi]"
# or
pip install fastapi uvicorn
```

## Run it alongside the UI (recommended)

One command runs both — they share the same app, so live state matches:

```bash
python -m shabd_ui --port 8080 --fastapi-port 8090
```

* Stdlib UI:        `http://localhost:8080/`
* FastAPI + Swagger: `http://localhost:8090/docs`
* ReDoc:            `http://localhost:8090/redoc`
* OpenAPI:          `http://localhost:8090/openapi.json`

Build a spell in the UI's Spell Builder, then refresh Swagger — it's
there.

## Run it standalone

```python
from shabd import SHABD
from shabd_fastapi import build_fastapi
import uvicorn

app = SHABD("mine", secret="...")

@app.spell
def add(a: int, b: int) -> int:
    return a + b

api = build_fastapi(app)          # pass a UIServer too for /query + /ask
uvicorn.run(api, host="0.0.0.0", port=8090)
```

Or via the module launcher:

```bash
python -m shabd_fastapi --spells my_spells.py --port 8090
```

## Endpoints

Same shape as the stdlib server:

| Method | Path | What |
|---|---|---|
| GET  | `/healthz` | liveness |
| GET  | `/manifest` | all tools (MCP / OpenAI format) |
| GET  | `/spells` | list spells with schemas |
| POST | `/spells/{name}` | invoke a spell (Bearer token if scoped) |
| GET  | `/grimoire/verify` | verify the audit chain |
| GET  | `/grimoire/head` | latest chain head |
| GET  | `/agents` | list saved agents *(if a UIServer was passed)* |
| POST | `/query/{agent}` | ask a specific agent |
| POST | `/ask` | orchestrator routes to the right agent |
| GET  | `/docs`, `/redoc`, `/openapi.json` | FastAPI auto-docs |

Example:

```bash
# open spell
curl -X POST http://localhost:8090/spells/add \
     -H "Content-Type: application/json" \
     -d '{"a":7,"b":35}'
# {"result":42}

# scoped spell — needs a token with the scope
curl -X POST http://localhost:8090/spells/pay \
     -H "Authorization: Bearer <TOKEN>" \
     -d '{"amount":100}'

# ask the orchestrator
curl -X POST http://localhost:8090/ask \
     -H "Content-Type: application/json" \
     -d '{"question":"add two numbers"}'
```

## When to use which

| Situation | Use |
|---|---|
| Restricted / air-gapped network | stdlib UI only (`python -m shabd_ui`) |
| Need Swagger UI / Postman import | add `--fastapi-port` |
| High traffic / many concurrent calls | FastAPI port (async) |
| Internal demo / pilot, low traffic | either; stdlib is fine |

Both servers can run at once from the single `python -m shabd_ui
--fastapi-port` command, sharing the same spells, agents and audit
chain.

## Notes

* Handlers are deliberately synchronous (`def`). FastAPI runs them in a
  worker threadpool, so SHABD's `app.invoke()` behaves exactly as in
  the stdlib server — including the agent loop's nested tool calls.
* The same secret signs tokens for both servers, so a token minted in
  the UI's *Issue Token* page works against the FastAPI port too.
* For production, put a reverse proxy (nginx / Caddy) with TLS in front
  and run uvicorn with multiple workers behind it.
