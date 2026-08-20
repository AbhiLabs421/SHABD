"""
bootstrap.py — shared setup for every microservice.

Puts `backend/shabd_core` on sys.path (so `from shabd import SHABD` works),
exposes the stable secret, service URLs/ports, and the shared data dir.

Each service is an INDEPENDENT process. If one crashes, the others keep
running — that is the whole point of the split. They only share:
  * the stable secret (so Grimoire chains verify across services), and
  * plain HTTP calls through the gateway.
"""
from __future__ import annotations

import os
import pathlib
import sys

# --- make the core library importable (flat modules) ---
# this file: backend/services/common/bootstrap.py -> parents[2] == backend/
_BACKEND = pathlib.Path(__file__).resolve().parents[2]          # backend/
_CORE = _BACKEND / "shabd_core"
for _p in (str(_BACKEND), str(_CORE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from stable_secret import load_secret  # noqa: E402

# --- shared, durable data directory (Grimoire logs, secret) ---
REPO_ROOT = _BACKEND.parent


def _load_dotenv() -> None:
    """Tiny stdlib .env loader (no python-dotenv dependency). Reads
    backend/.env then repo-root .env; existing env vars always win."""
    for envfile in (_BACKEND / ".env", REPO_ROOT / ".env"):
        if not envfile.exists():
            continue
        for line in envfile.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip().strip('"').strip("'")
            os.environ.setdefault(key, val)


_load_dotenv()
DATA_DIR = pathlib.Path(os.environ.get("SHABD_DATA_DIR", REPO_ROOT / "shared" / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

SECRET = load_secret()


def audit_path(name: str) -> str:
    """Per-service Grimoire JSONL path, e.g. audit_path('spells')."""
    return str(DATA_DIR / f"{name}-audit.jsonl")


# --- service registry (override via env in docker / prod) ---
def _url(env: str, default: str) -> str:
    return os.environ.get(env, default).rstrip("/")


GATEWAY_PORT = int(os.environ.get("GATEWAY_PORT", "8000"))
SPELLS_PORT = int(os.environ.get("SPELLS_PORT", "8001"))
NOTARY_PORT = int(os.environ.get("NOTARY_PORT", "8002"))
USERS_PORT = int(os.environ.get("USERS_PORT", "8003"))
AGENT_PORT = int(os.environ.get("AGENT_PORT", "8004"))

SPELLS_URL = _url("SPELLS_URL", f"http://127.0.0.1:{SPELLS_PORT}")
NOTARY_URL = _url("NOTARY_URL", f"http://127.0.0.1:{NOTARY_PORT}")
USERS_URL = _url("USERS_URL", f"http://127.0.0.1:{USERS_PORT}")
AGENT_URL = _url("AGENT_URL", f"http://127.0.0.1:{AGENT_PORT}")
