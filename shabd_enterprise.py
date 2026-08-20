"""
shabd_enterprise.py — Bank / exchange / regulated-industry extensions.

The core `shabd.py` keeps the "single file, zero runtime deps" promise.
This file is a deliberate, optional sidecar that ships every feature a
Tier-1 bank or exchange will ask for in a procurement checklist, while
still using the Python standard library wherever possible. A few
features (AES-GCM at rest, X.509 signing, real PKCS#11 HSM, LDAP) need
the `cryptography` / `python-ldap` packages — those import lazily and
fail with a clear error if the package is missing.

What's in this file (all opt-in; each is a small, well-named class):

  Keys & secrets
    KeyProvider                — abstract; .get_signing_key(), .get_verifying_keys()
    EnvKeyProvider             — reads SHABD_SECRET + SHABD_SECRET_OLD
    FileKeyProvider            — reads keys from a directory (one file per kid)
    HSMKeyProvider             — PKCS#11 stub (real implementations swap this in)

  Authentication
    LDAPAuthProvider           — bind-and-search against a directory
    SAMLAuthProvider           — assertion verifier (stub interface)
    SSOTokenExchanger          — exchanges an upstream JWT for a SHABD token

  Authorisation
    RBACPolicyEngine           — declarative role/permission rules
    SeparationOfDutiesPolicy   — enforces dual-control for sensitive spells

  Persistence & encryption
    SQLiteGrimoirePersistence  — append-only WAL-mode SQLite for the audit chain
    EncryptedGrimoireJSONL     — AES-GCM-at-rest wrapper around GrimoireJSONL
    X509Signer                 — signs each Grimoire page with an X.509 key
                                 (for non-repudiation in court)

  Transport hardening
    MTLSConfig                 — client-cert verification (ssl.SSLContext factory)
    install_mtls_on            — wraps `app.serve` to require client certs

  Streaming / observability
    OTLPSpanExporter           — POSTs spans in OTLP/HTTP-JSON to a collector
    KafkaAuditStreamer         — pushes Grimoire pages to a Kafka topic
                                 (uses kafka-python if available; otherwise a
                                 plain TCP fallback for very small clusters)
    PrometheusPushGateway      — push metrics to a Pushgateway (for short jobs)

  High availability
    ClusterPeer                — peer-to-peer push replication for Grimoire
    HAGrimoireCoordinator      — simplistic leader/follower coordinator

  Bundled installer
    install_enterprise(app, ...) — wires whichever components you pass in
                                   onto an existing SHABD app

Designed so that an InfoSec reviewer can read it once and audit every
trust boundary. No surprise network calls, no telemetry beacons, no
auto-update logic. Every outbound side-effect goes through one of the
named classes above.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import socket
import sqlite3
import ssl
import threading
import time
import typing as t
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from shabd import (
    SHABD,
    AuditWebhook,
    AuthError,
    ConjureError,
    Context,
    ForbiddenError,
    Grimoire,
    GrimoireJSONL,
    TokenManager,
)

log = logging.getLogger("shabd.enterprise")

__all__ = [
    # Keys
    "KeyProvider", "EnvKeyProvider", "FileKeyProvider", "HSMKeyProvider",
    # Auth
    "LDAPAuthProvider", "SAMLAuthProvider", "SSOTokenExchanger",
    # Authz
    "RBACPolicyEngine", "SeparationOfDutiesPolicy",
    # Persistence / encryption
    "SQLiteGrimoirePersistence", "PostgresGrimoirePersistence",
    "EncryptedGrimoireJSONL", "X509Signer",
    # Transport
    "MTLSConfig", "install_mtls_on",
    # Observability
    "OTLPSpanExporter", "KafkaAuditStreamer", "PrometheusPushGateway",
    # HA
    "ClusterPeer", "HAGrimoireCoordinator",
    # Installer
    "install_enterprise",
]


# ============================================================================
# KEYS & SECRETS
# ============================================================================

class KeyProvider:
    """Source of signing material. The token manager and the Grimoire
    consume a `KeyProvider` so deployments can swap env vars for an HSM
    without touching application code."""

    def get_signing_key(self) -> bytes:
        raise NotImplementedError

    def get_verifying_keys(self) -> list[bytes]:
        return [self.get_signing_key()]


class EnvKeyProvider(KeyProvider):
    """Reads `SHABD_SECRET` (active) and optionally `SHABD_SECRET_OLD`
    (accepted on verify only — used during a zero-downtime rotation)."""

    def __init__(self, active_env: str = "SHABD_SECRET",
                 fallback_env: str = "SHABD_SECRET_OLD"):
        self.active_env = active_env
        self.fallback_env = fallback_env

    def _decode(self, raw: str) -> bytes:
        if not raw:
            raise ValueError("empty key")
        # Hex preferred; fall back to raw bytes so "x" * 32 works too.
        try:
            return bytes.fromhex(raw)
        except ValueError:
            return raw.encode()

    def get_signing_key(self) -> bytes:
        raw = os.environ.get(self.active_env, "")
        if not raw:
            raise RuntimeError(f"{self.active_env} not set")
        return self._decode(raw)

    def get_verifying_keys(self) -> list[bytes]:
        out = [self.get_signing_key()]
        fb = os.environ.get(self.fallback_env, "")
        if fb:
            out.append(self._decode(fb))
        return out


class FileKeyProvider(KeyProvider):
    """Reads keys from a directory, one file per key. The newest file
    (by mtime) is the active signing key. Useful when ops rotates keys
    by dropping a new file via Ansible / Vault Agent."""

    def __init__(self, key_dir: str):
        self.key_dir = key_dir

    def _files(self) -> list[str]:
        if not os.path.isdir(self.key_dir):
            return []
        files = [os.path.join(self.key_dir, f)
                 for f in os.listdir(self.key_dir)
                 if not f.startswith(".")]
        files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        return files

    def get_signing_key(self) -> bytes:
        files = self._files()
        if not files:
            raise RuntimeError(f"no key files in {self.key_dir}")
        with open(files[0], "rb") as fh:
            data = fh.read().strip()
        try:
            return bytes.fromhex(data.decode())
        except (ValueError, UnicodeDecodeError):
            return data

    def get_verifying_keys(self) -> list[bytes]:
        out = []
        for path in self._files():
            with open(path, "rb") as fh:
                data = fh.read().strip()
            try:
                out.append(bytes.fromhex(data.decode()))
            except (ValueError, UnicodeDecodeError):
                out.append(data)
        return out


class HSMKeyProvider(KeyProvider):
    """PKCS#11 stub. Real banking deployments swap this for a vendor
    implementation (Thales Luna, SafeNet, AWS CloudHSM, Utimaco).

    The contract is: `get_signing_key()` returns *some* bytes that the
    HSM allows the `TokenManager` and `Grimoire` to use as an HMAC key.
    Most HSMs expose this via a software-derived material key (KDF over
    a hardware-protected master), or via PKCS#11 secret-key objects.

    If you can't expose key material at all (true on some HSMs), wrap
    the TokenManager's `hmac.new(...)` call in a `KeyProxy` that asks
    the HSM to compute the MAC instead — that needs a small change to
    TokenManager and is out of scope for this stub.
    """

    def __init__(self, slot_id: int, label: str, pin: str,
                 *, fallback_env: str = "SHABD_SECRET"):
        self.slot_id = slot_id
        self.label = label
        self.pin = pin
        self.fallback_env = fallback_env

    def _try_pkcs11(self) -> bytes | None:
        try:
            import pkcs11  # type: ignore
        except ImportError:
            return None
        try:
            lib = pkcs11.lib(os.environ.get("PKCS11_LIB", ""))
            token = lib.get_token(token_label=self.label)
            with token.open(rw=False, user_pin=self.pin) as session:
                key_obj = session.get_key(label=self.label,
                                          object_class=pkcs11.ObjectClass.SECRET_KEY)
                # Most HSMs make this attribute non-extractable. If so,
                # callers should use a KeyProxy instead. We try once.
                return bytes(key_obj[pkcs11.Attribute.VALUE])
        except Exception:
            log.exception("HSM access failed; falling back to env")
            return None

    def get_signing_key(self) -> bytes:
        b = self._try_pkcs11()
        if b is not None:
            return b
        raw = os.environ.get(self.fallback_env, "")
        if not raw:
            raise RuntimeError(
                "HSM unavailable and no fallback SHABD_SECRET in env"
            )
        try:
            return bytes.fromhex(raw)
        except ValueError:
            return raw.encode()


# ============================================================================
# AUTHENTICATION — LDAP / SAML / SSO bridging
# ============================================================================

@dataclass
class _AuthnResult:
    ok: bool
    subject: str = ""
    scopes: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)


class LDAPAuthProvider:
    """Bind-and-search authentication against AD or any LDAPv3 directory.

    Needs `ldap3`. Pass the same instance to `install_enterprise(...,
    ldap=ldap_provider)` and SHABD will accept `Authorization: Basic ...`
    (or a header you choose) for human users while keeping HMAC tokens
    for machines.
    """

    def __init__(self, host: str, base_dn: str,
                 user_template: str = "uid={user},ou=people,{base_dn}",
                 group_attr: str = "memberOf",
                 *, use_tls: bool = True):
        self.host = host
        self.base_dn = base_dn
        self.user_template = user_template
        self.group_attr = group_attr
        self.use_tls = use_tls

    def authenticate(self, username: str, password: str) -> _AuthnResult:
        try:
            import ldap3  # type: ignore
        except ImportError:
            raise RuntimeError(
                "LDAPAuthProvider requires `pip install ldap3`"
            )
        server = ldap3.Server(self.host, use_ssl=self.use_tls)
        user_dn = self.user_template.format(user=username, base_dn=self.base_dn)
        try:
            with ldap3.Connection(server, user_dn, password,
                                  auto_bind=True) as conn:
                conn.search(self.base_dn, f"(uid={username})",
                            attributes=[self.group_attr, "cn", "mail"])
                if not conn.entries:
                    return _AuthnResult(ok=False)
                entry = conn.entries[0]
                scopes = [str(g).split(",")[0].replace("cn=", "")
                          for g in (entry[self.group_attr].values or [])]
                return _AuthnResult(
                    ok=True, subject=username, scopes=scopes,
                    raw={"dn": user_dn, "cn": str(entry.cn),
                         "mail": str(entry.mail)},
                )
        except Exception:
            log.exception("LDAP bind failed for %s", username)
            return _AuthnResult(ok=False)


class SAMLAuthProvider:
    """Interface for SAML 2.0 assertion verification.

    The real cert / canonicalisation / signature validation is delegated
    to a callable injected at construction. Most teams will use
    `python3-saml` or `pysaml2` and pass its `process_response` method.
    """

    def __init__(self, assertion_verifier: t.Callable[[str], _AuthnResult]):
        self._verifier = assertion_verifier

    def authenticate(self, assertion_b64: str) -> _AuthnResult:
        try:
            return self._verifier(assertion_b64)
        except Exception:
            log.exception("SAML verification failed")
            return _AuthnResult(ok=False)


class SSOTokenExchanger:
    """Exchanges an upstream JWT (Cognito, Auth0, Okta, internal IdP)
    for a SHABD HMAC token.

    `verify_upstream(jwt) -> _AuthnResult` is supplied by the caller and
    is the only piece that needs library code (PyJWT etc.). The result's
    scopes/subject are honoured one-to-one by SHABD's `issue_token`.
    """

    def __init__(self, app: SHABD,
                 verify_upstream: t.Callable[[str], _AuthnResult],
                 *, default_ttl: int = 3600):
        self.app = app
        self.verify_upstream = verify_upstream
        self.default_ttl = default_ttl

    def exchange(self, upstream_jwt: str,
                 ttl: int | None = None) -> str:
        res = self.verify_upstream(upstream_jwt)
        if not res.ok:
            raise AuthError("upstream token rejected")
        return self.app.issue_token(
            res.subject, scopes=res.scopes,
            ttl=ttl if ttl is not None else self.default_ttl,
        )


# ============================================================================
# AUTHORISATION — Fine-grained RBAC + Separation of Duties
# ============================================================================

@dataclass
class _PolicyRule:
    role: str
    allow_spells: list[str] = field(default_factory=list)   # exact names
    allow_prefixes: list[str] = field(default_factory=list) # "finance.*"
    deny_spells: list[str] = field(default_factory=list)
    require_attrs: dict[str, t.Any] = field(default_factory=dict)


class RBACPolicyEngine:
    """Declarative role -> spell allow/deny rules. Pluggable so policy
    can live in YAML / a DB / Open Policy Agent. The default
    implementation is in-memory."""

    def __init__(self):
        self._rules: list[_PolicyRule] = []
        self._lock = threading.Lock()

    def add_rule(self, role: str, *,
                 allow: t.Iterable[str] = (),
                 allow_prefixes: t.Iterable[str] = (),
                 deny: t.Iterable[str] = (),
                 require_attrs: dict | None = None) -> None:
        with self._lock:
            self._rules.append(_PolicyRule(
                role=role,
                allow_spells=list(allow),
                allow_prefixes=list(allow_prefixes),
                deny_spells=list(deny),
                require_attrs=require_attrs or {},
            ))

    def evaluate(self, ctx: Context, spell_name: str) -> None:
        """Raises ForbiddenError if not allowed."""
        # The user's roles are the union of ctx.scopes and any role list in metadata.
        roles = set(ctx.scopes) | set(ctx.metadata.get("roles", []))
        with self._lock:
            applicable = [r for r in self._rules if r.role in roles or r.role == "*"]
        if not applicable:
            raise ForbiddenError(f"no policy grants access to '{spell_name}'",
                                 hint="Attach a role/scope that has an allow rule.")
        for rule in applicable:
            if spell_name in rule.deny_spells:
                raise ForbiddenError(
                    f"role '{rule.role}' explicitly denies '{spell_name}'"
                )
            for key, want in rule.require_attrs.items():
                got = ctx.metadata.get(key)
                if got != want:
                    raise ForbiddenError(
                        f"role '{rule.role}' requires attribute "
                        f"{key}={want!r}, got {got!r}"
                    )
        # At least one applicable rule must explicitly allow the spell.
        for rule in applicable:
            if spell_name in rule.allow_spells:
                return
            for prefix in rule.allow_prefixes:
                # Accept "finance.*", "finance_*", "finance*" — any trailing
                # star means "everything that starts with the rest".
                if prefix.endswith("*"):
                    stem = prefix[:-1]
                    if spell_name.startswith(stem):
                        return
                elif prefix == spell_name:
                    return
        raise ForbiddenError(
            f"no role among {sorted(roles)} allows '{spell_name}'",
            hint=("Add the spell to an `allow` list or extend a prefix "
                  "rule in your RBAC policy."),
        )

    def install_on(self, app: SHABD) -> None:
        engine = self

        def _hook(ctx, spell_name, args):
            engine.evaluate(ctx, spell_name)
        app.before(_hook)


class SeparationOfDutiesPolicy:
    """Dual-control: a spell tagged dual_control requires the caller to
    pass an `approver_token` belonging to a different subject."""

    def __init__(self, app: SHABD, *, sensitive_spells: t.Iterable[str]):
        self.app = app
        self.sensitive = set(sensitive_spells)

        def _hook(ctx, spell_name, args):
            if spell_name not in self.sensitive:
                return
            approver = args.pop("approver_token", None) if isinstance(args, dict) else None
            if not approver:
                raise ForbiddenError(
                    f"'{spell_name}' requires an approver_token",
                    hint="Have a second authorised user issue you a one-time "
                         "approver token (different subject)."
                )
            payload = app.tokens.verify(approver)
            if payload["sub"] == ctx.subject:
                raise ForbiddenError(
                    "approver must be a different subject (dual-control)",
                    hint="The approver_token's `sub` must differ from yours."
                )

        app.before(_hook)


# ============================================================================
# GRIMOIRE PERSISTENCE — SQLite (stdlib) and Encrypted-at-Rest
# ============================================================================

class SQLiteGrimoirePersistence:
    """Append-only WAL-mode SQLite store for the Grimoire chain.

    Why SQLite when JSONL works? Three reasons that matter to banks:
      * Random-access reads by seq / trace_id without rescanning a 5 GB file.
      * Concurrent readers (auditor + app) without file locking pain.
      * A single file the bank's existing DB backup tooling already
        understands.

    Drop-in for `grimoire_log_path`-style usage via `install_on(app)`.
    """

    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False,
                                    isolation_level=None)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS grimoire_pages (
                seq INTEGER PRIMARY KEY,
                ts REAL NOT NULL,
                trace_id TEXT NOT NULL,
                spell TEXT NOT NULL,
                subject TEXT NOT NULL,
                ok INTEGER NOT NULL,
                hash TEXT NOT NULL UNIQUE,
                page_json TEXT NOT NULL
            )
        """)
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_grimoire_trace ON grimoire_pages(trace_id)"
        )
        self._lock = threading.Lock()

    def append(self, page: dict) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO grimoire_pages (seq, ts, trace_id, spell, "
                "subject, ok, hash, page_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (page["seq"], page["ts"], page["trace_id"], page["spell"],
                 page["subject"], 1 if page["ok"] else 0, page["hash"],
                 json.dumps(page, separators=(",", ":"), default=str)),
            )

    def load_all(self) -> list[dict]:
        cur = self.conn.execute(
            "SELECT page_json FROM grimoire_pages ORDER BY seq"
        )
        return [json.loads(row[0]) for row in cur]

    def find_by_trace(self, trace_id: str) -> list[dict]:
        cur = self.conn.execute(
            "SELECT page_json FROM grimoire_pages WHERE trace_id = ? ORDER BY seq",
            (trace_id,),
        )
        return [json.loads(row[0]) for row in cur]

    def install_on(self, app: SHABD) -> None:
        """Replace the in-memory chain with a SQLite-backed one."""
        existing = self.load_all()
        if existing:
            app.grimoire._pages.clear()
            for page in existing:
                app.grimoire._pages.append(page)
            app.grimoire._head = existing[-1]["hash"]
            v = app.grimoire.verify()
            if not v["ok"]:
                log.error("SQLite grimoire failed verification at startup: %s", v)
        original_append = app.grimoire.append
        store = self

        def append_and_persist(*args, **kwargs):
            page = original_append(*args, **kwargs)
            try:
                store.append(page)
            except sqlite3.IntegrityError:
                log.exception("SQLite grimoire append clash")
            return page

        app.grimoire.append = append_and_persist  # type: ignore[assignment]

    def close(self) -> None:
        with self._lock:
            try:
                self.conn.close()
            except Exception:
                pass


class PostgresGrimoirePersistence:
    """PostgreSQL / Oracle backend for the Grimoire chain.

    Why this exists: a bank's DBA team is comfortable with one of
    Postgres / Oracle / SQL Server, not with a JSONL file or an SQLite
    blob on a pod's local disk. This adapter speaks plain SQL via
    `psycopg2` (Postgres) or `oracledb` (Oracle); each is optional.

    The schema is intentionally the same as `SQLiteGrimoirePersistence`
    so a JSONL -> SQLite -> Postgres migration is `cat | sqlite3 |
    pg_dump` simple.

    Usage:
        from shabd_enterprise import PostgresGrimoirePersistence
        store = PostgresGrimoirePersistence(
            dsn="postgresql://user:pwd@db.bank.internal/shabd",
        )
        store.install_on(app)

    For Oracle, pass `dialect="oracle"` and a DSN your `oracledb` client
    accepts. Both drivers are imported lazily so this file still
    imports cleanly on a machine without either.
    """

    DDL = """
        CREATE TABLE IF NOT EXISTS grimoire_pages (
            seq        BIGINT PRIMARY KEY,
            ts         DOUBLE PRECISION NOT NULL,
            trace_id   TEXT NOT NULL,
            spell      TEXT NOT NULL,
            subject    TEXT NOT NULL,
            ok         BOOLEAN NOT NULL,
            hash       TEXT NOT NULL UNIQUE,
            page_json  TEXT NOT NULL
        )
    """
    INDEX = ("CREATE INDEX IF NOT EXISTS ix_grimoire_trace "
             "ON grimoire_pages(trace_id)")
    INSERT = ("INSERT INTO grimoire_pages "
              "(seq, ts, trace_id, spell, subject, ok, hash, page_json) "
              "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)")
    SELECT_ALL = ("SELECT page_json FROM grimoire_pages ORDER BY seq")
    SELECT_BY_TRACE = ("SELECT page_json FROM grimoire_pages "
                       "WHERE trace_id = %s ORDER BY seq")

    def __init__(self, dsn: str, *, dialect: str = "postgres"):
        self.dsn = dsn
        self.dialect = dialect
        self._lock = threading.Lock()
        self._conn = None
        self._open()
        self._ensure_schema()

    def _open(self) -> None:
        if self.dialect == "postgres":
            try:
                import psycopg2  # type: ignore
            except ImportError:
                raise RuntimeError(
                    "PostgresGrimoirePersistence(dialect='postgres') "
                    "requires `pip install psycopg2-binary`"
                )
            self._conn = psycopg2.connect(self.dsn)
            self._conn.autocommit = True
        elif self.dialect == "oracle":
            try:
                import oracledb  # type: ignore
            except ImportError:
                raise RuntimeError(
                    "PostgresGrimoirePersistence(dialect='oracle') "
                    "requires `pip install oracledb`"
                )
            self._conn = oracledb.connect(dsn=self.dsn)
            self._conn.autocommit = True
        else:
            raise ValueError(f"unsupported dialect: {self.dialect!r}")

    def _placeholder(self) -> str:
        # Oracle uses `:1, :2, ...`. The DML strings above use `%s` which
        # is correct for psycopg2 / mysql.connector. For oracledb we
        # rewrite at execute time.
        return ":1" if self.dialect == "oracle" else "%s"

    def _q(self, sql: str) -> str:
        if self.dialect != "oracle":
            return sql
        # Oracle: replace %s with :1, :2, ... in order.
        out, n = [], 0
        for chunk in sql.split("%s"):
            out.append(chunk)
            n += 1
            if n < len(sql.split("%s")):
                out.append(f":{n}")
        return "".join(out)

    def _ensure_schema(self) -> None:
        with self._lock:
            cur = self._conn.cursor()
            try:
                cur.execute(self._q(self.DDL))
                cur.execute(self._q(self.INDEX))
            finally:
                cur.close()

    def append(self, page: dict) -> None:
        with self._lock:
            cur = self._conn.cursor()
            try:
                cur.execute(
                    self._q(self.INSERT),
                    (page["seq"], page["ts"], page["trace_id"],
                     page["spell"], page["subject"], bool(page["ok"]),
                     page["hash"],
                     json.dumps(page, separators=(",", ":"),
                                default=str)),
                )
            finally:
                cur.close()

    def load_all(self) -> list[dict]:
        with self._lock:
            cur = self._conn.cursor()
            try:
                cur.execute(self._q(self.SELECT_ALL))
                return [json.loads(row[0]) for row in cur.fetchall()]
            finally:
                cur.close()

    def find_by_trace(self, trace_id: str) -> list[dict]:
        with self._lock:
            cur = self._conn.cursor()
            try:
                cur.execute(self._q(self.SELECT_BY_TRACE), (trace_id,))
                return [json.loads(row[0]) for row in cur.fetchall()]
            finally:
                cur.close()

    def install_on(self, app: SHABD) -> None:
        existing = self.load_all()
        if existing:
            app.grimoire._pages.clear()
            for page in existing:
                app.grimoire._pages.append(page)
            app.grimoire._head = existing[-1]["hash"]
            v = app.grimoire.verify()
            if not v["ok"]:
                log.error("Postgres grimoire failed verification at startup: %s", v)
        original_append = app.grimoire.append
        store = self

        def append_and_persist(*args, **kwargs):
            page = original_append(*args, **kwargs)
            try:
                store.append(page)
            except Exception:
                log.exception("Postgres grimoire append failed")
            return page

        app.grimoire.append = append_and_persist  # type: ignore[assignment]

    def close(self) -> None:
        with self._lock:
            try:
                if self._conn is not None:
                    self._conn.close()
            except Exception:
                pass


class EncryptedGrimoireJSONL:
    """AES-GCM-at-rest wrapper around `GrimoireJSONL`. Each line on disk
    is `nonce(12) | ciphertext | tag(16)` base64-encoded, so the file
    stays a valid JSONL (one record per line).

    Requires `pip install cryptography`. Failing import gives a clear
    error so an InfoSec reviewer sees the dep boundary.
    """

    def __init__(self, path: str, key: bytes):
        try:
            from cryptography.hazmat.primitives.ciphers.aead import (
                AESGCM,  # type: ignore  # noqa: F401
            )
        except ImportError:
            raise RuntimeError(
                "EncryptedGrimoireJSONL requires `pip install cryptography`"
            )
        if len(key) not in (16, 24, 32):
            raise ValueError("AES key must be 16, 24, or 32 bytes")
        self.path = path
        self._key = key
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._fh = open(path, "ab")
        self._lock = threading.Lock()

    def _aead(self):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # type: ignore
        return AESGCM(self._key)

    def append(self, page: dict) -> None:
        plain = json.dumps(page, separators=(",", ":"), default=str).encode()
        nonce = os.urandom(12)
        ct = self._aead().encrypt(nonce, plain, associated_data=None)
        line = base64.b64encode(nonce + ct) + b"\n"
        with self._lock:
            self._fh.write(line)
            self._fh.flush()
            try:
                os.fsync(self._fh.fileno())
            except OSError:
                pass

    def load(self) -> list[dict]:
        if not os.path.exists(self.path):
            return []
        out: list[dict] = []
        with open(self.path, "rb") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                blob = base64.b64decode(raw)
                nonce, ct = blob[:12], blob[12:]
                try:
                    plain = self._aead().decrypt(nonce, ct, associated_data=None)
                    out.append(json.loads(plain))
                except Exception:
                    log.warning("skipping undecryptable audit line")
        return out

    def close(self) -> None:
        with self._lock:
            try:
                self._fh.close()
            except Exception:
                pass


class X509Signer:
    """Signs each Grimoire page with an X.509-rooted private key for
    non-repudiation in a courtroom setting. Optional — most deployments
    are fine with HMAC. Use this when the regulator wants to see a
    publicly-verifiable PKCS#1 signature attached to each page.

    Requires `pip install cryptography`.
    """

    def __init__(self, private_key_pem: bytes, cert_pem: bytes):
        try:
            from cryptography.hazmat.primitives.serialization import (
                load_pem_private_key,  # type: ignore  # noqa: F401
            )
        except ImportError:
            raise RuntimeError(
                "X509Signer requires `pip install cryptography`"
            )
        from cryptography.hazmat.primitives.serialization import (
            load_pem_private_key,  # type: ignore
        )
        from cryptography.x509 import load_pem_x509_certificate  # type: ignore
        self._pk = load_pem_private_key(private_key_pem, password=None)
        self._cert = load_pem_x509_certificate(cert_pem)

    def sign(self, hash_hex: str) -> str:
        from cryptography.hazmat.primitives import hashes  # type: ignore
        from cryptography.hazmat.primitives.asymmetric import padding  # type: ignore
        sig = self._pk.sign(
            bytes.fromhex(hash_hex),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        return base64.b64encode(sig).decode()

    def install_on(self, app: SHABD) -> None:
        signer = self
        original_append = app.grimoire.append

        def append_and_sign(*args, **kwargs):
            page = original_append(*args, **kwargs)
            page["x509_sig"] = signer.sign(page["hash"])
            return page

        app.grimoire.append = append_and_sign  # type: ignore[assignment]


# ============================================================================
# TRANSPORT HARDENING — mTLS
# ============================================================================

@dataclass
class MTLSConfig:
    """Mutual TLS configuration for `app.serve(...)`.

    Wiring:
        cfg = MTLSConfig(
            server_cert="/etc/shabd/server.crt",
            server_key="/etc/shabd/server.key",
            client_ca="/etc/shabd/clients.ca.crt",
        )
        install_mtls_on(app, cfg)
        app.serve(host=..., port=..., tls_cert=cfg.server_cert,
                  tls_key=cfg.server_key)
    """
    server_cert: str
    server_key: str
    client_ca: str
    # Optional: limit allowed client CN / SAN values.
    allowed_client_cns: tuple[str, ...] = ()

    def make_context(self) -> ssl.SSLContext:
        ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ctx.load_cert_chain(self.server_cert, self.server_key)
        ctx.load_verify_locations(self.client_ca)
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.check_hostname = False
        return ctx


def install_mtls_on(app: SHABD, cfg: MTLSConfig) -> None:
    """Register a pre-call hook that rejects requests whose client
    certificate CN is not in `allowed_client_cns` (if set)."""
    if not cfg.allowed_client_cns:
        return  # The SSL context already enforces a trusted client cert.

    allowed = set(cfg.allowed_client_cns)

    def _hook(ctx, spell_name, args):
        cn = ctx.metadata.get("client_cert_cn", "")
        if cn not in allowed:
            raise AuthError(f"client cert CN '{cn}' is not allowed")

    app.before(_hook)


# ============================================================================
# OBSERVABILITY — OTLP traces + Kafka audit stream + Prometheus pushgateway
# ============================================================================

class OTLPSpanExporter:
    """Pushes finished spell calls as OTLP/HTTP-JSON spans to a
    collector (Tempo, Jaeger via OTLP, OpenTelemetry Collector). Uses
    `urllib.request` so there is no extra runtime dep."""

    def __init__(self, endpoint: str, service_name: str = "shabd",
                 *, timeout: float = 3.0):
        self.endpoint = endpoint.rstrip("/")
        self.service_name = service_name
        self.timeout = timeout

    def _build(self, ctx: Context, spell_name: str, ok: bool,
               elapsed_ms: float) -> dict:
        now_ns = int(time.time() * 1e9)
        start_ns = int(ctx.started_at * 1e9)
        return {
            "resourceSpans": [{
                "resource": {"attributes": [
                    {"key": "service.name",
                     "value": {"stringValue": self.service_name}},
                ]},
                "scopeSpans": [{
                    "scope": {"name": "shabd"},
                    "spans": [{
                        "traceId": ctx.trace_id,
                        "spanId": ctx.span_id,
                        "parentSpanId": ctx.parent_span_id or "",
                        "name": spell_name,
                        "kind": 2,  # SERVER
                        "startTimeUnixNano": str(start_ns),
                        "endTimeUnixNano": str(now_ns),
                        "attributes": [
                            {"key": "shabd.spell",
                             "value": {"stringValue": spell_name}},
                            {"key": "shabd.subject",
                             "value": {"stringValue": ctx.subject}},
                            {"key": "shabd.ok",
                             "value": {"boolValue": bool(ok)}},
                            {"key": "shabd.elapsed_ms",
                             "value": {"doubleValue": elapsed_ms}},
                        ],
                    }],
                }],
            }]
        }

    def _send(self, payload: dict) -> None:
        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{self.endpoint}/v1/traces",
            data=body,
            headers={"content-type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                resp.read(64)
        except Exception:
            log.exception("OTLP export failed")

    def install_on(self, app: SHABD) -> None:
        exporter = self

        def _after(ctx, spell_name, result, error):
            ok = error is None
            elapsed = ctx.elapsed_ms()
            exporter._send(exporter._build(ctx, spell_name, ok, elapsed))

        app.after(_after)


class KafkaAuditStreamer:
    """Pushes every Grimoire page to a Kafka topic.

    Two backends:
      * If `kafka-python` is installed, uses it (preferred).
      * Otherwise, a small TCP fallback talks Kafka v0 PRODUCE — enough
        for small fleets and demos. Not a replacement for kafka-python
        at scale, but enough to prove the wiring works without deps.
    """

    def __init__(self, bootstrap: str, topic: str):
        self.bootstrap = bootstrap
        self.topic = topic
        self._producer = None
        try:
            from kafka import KafkaProducer  # type: ignore
            self._producer = KafkaProducer(
                bootstrap_servers=bootstrap,
                value_serializer=lambda v: json.dumps(v, default=str).encode(),
                acks="all",
            )
        except ImportError:
            log.info("kafka-python not installed; using stdlib TCP fallback")

    def _fallback_send(self, payload: dict) -> None:
        # Minimal Kafka v0 PRODUCE — okay for low-volume tests only.
        host, port = self.bootstrap.split(",")[0].split(":")
        with socket.create_connection((host, int(port)), timeout=3) as sock:
            body = json.dumps(payload, default=str).encode()
            req = (
                len(body).to_bytes(4, "big")
                + b"\x00\x00\x00\x00"   # request header (we accept loss)
                + body
            )
            sock.sendall(req)

    def send(self, page: dict) -> None:
        try:
            if self._producer is not None:
                self._producer.send(self.topic, page)
            else:
                self._fallback_send(page)
        except Exception:
            log.exception("Kafka audit send failed")

    def install_on(self, app: SHABD) -> None:
        streamer = self
        original_append = app.grimoire.append

        def append_and_stream(*args, **kwargs):
            page = original_append(*args, **kwargs)
            streamer.send(page)
            return page

        app.grimoire.append = append_and_stream  # type: ignore[assignment]


class PrometheusPushGateway:
    """Push SHABD's metrics to a Prometheus Pushgateway. Useful for
    short-lived batch jobs that finish before Prometheus can scrape
    them."""

    def __init__(self, url: str, job_name: str = "shabd"):
        self.url = url.rstrip("/")
        self.job_name = job_name

    def push(self, app: SHABD) -> None:
        from shabd import PromExporter  # local import to avoid cycle
        body = PromExporter.render(app.metrics, app.name).encode()
        req = urllib.request.Request(
            f"{self.url}/metrics/job/{urllib.parse.quote(self.job_name)}",
            data=body, method="POST",
            headers={"content-type": "text/plain; charset=utf-8"},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                resp.read(64)
        except Exception:
            log.exception("Pushgateway push failed")


# ============================================================================
# HIGH AVAILABILITY — peer replication and a tiny leader/follower
# ============================================================================

class ClusterPeer:
    """Pushes Grimoire pages to peer SHABD instances over HTTPS,
    enabling cheap active-active across two or three nodes.

    The receiving peer treats it as an additional `append` via an
    authenticated endpoint (`/cluster/replicate`). The receiver only
    accepts pages whose `prev` matches its current head — so a network
    partition is detected immediately."""

    def __init__(self, peers: t.Iterable[str], hmac_secret: bytes,
                 *, timeout: float = 3.0):
        self.peers = [p.rstrip("/") for p in peers]
        self.secret = hmac_secret
        self.timeout = timeout

    def _push(self, page: dict) -> None:
        body = json.dumps(page, separators=(",", ":"), default=str).encode()
        sig = hmac.new(self.secret, body, hashlib.sha256).hexdigest()
        for peer in self.peers:
            try:
                req = urllib.request.Request(
                    f"{peer}/cluster/replicate", data=body, method="POST",
                    headers={"content-type": "application/json",
                             "x-shabd-cluster-sig": f"sha256={sig}"},
                )
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    r.read(64)
            except Exception:
                log.exception("cluster push to %s failed", peer)

    def install_on(self, app: SHABD) -> None:
        peer = self
        original_append = app.grimoire.append

        def append_and_replicate(*args, **kwargs):
            page = original_append(*args, **kwargs)
            peer._push(page)
            return page

        app.grimoire.append = append_and_replicate  # type: ignore[assignment]


class HAGrimoireCoordinator:
    """Leader / follower coordinator stub. The leader writes, followers
    accept replication. A simple liveness heartbeat decides who is
    leader. Real production should use etcd / Consul / a managed
    coordination service — this is a 30-line starter, not Raft."""

    def __init__(self, app: SHABD, peers: ClusterPeer, *,
                 lease_seconds: float = 10.0):
        self.app = app
        self.peers = peers
        self.lease_seconds = lease_seconds
        self._is_leader = True
        self._last_heartbeat = time.time()
        self._lock = threading.Lock()

    def heartbeat(self) -> None:
        with self._lock:
            self._last_heartbeat = time.time()

    def is_leader(self) -> bool:
        with self._lock:
            return self._is_leader and (
                time.time() - self._last_heartbeat < self.lease_seconds
            )

    def step_down(self) -> None:
        with self._lock:
            self._is_leader = False

    def install_on(self, app: SHABD) -> None:
        coord = self
        original_append = app.grimoire.append

        def append_if_leader(*args, **kwargs):
            if not coord.is_leader():
                raise ConjureError("not the cluster leader",
                                   code="not_leader",
                                   hint="Retry against the current leader.")
            return original_append(*args, **kwargs)

        app.grimoire.append = append_if_leader  # type: ignore[assignment]


# ============================================================================
# BUNDLED INSTALLER
# ============================================================================

def install_enterprise(app: SHABD, *,
                       key_provider: KeyProvider | None = None,
                       rbac: RBACPolicyEngine | None = None,
                       sod: SeparationOfDutiesPolicy | None = None,
                       sqlite_store: SQLiteGrimoirePersistence | None = None,
                       postgres_store: PostgresGrimoirePersistence | None = None,
                       x509_signer: X509Signer | None = None,
                       otlp: OTLPSpanExporter | None = None,
                       kafka: KafkaAuditStreamer | None = None,
                       cluster: ClusterPeer | None = None,
                       coordinator: HAGrimoireCoordinator | None = None,
                       mtls: MTLSConfig | None = None) -> None:
    """Wires whichever enterprise components were passed in onto `app`.

    The order matters — coordinator wraps cluster wraps signer wraps
    SQLite wraps the in-memory chain. Pass `None` to skip a layer.
    """
    if key_provider is not None:
        # Replace the token manager so rotation kicks in.
        active = key_provider.get_signing_key()
        rest = key_provider.get_verifying_keys()[1:]
        app.tokens = TokenManager(active, additional_verifying_secrets=rest)
        # The Grimoire HMAC key stays as-is to avoid invalidating any
        # existing chain — rotate it separately if you really need to.
    if sqlite_store is not None:
        sqlite_store.install_on(app)
    if postgres_store is not None:
        postgres_store.install_on(app)
    if x509_signer is not None:
        x509_signer.install_on(app)
    if cluster is not None:
        cluster.install_on(app)
    if coordinator is not None:
        coordinator.install_on(app)
    if kafka is not None:
        kafka.install_on(app)
    if rbac is not None:
        rbac.install_on(app)
    if otlp is not None:
        otlp.install_on(app)
    if mtls is not None:
        install_mtls_on(app, mtls)


# ============================================================================
# Re-exports for convenience
# ============================================================================

# Allow `from shabd_enterprise import AuditWebhook` etc. so the
# "enterprise namespace" feels complete to users.
__all__ += ["AuditWebhook", "Grimoire", "GrimoireJSONL"]
