"""
stable_secret.py — ONE stable HMAC secret across every SHABD service.

Why this file exists
====================
The Grimoire signs every audit page with the app `secret`. `verify()` later
recomputes that HMAC. If the secret is DIFFERENT on a later start, every
persisted page fails the signature check and the dashboard shows
"Tamper detected" — even though nothing was actually tampered.

That is exactly the "baar baar tamper" symptom: the old launcher fell back to
`os.environ.get("SHABD_SECRET", "x"*32)`, so a run *with* the env var and a run
*without* it used two different keys over the same on-disk chain.

The fix: resolve the secret in ONE deterministic way for every process:

    1. SHABD_SECRET / CONJURE_SECRET env var, if set (prod).
    2. Otherwise a generated key persisted once to `shared/.shabd-secret`
       and reused by every service forever after.

Because all services import this, they all sign with the same key, so their
Grimoire chains stay verifiable across restarts.
"""
from __future__ import annotations

import os
import pathlib
import secrets

__all__ = ["load_secret", "secret_file_path"]


def secret_file_path() -> pathlib.Path:
    """`<repo-root>/shared/.shabd-secret`. This file is two levels up from
    backend/shabd_core/ ."""
    override = os.environ.get("SHABD_SECRET_FILE")
    if override:
        return pathlib.Path(override)
    root = pathlib.Path(__file__).resolve().parents[2]  # shabd_core -> backend -> root
    return root / "shared" / ".shabd-secret"


def load_secret() -> str:
    """Return the one stable secret. Creates + persists it on first call."""
    env = os.environ.get("SHABD_SECRET") or os.environ.get("CONJURE_SECRET")
    if env and len(env) >= 16:
        return env

    path = secret_file_path()
    if path.exists():
        value = path.read_text(encoding="utf-8").strip()
        if len(value) >= 16:
            return value

    # First run anywhere: mint a strong key and persist it for every service.
    value = secrets.token_hex(32)  # 64 hex chars = 256 bits
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    try:
        os.chmod(path, 0o600)  # best-effort; no-op on some Windows setups
    except OSError:
        pass
    return value
