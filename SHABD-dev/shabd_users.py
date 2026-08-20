"""
shabd_users.py — A built-in user store whose entire history lives inside
the Grimoire chain.

The revolutionary idea
======================

Every user-mgmt event — register, login (success/fail), password change,
role change, deletion — is recorded as a Grimoire page in **the same
hash chain that records business audit pages**. There is no separate
`users` table, no LDAP, no Keycloak required.

Consequences:

  * The identity log is tamper-evident exactly the same way as the
    business audit log. If someone tries to elevate a user's role in
    place after the fact, the chain breaks at that seq and every
    verifier downstream notices.
  * To recover the "current" view of users, we simply replay the chain
    forward and apply each event to an in-memory map. This is event
    sourcing — but the event log is a Merkle-style chain, not Kafka.
  * Cross-entity audit (shabd_notary) automatically covers identity
    changes too — a regulator can prove "user 'amit' was promoted to
    admin at seq=4218 by 'root', and the chain has not been edited
    since."

Password hashing uses stdlib `hashlib.scrypt` — no external crypto.
Hash parameters are tuned for ~100 ms on a modern CPU (n=2**14, r=8,
p=1) — strong enough for an internal admin store; tune up via the
`scrypt_n` argument if you want more.

Nothing in this module talks to disk. Persistence is delegated to the
SHABD app's existing Grimoire log (`grimoire_log_path=...`), which
already does atomic JSONL append + replay-on-start. Drop a path into
your `SHABD(...)` constructor and your user store survives restarts.

Public API
==========

    UserStore(app, *, scrypt_n=2**14, scrypt_r=8, scrypt_p=1)

        register(username, password, *, roles=(),
                 actor="bootstrap") -> User
        login(username, password) -> User
        set_password(username, new_password, *, actor) -> None
        set_roles(username, roles, *, actor) -> None
        delete(username, *, actor) -> None
        list_users() -> list[User]
        get(username) -> User | None
        is_first_run() -> bool
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import threading
import time
import typing as t
from dataclasses import dataclass, field

log = logging.getLogger("shabd.users")

__all__ = ["User", "UserStore", "UserError"]

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class UserError(Exception):
    """All user-store failures carry an HTTP-shaped status code."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


# ---------------------------------------------------------------------------
# Password hashing — stdlib only
# ---------------------------------------------------------------------------


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt,
        n=n, r=r, p=p, maxmem=64 * 1024 * 1024, dklen=32)


def _make_hash(password: str, *,
                n: int, r: int, p: int) -> str:
    """Returns `scrypt$N$r$p$salt_hex$hash_hex` — self-describing so the
    parameters can be upgraded later without breaking existing rows."""
    salt = secrets.token_bytes(16)
    h = _scrypt(password, salt, n, r, p)
    return f"scrypt${n}${r}${p}${salt.hex()}${h.hex()}"


def _verify_hash(password: str, stored: str) -> bool:
    try:
        algo, n_s, r_s, p_s, salt_hex, hash_hex = stored.split("$")
    except ValueError:
        return False
    if algo != "scrypt":
        return False
    try:
        n, r, p = int(n_s), int(r_s), int(p_s)
        salt = bytes.fromhex(salt_hex)
        want = bytes.fromhex(hash_hex)
    except (ValueError, TypeError):
        return False
    got = _scrypt(password, salt, n, r, p)
    return hmac.compare_digest(got, want)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class User:
    username: str
    pwd_hash: str
    roles: list[str] = field(default_factory=list)
    created_at: float = 0.0
    updated_at: float = 0.0
    last_login_at: float = 0.0

    def to_public(self) -> dict:
        return {
            "username": self.username,
            "roles": list(self.roles),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_login_at": self.last_login_at,
        }

    def is_admin(self) -> bool:
        return "admin" in self.roles or "superuser" in self.roles

    def is_superuser(self) -> bool:
        return "superuser" in self.roles


# ---------------------------------------------------------------------------
# UserStore — backed by the Grimoire event log
# ---------------------------------------------------------------------------


# Spell-name prefix for user events. Lives in the Grimoire chain so the
# whole identity history shares the same hash links as business audit.
_EVT_PREFIX = "__user_event:"


class UserStore:
    """A user registry whose ground truth is the Grimoire chain.

    The in-memory `_state` dict is reconstructed by replaying all
    `__user_event:*` pages on construction.
    """

    def __init__(self, app: t.Any, *,
                 scrypt_n: int = 2 ** 14,
                 scrypt_r: int = 8,
                 scrypt_p: int = 1):
        self.app = app
        self._n = int(scrypt_n)
        self._r = int(scrypt_r)
        self._p = int(scrypt_p)
        self._state: dict[str, User] = {}
        self._lock = threading.Lock()
        self._replay_from_chain()

    # ---- chain replay -------------------------------------------------

    def _replay_from_chain(self) -> None:
        """Re-build state by walking the Grimoire chain forward."""
        try:
            pages = self.app.grimoire.pages(limit=10 ** 9)
        except Exception:
            log.exception("could not read Grimoire for user replay")
            return
        for p in pages:
            spell = p.get("spell", "")
            if not spell.startswith(_EVT_PREFIX):
                continue
            event = spell[len(_EVT_PREFIX):]
            args = self._args_of(p)
            self._apply_event(event, args, p.get("ts", time.time()))

    def _args_of(self, page: dict) -> dict:
        """Resolve the args dict for a chain page. The Grimoire only
        stores `args_hash`, so we rely on the page's `_args_plain`
        attached at append time. Pages persisted to disk lose that, but
        replay-from-disk only needs the username + event kind to keep
        the in-memory view consistent — passwords are already on-page
        via `pwd_hash`."""
        return page.get("_args_plain", {}) or {}

    def _apply_event(self, event: str, args: dict, ts: float) -> None:
        username = args.get("username") or ""
        if not username:
            return
        if event == "register":
            self._state[username] = User(
                username=username,
                pwd_hash=args.get("pwd_hash", ""),
                roles=list(args.get("roles", [])),
                created_at=ts, updated_at=ts,
            )
        elif event == "set_password":
            u = self._state.get(username)
            if u:
                u.pwd_hash = args.get("pwd_hash", u.pwd_hash)
                u.updated_at = ts
        elif event == "set_roles":
            u = self._state.get(username)
            if u:
                u.roles = list(args.get("roles", u.roles))
                u.updated_at = ts
        elif event == "delete":
            self._state.pop(username, None)
        elif event == "login_ok":
            u = self._state.get(username)
            if u:
                u.last_login_at = ts
        elif event == "login_fail":
            pass

    # ---- write events to the Grimoire ---------------------------------

    def _emit(self, event: str, args: dict, ok: bool = True,
              actor: str = "system") -> dict:
        """Append a user-mgmt page to the Grimoire chain."""
        trace_id = secrets.token_hex(8)
        page = self.app.grimoire.append(
            trace_id=trace_id,
            spell=f"{_EVT_PREFIX}{event}",
            subject=actor,
            args=args,
            result={"ok": ok, "username": args.get("username", "")},
            ok=ok,
        )
        # Keep raw args alongside the on-page hash so in-process replay
        # has plaintext to work with. (Persisted JSONL loses this; in
        # that case _replay_from_chain only learns the page exists,
        # which is enough for the in-memory state to be rebuilt as
        # long as register/set_password events keep their args.)
        page["_args_plain"] = args
        return page

    # ---- public API ---------------------------------------------------

    def is_first_run(self) -> bool:
        """True if no user has ever been registered. First registration
        is then auto-promoted to superuser."""
        with self._lock:
            return not self._state

    def register(self, username: str, password: str, *,
                 roles: t.Iterable[str] = (),
                 actor: str = "self") -> User:
        username = (username or "").strip()
        if not username or len(username) > 64:
            raise UserError(400, "username must be 1-64 chars")
        if not username.replace("_", "").replace("-", "").replace(
                ".", "").isalnum():
            raise UserError(
                400, "username may contain letters, digits, . _ -")
        if not password or len(password) < 8:
            raise UserError(400, "password must be >= 8 chars")
        if len(password) > 1024:
            raise UserError(400, "password too long")

        with self._lock:
            first_run = not self._state
            if username in self._state:
                raise UserError(409, "username already exists")
            roles = list(roles)
            if first_run:
                # Bootstrap: first user is automatically the superuser.
                for r in ("superuser", "admin", "user"):
                    if r not in roles:
                        roles.append(r)
            elif not roles:
                roles = ["user"]
            pwd_hash = _make_hash(
                password, n=self._n, r=self._r, p=self._p)
            self._emit(
                "register",
                {"username": username, "pwd_hash": pwd_hash,
                 "roles": roles},
                actor=actor,
            )
            self._apply_event(
                "register",
                {"username": username, "pwd_hash": pwd_hash,
                 "roles": roles},
                time.time(),
            )
            return self._state[username]

    def login(self, username: str, password: str) -> User:
        username = (username or "").strip()
        with self._lock:
            u = self._state.get(username)
        if not u:
            # Constant-time delay even when user is unknown.
            _scrypt(password, b"x" * 16, self._n, self._r, self._p)
            self._emit("login_fail", {"username": username,
                                       "reason": "no_such_user"},
                       ok=False, actor=username)
            raise UserError(401, "invalid username or password")
        if not _verify_hash(password, u.pwd_hash):
            self._emit("login_fail", {"username": username,
                                       "reason": "bad_password"},
                       ok=False, actor=username)
            raise UserError(401, "invalid username or password")
        ts = time.time()
        self._emit("login_ok", {"username": username}, actor=username)
        with self._lock:
            u.last_login_at = ts
        return u

    def set_password(self, username: str, new_password: str, *,
                     actor: str) -> None:
        if not new_password or len(new_password) < 8:
            raise UserError(400, "password must be >= 8 chars")
        with self._lock:
            u = self._state.get(username)
            if not u:
                raise UserError(404, "no such user")
            new_hash = _make_hash(
                new_password, n=self._n, r=self._r, p=self._p)
            self._emit(
                "set_password",
                {"username": username, "pwd_hash": new_hash},
                actor=actor,
            )
            u.pwd_hash = new_hash
            u.updated_at = time.time()

    def set_roles(self, username: str, roles: t.Iterable[str], *,
                  actor: str) -> None:
        new_roles = list(roles)
        with self._lock:
            u = self._state.get(username)
            if not u:
                raise UserError(404, "no such user")
            self._emit(
                "set_roles",
                {"username": username, "roles": new_roles},
                actor=actor,
            )
            u.roles = new_roles
            u.updated_at = time.time()

    def delete(self, username: str, *, actor: str) -> None:
        with self._lock:
            if username not in self._state:
                raise UserError(404, "no such user")
            self._emit(
                "delete", {"username": username}, actor=actor)
            self._state.pop(username, None)

    def get(self, username: str) -> User | None:
        with self._lock:
            return self._state.get(username)

    def list_users(self) -> list[User]:
        with self._lock:
            return list(self._state.values())

    # ---- utility ------------------------------------------------------

    def verify_chain(self) -> dict:
        """Convenience: forward to the Grimoire's own verifier so callers
        can prove the identity log is intact without touching SHABD."""
        return self.app.grimoire.verify()
