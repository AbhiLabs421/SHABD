"""
notary_service — cross-entity witness over the engine's Grimoire.

This service does NOT own the chain. It PULLS the spells engine's Grimoire
pages over HTTP and wraps them with an AgentNotary, so it can:
  * publish a signed root (snapshot of the chain head at an instant), and
  * build an inclusion proof that "decision #seq existed at that moment",
    which a regulator can verify offline.

Because it uses the same stable secret, its roots verify consistently.

Endpoints
---------
GET  /notary/verify           -> is the engine chain internally consistent?
GET  /notary/root             -> publish + return a signed NotaryRoot
POST /notary/inclusion {seq}  -> inclusion proof for one page + its verification
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from common import bootstrap  # noqa: E402

import httpx  # noqa: E402
from fastapi import Body, FastAPI, HTTPException  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

from shabd_notary import AgentNotary, verify_inclusion  # noqa: E402

ENTITY = "shabd-notary"
PUB_SECRET = bootstrap.SECRET  # stable; hex string >= 16 bytes


# --- shim so AgentNotary can read a chain it doesn't own locally ---
class _RemoteGrimoire:
    GENESIS = "0" * 64

    def __init__(self, pages: list[dict]):
        self._pages = pages

    def head(self) -> str:
        return self._pages[-1]["hash"] if self._pages else self.GENESIS


class _ShimApp:
    def __init__(self, pages: list[dict]):
        self.grimoire = _RemoteGrimoire(pages)


def _fetch_pages() -> list[dict]:
    try:
        r = httpx.get(f"{bootstrap.SPELLS_URL}/grimoire/pages",
                      params={"since": 0, "limit": 10 ** 9}, timeout=5.0)
        r.raise_for_status()
        return r.json()["pages"]
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"engine unreachable: {e}")


def _notary(pages: list[dict]) -> AgentNotary:
    return AgentNotary(_ShimApp(pages), entity=ENTITY, publishing_secret=PUB_SECRET)


api = FastAPI(title="SHABD notary_service")
api.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)


@api.get("/health")
def health() -> dict:
    return {"service": "notary", "ok": True, "entity": ENTITY}


@api.get("/notary/verify")
def verify() -> dict:
    try:
        r = httpx.get(f"{bootstrap.SPELLS_URL}/grimoire/verify", timeout=5.0)
        return r.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"engine unreachable: {e}")


@api.get("/notary/root")
def publish_root() -> dict:
    pages = _fetch_pages()
    root = _notary(pages).publish_root()
    return {"ok": True, "root": root.to_dict()}


@api.post("/notary/inclusion")
def inclusion(payload: dict = Body(...)) -> dict:
    seq = int(payload.get("seq", -1))
    pages = _fetch_pages()
    nt = _notary(pages)
    root = nt.publish_root()
    try:
        proof = nt.build_inclusion_proof(seq=seq, against=root)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    result = verify_inclusion(proof, PUB_SECRET.encode() if not _is_hex(PUB_SECRET)
                              else bytes.fromhex(PUB_SECRET))
    return {"ok": True, "proof": proof.to_dict(), "verification": result}


def _is_hex(s: str) -> bool:
    try:
        bytes.fromhex(s)
        return True
    except ValueError:
        return False


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(api, host="0.0.0.0", port=bootstrap.NOTARY_PORT)
