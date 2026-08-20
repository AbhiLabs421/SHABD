"""
shabd_fastapi.py — OPTIONAL FastAPI front-end for a SHABD app.

Why this exists
===============

The default SHABD UI (`shabd_ui.py`) is pure stdlib, zero-dependency —
the right choice for restricted / air-gapped networks. But on the public
internet you may want FastAPI's async speed, automatic Swagger UI, and
ecosystem.

This module gives you that WITHOUT changing anything else:

  * It reuses the SAME `SHABD` app — same spells, same Grimoire audit,
    same validation, same idempotency. The accuracy and behaviour are
    identical, because every call still goes through `app.invoke(...)`.
  * Routes are DYNAMIC (catch-all), so spells you create from the UI's
    Spell Builder appear in the FastAPI server instantly — no restart,
    no re-registration.
  * If you pass a `UIServer`, the agent endpoints (`/query/<agent>`,
    `/ask`) light up too, sharing the UI's saved agents and intents.
  * If FastAPI isn't installed, importing this module raises a clear
    message — the rest of SHABD keeps working untouched.

Two ways to run
===============

1. Alongside the UI (recommended — they share live state):

       python -m shabd_ui --fastapi-port 8090

   Now the stdlib UI is on :8080 and FastAPI + Swagger on :8090, both
   backed by the same app. Build a spell in the UI → it's in Swagger.

2. Standalone:

       from shabd import SHABD
       from shabd_fastapi import build_fastapi
       import uvicorn

       app = SHABD("mine", secret="...")
       @app.spell
       def add(a: int, b: int) -> int: return a + b

       api = build_fastapi(app)
       uvicorn.run(api, host="0.0.0.0", port=8090)

   Swagger UI:  http://localhost:8090/docs
   ReDoc:       http://localhost:8090/redoc
   OpenAPI:     http://localhost:8090/openapi.json   (FastAPI built-in)
"""
from __future__ import annotations

import typing as t

try:
    from fastapi import Body, FastAPI, Header, HTTPException, Request
    from fastapi.responses import JSONResponse
    _HAVE_FASTAPI = True
except ImportError:  # pragma: no cover - exercised only without fastapi
    _HAVE_FASTAPI = False


__all__ = ["build_fastapi", "have_fastapi", "run"]


def have_fastapi() -> bool:
    return _HAVE_FASTAPI


def _require_fastapi() -> None:
    if not _HAVE_FASTAPI:
        raise RuntimeError(
            "FastAPI is not installed. Install the optional extra:\n"
            "    pip install fastapi uvicorn\n"
            "The stdlib UI (python -m shabd_ui) works without it.")


def build_fastapi(app: t.Any, ui: t.Any = None) -> t.Any:
    """Return a FastAPI application that exposes `app`'s spells (and, if
    `ui` is given, its agents + orchestrator) as HTTP endpoints.

    The same SHABD pipeline runs under every call, so results match the
    stdlib server exactly."""
    _require_fastapi()

    api = FastAPI(
        title=f"SHABD — {getattr(app, 'name', 'app')}",
        version="1.0.0",
        description=(
            "FastAPI front-end over a SHABD app. Same spells, same "
            "Grimoire audit, same validation as the stdlib server. "
            "Spells you add from the UI appear here live."),
    )

    def _token_from(authorization: str | None) -> str | None:
        if not authorization:
            return None
        return authorization.removeprefix("Bearer ").strip() or None

    # -- core ---------------------------------------------------------

    @api.get("/healthz", tags=["core"], summary="Liveness")
    def healthz():
        return {"ok": True}

    @api.get("/manifest", tags=["core"],
             summary="All tools (MCP / OpenAI format)")
    def manifest():
        return app.manifest()

    @api.get("/grimoire/verify", tags=["core"],
             summary="Verify the audit chain")
    def grimoire_verify():
        return app.grimoire.verify()

    @api.get("/grimoire/head", tags=["core"],
             summary="Latest audit chain head")
    def grimoire_head():
        return {"head": app.grimoire.head()}

    @api.get("/spells", tags=["spells"],
             summary="List registered spells")
    def list_spells():
        return {"spells": [
            {"name": n, "description": s.description,
             "scopes": list(s.scopes or []),
             "schema": s.schema}
            for n, s in app._spells.items()
        ]}

    # -- dynamic spell invoke (catch-all, so UI-built spells work) ----

    # NOTE: handlers are SYNC (`def`, not `async def`) on purpose.
    # FastAPI runs sync routes in a worker threadpool, so SHABD's
    # `app.invoke()` (which internally calls asyncio.run) works exactly
    # as it does in the stdlib server — including deep inside agent
    # tool calls. This keeps behaviour/accuracy identical.
    @api.post("/spells/{name}", tags=["spells"],
              summary="Invoke a spell (token-authenticated)")
    def invoke_spell(
            name: str,
            body: dict = Body(default={}),
            authorization: str | None = Header(default=None)):
        if name not in app._spells:
            raise HTTPException(
                status_code=404,
                detail={"code": "spell_not_found",
                        "message": f"no such spell: {name}"})
        token = _token_from(authorization)
        try:
            result = app.invoke(name, body or {}, token=token)
            return {"result": result}
        except Exception as e:  # map SHABD errors to HTTP codes
            raise _to_http(e) from None

    # -- agent + orchestrator endpoints (only if a UI is supplied) ----

    if ui is not None:

        @api.get("/agents", tags=["agents"],
                 summary="List saved agents")
        def list_agents():
            return {"agents": list(ui._agents.keys())}

        @api.post("/query/{agent}", tags=["agents"],
                  summary="Ask a specific agent")
        def query_agent(
                agent: str,
                body: dict = Body(...),
                authorization: str | None = Header(default=None)):
            subject = _subject(ui, app, authorization)
            try:
                return ui.ask_agent(
                    agent_name=agent,
                    question=(body.get("question")
                              or body.get("query") or ""),
                    subject=subject)
            except Exception as e:
                raise _to_http(e) from None

        @api.post("/ask", tags=["agents"],
                  summary="Ask — orchestrator routes to the right agent")
        def ask(
                body: dict = Body(...),
                authorization: str | None = Header(default=None)):
            subject = _subject(ui, app, authorization)
            try:
                return ui.ask_orchestrator(
                    question=(body.get("question")
                              or body.get("query") or ""),
                    subject=subject)
            except Exception as e:
                raise _to_http(e) from None

    @api.exception_handler(HTTPException)
    async def _eh(request: Request, exc: HTTPException):
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": exc.detail}
            if isinstance(exc.detail, dict)
            else {"error": {"message": exc.detail}})

    return api


def _subject(ui: t.Any, app: t.Any,
             authorization: str | None) -> str:
    if not authorization:
        return "api"
    token = authorization.removeprefix("Bearer ").strip()
    try:
        payload = app.tokens.verify(token)
        return payload.get("sub", "api")
    except Exception:
        raise HTTPException(
            status_code=401,
            detail={"code": "unauthorized",
                    "message": "invalid token"}) from None


def _to_http(e: Exception):
    """Map a SHABD exception to a FastAPI HTTPException with the right
    status code and the structured error envelope SHABD already
    produces."""
    name = type(e).__name__
    status = 500
    if name == "AuthError":
        status = 401
    elif name == "ForbiddenError":
        status = 403
    elif name == "SpellNotFoundError":
        status = 404
    elif name in ("ValidationError", "SchemaError"):
        status = 400
    detail = {"code": name, "message": str(e)}
    hint = getattr(e, "hint", None)
    if hint:
        detail["hint"] = hint
    if isinstance(e, HTTPException):
        return e
    return HTTPException(status_code=status, detail=detail)


def run(app: t.Any, ui: t.Any = None, *,
        host: str = "0.0.0.0", port: int = 8090) -> None:
    """Convenience: build + serve with uvicorn (blocking)."""
    _require_fastapi()
    import uvicorn
    api = build_fastapi(app, ui)
    uvicorn.run(api, host=host, port=port)


# ---------------------------------------------------------------------------
# `python -m shabd_fastapi` — standalone launcher (loads a spells file)
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    import argparse
    import os

    ap = argparse.ArgumentParser(
        prog="python -m shabd_fastapi",
        description="Serve a SHABD app via FastAPI + Swagger.")
    ap.add_argument("--spells", default="my_spells.py",
                    help="optional spells file to import")
    ap.add_argument("--audit", default="shabd-audit.jsonl",
                    help="Grimoire JSONL path")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8090)
    args = ap.parse_args(argv)

    if not _HAVE_FASTAPI:
        print("FastAPI not installed. Run:  pip install fastapi uvicorn")
        return 1

    from shabd import SHABD
    app = SHABD("shabd-fastapi",
                secret=os.environ.get("SHABD_SECRET", "x" * 32),
                require_auth=False,
                grimoire_log_path=args.audit)

    # Reuse the UI launcher's spell loader for parity.
    try:
        from shabd_ui_cli import _load_spells
        loaded = _load_spells(app, args.spells)
    except Exception:
        loaded = 0

    print()
    print("=" * 60)
    print("  SHABD · FastAPI front-end")
    print("=" * 60)
    print(f"  Spells   : {loaded or len(app._spells)}")
    print(f"  Swagger  : http://{args.host}:{args.port}/docs")
    print(f"  ReDoc    : http://{args.host}:{args.port}/redoc")
    print(f"  OpenAPI  : http://{args.host}:{args.port}/openapi.json")
    print("=" * 60)
    print()
    run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
