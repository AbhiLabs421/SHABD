"""
users_service — identity & auth.

Owns its own SHABD app whose Grimoire chain (shared/data/users-audit.jsonl)
IS the user database: every register/login is a signed, tamper-evident page.
The first account to register automatically becomes the superuser.

On successful login we mint a SHABD bearer token (HMAC, signed with the SAME
stable secret every service uses) — so the token is valid engine-wide.

Endpoints
---------
POST /auth/register   {username, password}         -> user (first = superuser)
POST /auth/login      {username, password}          -> {token, user}
GET  /auth/verify?token=...                          -> {valid, subject, scopes}
GET  /users                                          -> list users (public)
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from common import bootstrap  # noqa: E402

from fastapi import Body, FastAPI, HTTPException  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

from shabd import SHABD  # noqa: E402
from shabd_users import UserError, UserStore  # noqa: E402

app_shabd = SHABD(
    "users",
    secret=bootstrap.SECRET,
    require_auth=False,
    grimoire_log_path=bootstrap.audit_path("users"),
)
store = UserStore(app_shabd)

api = FastAPI(title="SHABD users_service")
api.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)


def _roles_to_scopes(roles: list[str]) -> list[str]:
    scopes = set()
    if "superuser" in roles or "admin" in roles:
        scopes.update({"read", "write", "admin"})
    else:
        scopes.update({"read", "write"})
    return sorted(scopes)


@api.get("/health")
def health() -> dict:
    return {"service": "users", "ok": True, "users": len(store.list_users())}


@api.post("/auth/register")
def register(payload: dict = Body(...)) -> dict:
    try:
        user = store.register(payload.get("username", ""), payload.get("password", ""))
        return {"ok": True, "user": user.to_public()}
    except UserError as e:
        raise HTTPException(status_code=e.status, detail=e.message)


@api.post("/auth/login")
def login(payload: dict = Body(...)) -> dict:
    try:
        user = store.login(payload.get("username", ""), payload.get("password", ""))
    except UserError as e:
        raise HTTPException(status_code=e.status, detail=e.message)
    scopes = _roles_to_scopes(user.roles)
    token = app_shabd.issue_token(user.username, scopes=scopes, ttl=8 * 3600)
    return {"ok": True, "token": token, "user": user.to_public(), "scopes": scopes}


_SECRET_BYTES = bootstrap.SECRET.encode()


@api.get("/auth/verify")
def verify(token: str) -> dict:
    """Stateless verify: signature + expiry only. We deliberately do NOT use
    the engine's replay-protected verify here, because a session bearer token
    is presented on EVERY request — that is not a replay, it is normal use."""
    try:
        if not token or token.count(".") != 1:
            return {"valid": False, "reason": "malformed token"}
        body_b64, sig_b64 = token.split(".", 1)
        expected = hmac.new(_SECRET_BYTES, body_b64.encode(), hashlib.sha256).digest()
        pad = 4 - (len(sig_b64) % 4)
        given = base64.urlsafe_b64decode(sig_b64 + "=" * pad)
        if not hmac.compare_digest(expected, given):
            return {"valid": False, "reason": "invalid signature"}
        pad = 4 - (len(body_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(body_b64 + "=" * pad))
        if payload.get("exp", 0) < time.time():
            return {"valid": False, "reason": "token expired"}
        return {"valid": True, "subject": payload.get("sub"),
                "scopes": payload.get("scopes", [])}
    except Exception as e:
        return {"valid": False, "reason": str(e)}


@api.get("/users")
def users() -> dict:
    return {"users": [u.to_public() for u in store.list_users()]}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(api, host="0.0.0.0", port=bootstrap.USERS_PORT)
