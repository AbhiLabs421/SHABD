"""
spells_service — the SHABD engine.

Owns a SHABD app: registers spells, runs them, and writes every call to its
own Grimoire chain (shared/data/spells-audit.jsonl). Independent process.

Endpoints
---------
GET  /health
GET  /spells                 -> manifest of every spell (name, schema, tags)
POST /spells/{name}          -> run a spell, body = args JSON
GET  /grimoire/verify        -> chain integrity {ok, pages, head}
GET  /grimoire/head          -> current head hash
GET  /grimoire/pages         -> ?since=0&limit=100 audit pages
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))  # services/
from common import bootstrap  # noqa: E402

from fastapi import Body, FastAPI, HTTPException  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

from shabd import SHABD, ConfigLoader  # noqa: E402
import demo_spells  # noqa: E402

app_shabd = SHABD(
    "spells",
    secret=bootstrap.SECRET,
    require_auth=False,
    grimoire_log_path=bootstrap.audit_path("spells"),
)
demo_spells.register(app_shabd)  # add your own tools in shabd_core/demo_spells.py

_chains: list[dict] = []    # chains created at runtime
_sources: list[dict] = []   # external tool sources mounted at runtime


api = FastAPI(title="SHABD spells_service")
api.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)


@api.get("/health")
def health() -> dict:
    return {"service": "spells", "ok": True, "spells": len(app_shabd._spells)}


@api.get("/spells")
def list_spells() -> dict:
    return app_shabd.manifest()


@api.post("/spells/{name}")
async def invoke(name: str, args: dict = Body(default={})) -> dict:
    try:
        result = await app_shabd.invoke_async(name, args or {})
        return {"ok": True, "result": result}
    except Exception as e:  # AI-native errors carry hint/did_you_mean
        detail = {"error": type(e).__name__, "message": str(e)}
        for attr in ("hint", "did_you_mean", "example"):
            if hasattr(e, attr):
                detail[attr] = getattr(e, attr)
        raise HTTPException(status_code=400, detail=detail)


# ---- Spell Chains: a | b | c pipelines ----
@api.get("/chains")
def list_chains() -> dict:
    return {"chains": _chains}


@api.post("/chains")
def create_chain(payload: dict = Body(...)) -> dict:
    name = (payload.get("name") or "").strip()
    pipeline = (payload.get("pipeline") or "").strip()
    if not name or not pipeline:
        raise HTTPException(status_code=400, detail="name and pipeline are required")
    try:
        app_shabd.chain(pipeline, name=name, description=f"chain: {pipeline}",
                        tags=["chain"])
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    entry = {"name": name, "pipeline": pipeline}
    _chains.append(entry)
    return {"ok": True, "chain": entry}


# ---- Tool Sources: mount YAML-defined REST tools as spells ----
@api.get("/sources")
def list_sources() -> dict:
    return {"sources": _sources}


@api.post("/sources/connect")
def connect_source(payload: dict = Body(...)) -> dict:
    """Mount a YAML REST spec as a live spell (no Python)."""
    ys = {
        "name": (payload.get("name") or "").strip(),
        "url": payload.get("url", ""),
        "method": payload.get("method", "GET"),
        "params": payload.get("params", {}),
        "description": payload.get("description", ""),
        "tags": ["source", "yaml"],
    }
    if not ys["name"] or not ys["url"]:
        raise HTTPException(status_code=400, detail="name and url are required")
    try:
        ConfigLoader._register_yaml_spell(app_shabd, ys)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    entry = {"name": ys["name"], "type": "yaml", "url": ys["url"]}
    _sources.append(entry)
    return {"ok": True, "source": entry}


# ---- Builder: register a spell from posted Python (superuser feature) ----
@api.post("/builder")
def builder(payload: dict = Body(...)) -> dict:
    """Exec user Python that decorates spells onto `app`. NOTE: this runs code
    server-side — the gateway must gate it to superusers before prod use."""
    code = payload.get("code", "")
    if not code.strip():
        raise HTTPException(status_code=400, detail="code is required")
    before = set(app_shabd._spells.keys())
    namespace = {"app": app_shabd}
    try:
        exec(compile(code, "<builder>", "exec"), namespace)  # noqa: S102
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"{type(e).__name__}: {e}")
    added = sorted(set(app_shabd._spells.keys()) - before)
    if not added:
        raise HTTPException(status_code=400,
                            detail="no new spell registered — use @app.spell")
    return {"ok": True, "registered": added}


@api.get("/grimoire/verify")
def grimoire_verify() -> dict:
    return app_shabd.grimoire.verify()


@api.get("/grimoire/head")
def grimoire_head() -> dict:
    return {"head": app_shabd.grimoire.head(), "pages": len(app_shabd.grimoire._pages)}


@api.get("/grimoire/pages")
def grimoire_pages(since: int = 0, limit: int = 100) -> dict:
    return {"pages": app_shabd.grimoire.pages(since_seq=since, limit=limit)}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(api, host="0.0.0.0", port=bootstrap.SPELLS_PORT)
