"""
gateway — the single front door (port 8000).

Everything the frontend calls goes through here. The gateway:
  * exposes /api/auth/* WITHOUT auth (register + login),
  * requires a valid bearer token for every OTHER /api/* route
    (this is the "shabd_ui login pehle, phir baaki khulta hai" rule),
  * reverse-proxies to the independent services.

If any one downstream service is down, only ITS routes fail — the gateway and
the rest keep serving. That is the crash-isolation you wanted.

Route map
---------
/api/auth/*      -> users_service      (open)
/api/users       -> users_service      (auth)
/api/spells*     -> spells_service     (auth)
/api/grimoire/*  -> spells_service     (auth)
/api/notary/*    -> notary_service     (auth)
/api/agent/*     -> agent_service      (auth)
/api/health      -> aggregate          (open)
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from common import bootstrap  # noqa: E402

import httpx  # noqa: E402
from fastapi import FastAPI, Request, Response  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

api = FastAPI(title="SHABD gateway")
api.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

_client = httpx.AsyncClient(timeout=180.0)

# prefix -> downstream base url (longest/most-specific first)
ROUTES = [
    ("/api/auth", bootstrap.USERS_URL, "/auth"),
    ("/api/users", bootstrap.USERS_URL, "/users"),
    ("/api/spells", bootstrap.SPELLS_URL, "/spells"),
    ("/api/grimoire", bootstrap.SPELLS_URL, "/grimoire"),
    ("/api/chains", bootstrap.SPELLS_URL, "/chains"),
    ("/api/sources", bootstrap.SPELLS_URL, "/sources"),
    ("/api/builder", bootstrap.SPELLS_URL, "/builder"),
    ("/api/notary", bootstrap.NOTARY_URL, "/notary"),
    ("/api/orchestrator", bootstrap.AGENT_URL, "/orchestrator"),
    ("/api/agent", bootstrap.AGENT_URL, "/agent"),
]

OPEN_PREFIXES = ("/api/auth", "/api/health")


async def _is_authorized(request: Request) -> bool:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return False
    token = auth[7:]
    try:
        r = await _client.get(f"{bootstrap.USERS_URL}/auth/verify",
                              params={"token": token})
        return bool(r.json().get("valid"))
    except Exception:
        return False


@api.get("/api/health")
async def health() -> dict:
    out = {"gateway": True}
    checks = {
        "users": bootstrap.USERS_URL,
        "spells": bootstrap.SPELLS_URL,
        "notary": bootstrap.NOTARY_URL,
        "agent": bootstrap.AGENT_URL,
    }
    for name, url in checks.items():
        try:
            r = await _client.get(f"{url}/health", timeout=2.0)
            out[name] = r.json().get("ok", False)
        except Exception:
            out[name] = False  # one service down != gateway down
    return out


@api.post("/api/client/call")
async def client_call(request: Request) -> Response:
    """Client Console: outbound proxy to ANY external SHABD server. Keeps the
    remote token server-side and dodges browser CORS. Auth-gated like the rest."""
    if not await _is_authorized(request):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    payload = await request.json()
    base = (payload.get("base_url") or "").rstrip("/")
    token = payload.get("token") or ""
    action = payload.get("action") or "manifest"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        if action == "manifest":
            r = await _client.get(f"{base}/manifest", headers=headers, timeout=10.0)
        elif action == "invoke":
            r = await _client.post(
                f"{base}/spells/{payload.get('spell')}",
                json=payload.get("args", {}), headers=headers, timeout=30.0)
        else:
            return JSONResponse(status_code=400, content={"error": "bad_action"})
        return Response(content=r.content, status_code=r.status_code,
                        media_type=r.headers.get("content-type", "application/json"))
    except Exception as e:
        return JSONResponse(status_code=502,
                            content={"error": "remote_unreachable", "detail": str(e)})


@api.api_route("/api/{path:path}",
               methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy(path: str, request: Request) -> Response:
    full = "/api/" + path

    # auth gate (everything except open prefixes)
    if not any(full.startswith(p) for p in OPEN_PREFIXES):
        if not await _is_authorized(request):
            return JSONResponse(
                status_code=401,
                content={"error": "unauthorized", "message": "login first"},
            )

    for prefix, base, downstream_prefix in ROUTES:
        if full == prefix or full.startswith(prefix + "/") or full.startswith(prefix):
            tail = full[len(prefix):]
            target = f"{base}{downstream_prefix}{tail}"
            body = await request.body()
            try:
                resp = await _client.request(
                    request.method, target,
                    content=body,
                    params=dict(request.query_params),
                    headers={"content-type": request.headers.get("content-type",
                                                                  "application/json")},
                )
            except Exception as e:
                return JSONResponse(
                    status_code=502,
                    content={"error": "service_unavailable",
                             "service": base, "detail": str(e)},
                )
            media = resp.headers.get("content-type", "application/json")
            return Response(content=resp.content, status_code=resp.status_code,
                            media_type=media)

    return JSONResponse(status_code=404, content={"error": "no_route", "path": full})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(api, host="0.0.0.0", port=bootstrap.GATEWAY_PORT)
