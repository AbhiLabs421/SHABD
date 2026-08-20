"""
shabd.py — v2.1  |  The Complete AI-Function Framework
=======================================================
A single-file, zero-dependency framework for turning Python functions into
tools that AI agents can call — with built-in security, caching, rate limiting,
an HTTP server, a live dashboard, full MCP support, *and* three features
that nobody else has shipped yet:

  * Grimoire        — cryptographically hash-chained, tamper-evident audit log
  * Semantic Types  — Email / Aadhaar / GSTIN / IndianPhone / Money / URL
                      with built-in validation and PII auto-redaction
  * AI-Native Errors — every error returns a `hint`, `example` and a
                      `did_you_mean` suggestion so the calling LLM can
                      self-correct without a human in the loop

NEW in v2.0 vs v1.x:
  ✅ MCP Resources   — @app.resource() se files/data expose karo
  ✅ MCP Prompts     — @app.prompt() se reusable prompt templates
  ✅ Image/File      — SpellImage / SpellFile return types
  ✅ Python 3.10+    — 3.11 ka koi restriction nahi
  ✅ SHABD branding  — Consistent naming (SHABD = Conjure alias)
  🚀 Spell Chains    — "spell1 | spell2 | spell3" pipeline
  🚀 YAML Spells     — Python likhe bina YAML se tools define karo
  🚀 Hot Reload      — Spells change pe auto-reload, no restart
  🚀 Playground      — Dashboard mein live spell testing UI
  🚀 CPM Native      — app.cpm_config() → CPM project YAML auto-gen
  🚀 Replay          — /replay/{trace_id} se failed calls dobara chalao
  🚀 Multi-Project   — app.group("finance") se namespaced subgroups

Quick start:
    from shabd import SHABD

    app = SHABD("my-tools", secret="change-me")

    @app.spell
    def add(a: int, b: int) -> int:
        '''Add two numbers.'''
        return a + b

    @app.resource("/docs/{slug}")
    def get_doc(slug: str) -> str:
        '''Return a documentation page.'''
        return open(f"docs/{slug}.md").read()

    @app.prompt("code_review")
    def review_prompt(language: str = "python") -> str:
        '''Code review ke liye system prompt.'''
        return f"Review this {language} code..."

    app.serve(port=8765)

Spell Chain:
    app.chain("search_docs | summarize | translate",
              name="search_and_translate")

YAML Spells (config.yaml):
    yaml_spells:
      - name: get_weather
        url: "https://wttr.in/{city}?format=j1"
        method: GET
        params:
          city: {type: string, required: true}

Hot Reload:
    app.serve(port=8765, hot_reload="my_spells.py",
              reload_fn=register_spells)

Multi-Project:
    finance = app.group("finance")
    @finance.spell
    def calc_tax(amount: float) -> float: ...

CPM Config:
    yaml_str = app.cpm_config()

Author: Built for the CPM project. License: do whatever you want, no warranty.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import importlib.util
import inspect
import json
import logging
import os
import re as _re
import secrets
import struct
import subprocess as _subprocess
import sys
import threading
import time
import traceback
import typing as t
import urllib.parse
import urllib.request
import uuid
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field

__version__ = "2.2.0"
__all__ = [
    # Main class (two names for compatibility)
    "SHABD", "Conjure",
    # Plugins
    "ConjurePlugin", "RedisPlugin",
    # Errors
    "ConjureError", "AuthError", "RateLimitError",
    "ValidationError", "SpellNotFoundError", "ForbiddenError",
    # Data types
    "Context", "SpellImage", "SpellFile",
    # Semantic types — first-class meaning in the schema, PII auto-redacted
    "Email", "IndianPhone", "GSTIN", "Aadhaar", "Money", "URL",
    # Sub-systems
    "Grimoire", "MCPClient", "ProjectGroup", "ConfigLoader",
    # Enterprise (v2.2)
    "IdempotencyStore", "GrimoireJSONL", "PromExporter",
    "TraceContextCodec", "AuditWebhook",
]


# ============================================================================
# SECTION 1 — EXCEPTIONS
# ============================================================================

class ConjureError(Exception):
    status = 500
    code = "internal_error"

    def __init__(self, message: str, *, hint: str | None = None,
                 did_you_mean: t.Any = None, example: t.Any = None,
                 **details):
        super().__init__(message)
        self.message = message
        if hint is not None:
            details["hint"] = hint
        if did_you_mean is not None:
            details["did_you_mean"] = did_you_mean
        if example is not None:
            details["example"] = example
        self.details = details

    def to_dict(self) -> dict:
        return {"error": {"code": self.code, "message": self.message, **self.details}}


class AuthError(ConjureError):
    status = 401
    code = "unauthorized"


class ForbiddenError(ConjureError):
    status = 403
    code = "forbidden"


class RateLimitError(ConjureError):
    status = 429
    code = "rate_limited"


class ValidationError(ConjureError):
    status = 400
    code = "validation_failed"


class SpellNotFoundError(ConjureError):
    status = 404
    code = "spell_not_found"


class TimeoutError_(ConjureError):
    status = 504
    code = "timeout"


# ============================================================================
# SECTION 2 — NEW: RETURN TYPES (Image + File)
# ============================================================================

@dataclass
class SpellImage:
    """
    Spell se image return karo — Claude Desktop mein directly render hogi.

    Usage:
        @app.spell
        def generate_chart(data: list) -> SpellImage:
            buf = io.BytesIO()
            plt.savefig(buf, format='png')
            return SpellImage(data=buf.getvalue(), mime_type="image/png")
    """
    data: bytes
    mime_type: str = "image/png"
    alt_text: str = ""

    def to_base64(self) -> str:
        return base64.b64encode(self.data).decode("ascii")


@dataclass
class SpellFile:
    """
    Spell se downloadable file return karo.

    Usage:
        @app.spell
        def export_csv(rows: list) -> SpellFile:
            content = "col1,col2\\n" + "\\n".join(...)
            return SpellFile(data=content.encode(), name="export.csv",
                             mime_type="text/csv")
    """
    data: bytes
    name: str = "output"
    mime_type: str = "application/octet-stream"

    def to_base64(self) -> str:
        return base64.b64encode(self.data).decode("ascii")


# ============================================================================
# SECTION 2b — SEMANTIC TYPES
# ============================================================================
# These behave like `str` but carry meaning in the JSON schema (so an LLM
# knows it is asking for an Aadhaar, not just a string) and are flagged as
# PII so the audit log auto-redacts them.

class _SemanticType(str):
    """Base for typed strings with a JSON-schema hint and PII flag."""
    _semantic_name: str = "string"
    _semantic_format: str = ""
    _is_pii: bool = False
    _pattern: str | None = None
    _example: str = ""

    def __new__(cls, value):
        if not isinstance(value, str):
            value = str(value)
        if cls._pattern and not _re.match(cls._pattern, value):
            raise ValidationError(
                f"invalid {cls._semantic_name}: {value!r}",
                hint=f"Expected format: {cls._semantic_format}",
                example=cls._example,
            )
        return str.__new__(cls, value)


class Email(_SemanticType):
    """RFC-5322-ish email. PII."""
    _semantic_name = "email"
    _semantic_format = "user@domain.tld"
    _is_pii = True
    _pattern = r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$"
    _example = "alice@example.com"


class IndianPhone(_SemanticType):
    """Indian mobile number; +91 / 0 prefix allowed, must start with 6-9. PII."""
    _semantic_name = "indian_phone"
    _semantic_format = "10 digits, optional +91 / 0 prefix"
    _is_pii = True
    _pattern = r"^(\+91|0)?[6-9]\d{9}$"
    _example = "+919876543210"


class GSTIN(_SemanticType):
    """Indian Goods & Services Tax Identification Number (15 chars)."""
    _semantic_name = "gstin"
    _semantic_format = "15-char GSTIN"
    _is_pii = False
    _pattern = r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$"
    _example = "27AAPFU0939F1ZV"


class Aadhaar(_SemanticType):
    """Indian Aadhaar number (12 digits). High-PII — always redacted."""
    _semantic_name = "aadhaar"
    _semantic_format = "12 digits"
    _is_pii = True
    _pattern = r"^\d{12}$"
    _example = "234517895432"


class Money(_SemanticType):
    """Money as 'AMOUNT CCY' (ISO-4217 currency code) — e.g. '1500.00 INR'."""
    _semantic_name = "money"
    _semantic_format = "'AMOUNT CCY' e.g. '1500.00 INR'"
    _is_pii = False
    _pattern = r"^\d+(\.\d{1,2})?\s+[A-Z]{3}$"
    _example = "1500.00 INR"


class URL(_SemanticType):
    """HTTP(S) URL."""
    _semantic_name = "url"
    _semantic_format = "http(s)://..."
    _is_pii = False
    _pattern = r"^https?://[^\s]+$"
    _example = "https://example.com"


_SEMANTIC_TYPES: tuple[type, ...] = (
    Email, IndianPhone, GSTIN, Aadhaar, Money, URL,
)


def _mask_value(value: t.Any) -> str:
    """Replace middle of a value with stars for audit logs."""
    s = str(value)
    if len(s) <= 4:
        return "***"
    return s[:2] + "*" * (len(s) - 4) + s[-2:]


def _redact_for_audit(args: t.Any, schema: dict | None = None) -> t.Any:
    """Recursively redact any value whose schema marks `x-pii: true`."""
    if schema and schema.get("x-pii") and args is not None:
        return _mask_value(args)
    if isinstance(args, dict):
        props = (schema or {}).get("properties", {})
        return {k: _redact_for_audit(v, props.get(k, {})) for k, v in args.items()}
    if isinstance(args, list):
        item_schema = (schema or {}).get("items", {})
        return [_redact_for_audit(v, item_schema) for v in args]
    return args


# ============================================================================
# SECTION 3 — PLUGIN SYSTEM
# ============================================================================

class ConjurePlugin:
    """Base class for SHABD plugins. Override any hook you need."""

    def on_startup(self, app: Conjure):
        """Server start pe call hota hai."""

    def on_call_start(self, ctx: Context, spell_name: str, args: dict):
        """Har spell call se pehle. ConjureError raise karo to abort."""

    def on_call_end(self, ctx: Context, spell_name: str, args: dict,
                    result: t.Any, error: BaseException | None,
                    elapsed_ms: float):
        """Spell success/fail dono ke baad."""

    def on_spell_registered(self, spell: Spell):
        """Naya spell register hone pe."""

    def check_rate_limit(self, key: str, rate: int,
                         window: float) -> tuple[bool, int] | None:
        return None

    def cache_get(self, key: str) -> t.Any:
        return _MISS

    def cache_set(self, key: str, value: t.Any, ttl: int):
        pass

    def close(self):
        pass


_MISS = object()


# ============================================================================
# SECTION 4 — LOGGING
# ============================================================================

@dataclass
class CallRecord:
    trace_id: str
    spell: str
    subject: str
    transport: str
    args_size: int
    ok: bool
    elapsed_ms: float
    error_code: str | None
    args_snapshot: dict | None = None   # replay ke liye
    ts: float = field(default_factory=time.time)


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {"ts": time.time(), "level": record.levelname,
                   "msg": record.getMessage(), "logger": record.name}
        for k in ("trace_id", "spell", "subject", "extra"):
            if hasattr(record, k):
                v = getattr(record, k)
                if k == "extra" and isinstance(v, dict):
                    payload.update(v)
                else:
                    payload[k] = v
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def _get_logger(name: str = "shabd") -> logging.Logger:
    log = logging.getLogger(name)
    if not log.handlers:
        h = logging.StreamHandler(sys.stderr)
        h.setFormatter(_JsonFormatter())
        log.addHandler(h)
        log.setLevel(os.environ.get("SHABD_LOG_LEVEL", "INFO"))
        log.propagate = False
    return log


log = _get_logger()


# ============================================================================
# SECTION 5 — SECURITY
# ============================================================================

def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    pad = 4 - (len(data) % 4)
    return base64.urlsafe_b64decode(data + ("=" * pad))


class TokenManager:
    """
    HMAC token issuer / verifier with optional multi-key verification.

    Pass `additional_verifying_secrets=[old_key]` to accept tokens signed
    with a previous key while issuing new ones with the current key —
    zero-downtime secret rotation.
    """

    def __init__(self, secret: bytes,
                 additional_verifying_secrets: t.Iterable[bytes] = ()):
        if not secret or len(secret) < 16:
            raise ValueError("SHABD secret must be >= 16 bytes.")
        self._secret = secret  # used for signing new tokens
        # All keys we'll accept on verify (newest first):
        self._verifying = [secret] + [s for s in additional_verifying_secrets
                                      if s and len(s) >= 16]
        self._seen_jti: deque = deque(maxlen=10_000)
        self._jti_lock = threading.Lock()

    def issue(self, subject: str, scopes: t.Iterable[str] = (),
              ttl: int = 3600) -> str:
        now = int(time.time())
        payload = {"sub": subject, "scopes": list(scopes),
                   "iat": now, "exp": now + int(ttl),
                   "jti": secrets.token_hex(8)}
        body = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode())
        sig = hmac.new(self._secret, body.encode(), hashlib.sha256).digest()
        return f"{body}.{_b64url_encode(sig)}"

    def verify(self, token: str) -> dict:
        if not token or token.count(".") != 1:
            raise AuthError("malformed token")
        body_b64, sig_b64 = token.split(".", 1)
        try:
            given = _b64url_decode(sig_b64)
        except Exception:
            raise AuthError("invalid signature encoding")
        # Try every verifying secret — first match wins (constant-time).
        valid = False
        for key in self._verifying:
            expected = hmac.new(key, body_b64.encode(), hashlib.sha256).digest()
            if hmac.compare_digest(expected, given):
                valid = True
                break
        if not valid:
            raise AuthError("invalid signature")
        try:
            payload = json.loads(_b64url_decode(body_b64))
        except Exception:
            raise AuthError("invalid payload")
        if payload.get("exp", 0) < time.time():
            raise AuthError("token expired")
        jti = payload.get("jti", "")
        with self._jti_lock:
            now = time.time()
            while self._seen_jti and self._seen_jti[0][0] < now - 3600:
                self._seen_jti.popleft()
            for _, seen in self._seen_jti:
                if hmac.compare_digest(seen, jti):
                    raise AuthError("token replay detected")
            self._seen_jti.append((now, jti))
        return payload


# ============================================================================
# SECTION 6 — RATE LIMITER
# ============================================================================

class Grimoire:
    """
    Cryptographically signed, hash-chained audit log of every spell cast.

    Each page (call record) commits to the previous page's hash, so the chain
    is tamper-evident: altering any past page breaks every page that follows.
    Each page is also HMAC-signed with the app secret, so an outside reader
    cannot forge new pages without the secret.

    Properties:
      * append-only (pages are immutable once written)
      * tamper-evident (chain breaks on any edit)
      * verifiable in O(n)
      * PII-safe — schema-driven redaction before hashing

    This is genuinely unique among MCP / tool frameworks today.

        app.grimoire.verify()              -> {"ok": True, "pages": ...}
        app.grimoire.pages(since_seq=0)    -> [page, page, ...]
        app.grimoire.head()                -> latest leaf hash
    """
    GENESIS = "0" * 64

    def __init__(self, secret: bytes, max_pages: int = 100_000):
        self._secret = secret
        self._pages: deque = deque(maxlen=max_pages)
        self._head: str = self.GENESIS
        self._lock = threading.Lock()

    @staticmethod
    def _canonical(obj: t.Any) -> str:
        return json.dumps(obj, sort_keys=True, default=str, separators=(",", ":"))

    @classmethod
    def _sha(cls, obj: t.Any) -> str:
        return hashlib.sha256(cls._canonical(obj).encode()).hexdigest()

    def append(self, *, trace_id: str, spell: str, subject: str,
               args: t.Any, result: t.Any, ok: bool,
               ts: float | None = None) -> dict:
        with self._lock:
            seq = len(self._pages)
            ts = ts if ts is not None else time.time()
            leaf = {
                "prev": self._head,
                "seq": seq,
                "ts": ts,
                "trace_id": trace_id,
                "spell": spell,
                "subject": subject,
                "args_hash": self._sha(args),
                "result_hash": self._sha(result),
                "ok": bool(ok),
            }
            leaf_hash = self._sha(leaf)
            signature = hmac.new(
                self._secret, leaf_hash.encode(), hashlib.sha256
            ).hexdigest()
            page = {**leaf, "hash": leaf_hash, "sig": signature}
            self._pages.append(page)
            self._head = leaf_hash
            return page

    def head(self) -> str:
        return self._head

    def pages(self, since_seq: int = 0, limit: int = 100) -> list[dict]:
        if since_seq <= 0 and limit >= len(self._pages):
            return list(self._pages)
        return [p for p in self._pages if p["seq"] >= since_seq][:limit]

    def verify(self) -> dict:
        prev = self.GENESIS
        for page in self._pages:
            leaf = {k: page[k] for k in (
                "prev", "seq", "ts", "trace_id", "spell",
                "subject", "args_hash", "result_hash", "ok",
            )}
            if leaf["prev"] != prev:
                return {"ok": False, "reason": "chain break",
                        "at_seq": page["seq"]}
            if self._sha(leaf) != page["hash"]:
                return {"ok": False, "reason": "hash mismatch",
                        "at_seq": page["seq"]}
            expected_sig = hmac.new(
                self._secret, page["hash"].encode(), hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(expected_sig, page["sig"]):
                return {"ok": False, "reason": "bad signature",
                        "at_seq": page["seq"]}
            prev = page["hash"]
        return {"ok": True, "pages": len(self._pages), "head": prev}


class RateLimiter:
    def __init__(self, default_rate: int = 100, default_window: float = 60.0):
        self.default_rate = default_rate
        self.default_window = default_window
        self._buckets: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str, rate: int | None = None,
              window: float | None = None) -> tuple[bool, int]:
        rate = rate or self.default_rate
        window = window or self.default_window
        now = time.time()
        with self._lock:
            q = self._buckets[key]
            cutoff = now - window
            while q and q[0] < cutoff:
                q.popleft()
            if len(q) >= rate:
                return False, int(q[0] + window - now) + 1
            q.append(now)
            return True, 0


# ============================================================================
# SECTION 7 — CIRCUIT BREAKER
# ============================================================================

class CircuitBreaker:
    def __init__(self, threshold: int = 5, reset_after: float = 30.0):
        self.threshold = threshold
        self.reset_after = reset_after
        self._failures: dict[str, int] = defaultdict(int)
        self._opened_at: dict[str, float] = {}
        self._lock = threading.Lock()

    def is_open(self, key: str) -> bool:
        with self._lock:
            if key not in self._opened_at:
                return False
            if time.time() - self._opened_at[key] > self.reset_after:
                del self._opened_at[key]
                self._failures[key] = 0
                return False
            return True

    def record_success(self, key: str):
        with self._lock:
            self._failures[key] = 0
            self._opened_at.pop(key, None)

    def record_failure(self, key: str):
        with self._lock:
            self._failures[key] += 1
            if self._failures[key] >= self.threshold:
                self._opened_at[key] = time.time()


# ============================================================================
# SECTION 8 — SCHEMA INTROSPECTION (Python 3.10+ compatible)
# ============================================================================

_PRIMITIVES = {
    str: {"type": "string"},
    int: {"type": "integer"},
    float: {"type": "number"},
    bool: {"type": "boolean"},
    bytes: {"type": "string", "contentEncoding": "base64"},
    type(None): {"type": "null"},
    dict: {"type": "object"},
    list: {"type": "array"},
}


def _type_to_schema(tp: t.Any) -> dict:
    import enum as _enum_mod
    if tp is inspect.Parameter.empty or tp is t.Any:
        return {}
    if tp in _PRIMITIVES:
        return dict(_PRIMITIVES[tp])
    # SpellImage / SpellFile → annotate as base64 string
    if tp is SpellImage:
        return {"type": "object", "description": "Image data (base64)",
                "properties": {"data": {"type": "string"}, "mime_type": {"type": "string"}}}
    if tp is SpellFile:
        return {"type": "object", "description": "File data (base64)",
                "properties": {"data": {"type": "string"}, "name": {"type": "string"}}}
    if isinstance(tp, type) and issubclass(tp, _SemanticType) and tp is not _SemanticType:
        schema: dict = {
            "type": "string",
            "format": tp._semantic_name,
            "x-semantic": tp._semantic_name,
            "x-pii": tp._is_pii,
            "description": tp._semantic_format,
            "example": tp._example,
        }
        if tp._pattern:
            schema["pattern"] = tp._pattern
        return schema
    if isinstance(tp, type) and issubclass(tp, _enum_mod.Enum):
        return {"enum": [m.value for m in tp]}
    _STR_FORMATS: dict = {}
    try:
        from datetime import date, datetime
        from datetime import time as _time
        _STR_FORMATS[datetime] = "date-time"
        _STR_FORMATS[date] = "date"
        _STR_FORMATS[_time] = "time"
    except ImportError:
        pass
    try:
        from uuid import UUID
        _STR_FORMATS[UUID] = "uuid"
    except ImportError:
        pass
    try:
        from pathlib import Path
        _STR_FORMATS[Path] = "path"
    except ImportError:
        pass
    if tp in _STR_FORMATS:
        return {"type": "string", "format": _STR_FORMATS[tp]}
    origin = t.get_origin(tp)
    args = t.get_args(tp)

    # Python 3.10+ types.UnionType (X | Y syntax) bhi handle karo
    try:
        import types as _types
        if isinstance(tp, _types.UnionType):  # type: ignore[attr-defined]
            non_none = [a for a in tp.__args__ if a is not type(None)]
            if len(non_none) == 1 and type(None) in tp.__args__:
                s = _type_to_schema(non_none[0])
                return {"anyOf": [s, {"type": "null"}]}
            return {"anyOf": [_type_to_schema(a) for a in tp.__args__]}
    except AttributeError:
        pass

    if origin is list or origin is list:
        return {"type": "array", "items": _type_to_schema(args[0]) if args else {}}
    if origin is tuple:
        if args:
            return {"type": "array",
                    "prefixItems": [_type_to_schema(a) for a in args],
                    "minItems": len(args), "maxItems": len(args)}
        return {"type": "array"}
    if origin is dict or origin is dict:
        schema: dict = {"type": "object"}
        if args and len(args) == 2:
            val_schema = _type_to_schema(args[1])
            if val_schema:
                schema["additionalProperties"] = val_schema
        return schema
    if origin is t.Union:
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1 and type(None) in args:
            s = _type_to_schema(non_none[0])
            return {"anyOf": [s, {"type": "null"}]}
        return {"anyOf": [_type_to_schema(a) for a in args]}
    if origin is t.Literal:
        return {"enum": list(args)}
    try:
        import dataclasses
        if dataclasses.is_dataclass(tp) and isinstance(tp, type):
            props = {}
            req = []
            for f in dataclasses.fields(tp):
                ann = tp.__annotations__.get(f.name, t.Any)
                props[f.name] = _type_to_schema(ann)
                if (f.default is dataclasses.MISSING
                        and f.default_factory is dataclasses.MISSING):  # type: ignore
                    req.append(f.name)
            return {"type": "object", "properties": props, "required": req}
    except Exception:
        pass
    if hasattr(tp, "__annotations__") and tp.__annotations__:
        return {"type": "object",
                "properties": {k: _type_to_schema(v)
                               for k, v in tp.__annotations__.items()}}
    return {}


def _build_schema(func: t.Callable) -> dict:
    sig = inspect.signature(func)
    try:
        hints = t.get_type_hints(func)
    except Exception:
        hints = {}
    properties: dict = {}
    required: list[str] = []
    for name, param in sig.parameters.items():
        if name in ("self", "cls", "ctx", "context"):
            continue
        if param.kind in (inspect.Parameter.VAR_POSITIONAL,
                          inspect.Parameter.VAR_KEYWORD):
            continue
        schema = _type_to_schema(hints.get(name, param.annotation))
        if param.default is inspect.Parameter.empty:
            required.append(name)
        else:
            schema["default"] = param.default if _is_json_safe(param.default) else None
        properties[name] = schema
    return {"type": "object", "properties": properties,
            "required": required, "additionalProperties": False}


def _is_json_safe(value: t.Any) -> bool:
    try:
        json.dumps(value)
        return True
    except Exception:
        return False


def _validate_against_schema(value: t.Any, schema: dict, path: str = ""):
    if not schema:
        return
    if "anyOf" in schema:
        errors = []
        for sub in schema["anyOf"]:
            try:
                _validate_against_schema(value, sub, path)
                return
            except ValidationError as e:
                errors.append(str(e))
        raise ValidationError(f"{path or 'value'}: matches none of allowed types",
                              tried=errors)
    if "enum" in schema:
        if value not in schema["enum"]:
            raise ValidationError(f"{path or 'value'} must be one of {schema['enum']}")
        return
    expected = schema.get("type")
    if expected == "string":
        if not isinstance(value, str):
            raise ValidationError(
                f"{path or 'value'} must be string",
                hint=f"got {type(value).__name__}, expected string",
                example=schema.get("example"),
            )
        pattern = schema.get("pattern")
        if pattern and not _re.match(pattern, value):
            raise ValidationError(
                f"{path or 'value'} does not match expected format",
                hint=schema.get("description") or f"must match /{pattern}/",
                example=schema.get("example"),
            )
    if expected == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
        raise ValidationError(f"{path or 'value'} must be integer")
    if expected == "number" and not isinstance(value, (int, float)):
        raise ValidationError(f"{path or 'value'} must be number")
    if expected == "boolean" and not isinstance(value, bool):
        raise ValidationError(f"{path or 'value'} must be boolean")
    if expected == "null" and value is not None:
        raise ValidationError(f"{path or 'value'} must be null")
    if expected == "array":
        if not isinstance(value, list):
            raise ValidationError(f"{path or 'value'} must be array")
        item_schema = schema.get("items", {})
        for i, item in enumerate(value):
            _validate_against_schema(item, item_schema, f"{path}[{i}]")
    if expected == "object":
        if not isinstance(value, dict):
            raise ValidationError(f"{path or 'value'} must be object")
        for req in schema.get("required", []):
            if req not in value:
                raise ValidationError(f"{path or 'value'}: missing required '{req}'")
        for k, v in value.items():
            sub = schema.get("properties", {}).get(k)
            if sub:
                _validate_against_schema(v, sub, f"{path}.{k}" if path else k)
            elif schema.get("additionalProperties") is False:
                raise ValidationError(f"{path or 'value'}: unexpected field '{k}'")


# ============================================================================
# SECTION 9 — CONTEXT
# ============================================================================

@dataclass
class Context:
    trace_id: str
    subject: str = "anonymous"
    scopes: list[str] = field(default_factory=list)
    transport: str = "http"
    started_at: float = field(default_factory=time.time)
    metadata: dict = field(default_factory=dict)
    # W3C TraceContext fields:
    span_id: str = field(default_factory=lambda: secrets.token_hex(8))
    parent_span_id: str | None = None
    sampled: bool = True
    # Banking-grade safe retries:
    idempotency_key: str | None = None

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes or "*" in self.scopes

    def elapsed_ms(self) -> float:
        return (time.time() - self.started_at) * 1000

    def traceparent(self) -> str:
        """Emit a W3C traceparent header value for downstream calls."""
        return TraceContextCodec.encode(
            self.trace_id, self.span_id, sampled=self.sampled,
        )


# ============================================================================
# SECTION 10 — CORE DATA CLASSES: Spell, Resource, Prompt, SpellChainDef
# ============================================================================

@dataclass
class Spell:
    name: str
    func: t.Callable
    description: str
    schema: dict
    returns_schema: dict
    is_async: bool
    is_streaming: bool
    wants_context: bool
    scopes: list[str]
    rate_limit: int | None
    rate_window: float
    cache_ttl: int | None
    timeout: float | None
    retries: int
    tags: list[str]
    group: str = ""  # multi-project ke liye
    max_concurrent: int | None = None
    idempotent: bool = False  # safe to retry without an Idempotency-Key
    _semaphore: t.Any = field(default=None, repr=False, compare=False)

    def to_manifest_entry(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.schema,
            "output_schema": self.returns_schema,
            "streaming": self.is_streaming,
            "async": self.is_async,
            "scopes": self.scopes,
            "tags": self.tags,
            "group": self.group,
        }


@dataclass
class Resource:
    """
    MCP Resource — files, database rows, API data expose karo.

    @app.resource("/docs/{slug}")
    def get_doc(slug: str) -> str:
        return open(f"docs/{slug}.md").read()
    """
    uri_template: str        # e.g. "/docs/{slug}" or "file:///{path}"
    name: str
    description: str
    mime_type: str
    func: t.Callable
    is_async: bool
    wants_context: bool

    def uri_vars(self) -> list[str]:
        """URI template mein variables dhundho: /docs/{slug} → ["slug"]"""
        return _re.findall(r"\{(\w+)\}", self.uri_template)

    def match(self, uri: str) -> dict | None:
        """URI ko template se match karo, variables return karo."""
        pattern = _re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", self.uri_template)
        m = _re.fullmatch(pattern, uri)
        return m.groupdict() if m else None


@dataclass
class Prompt:
    """
    MCP Prompt — reusable prompt templates.

    @app.prompt("code_review")
    def review_prompt(language: str = "python") -> str:
        return f"Please review this {language} code..."
    """
    name: str
    description: str
    func: t.Callable
    arguments: list[dict]  # MCP-format argument descriptors
    is_async: bool


@dataclass
class SpellChainDef:
    """Registered spell chain/pipeline."""
    name: str
    steps: list[str]
    description: str
    scopes: list[str]
    tags: list[str]


# ============================================================================
# SECTION 11 — METRICS
# ============================================================================

class Metrics:
    def __init__(self):
        self._counts: dict[str, int] = defaultdict(int)
        self._timings: dict[str, deque] = defaultdict(lambda: deque(maxlen=1000))
        self._lock = threading.Lock()

    def incr(self, key: str, n: int = 1):
        with self._lock:
            self._counts[key] += n

    def time(self, key: str, ms: float):
        with self._lock:
            self._timings[key].append(ms)

    def snapshot(self) -> dict:
        with self._lock:
            out: dict = {"counters": dict(self._counts), "timings": {}}
            for k, q in self._timings.items():
                if not q:
                    continue
                arr = sorted(q)
                out["timings"][k] = {
                    "count": len(arr),
                    "p50": arr[len(arr) // 2],
                    "p95": arr[int(len(arr) * 0.95)],
                    "p99": arr[int(len(arr) * 0.99)],
                    "max": arr[-1],
                }
            return out


# ============================================================================
# SECTION 12 — TTL CACHE
# ============================================================================

class TTLCache:
    def __init__(self, max_size: int = 1024):
        self._store: dict[str, tuple[float, t.Any]] = {}
        self._max = max_size
        self._lock = threading.Lock()

    def get(self, key: str) -> t.Any:
        with self._lock:
            if key not in self._store:
                return None
            exp, val = self._store[key]
            if exp < time.time():
                del self._store[key]
                return None
            return val

    def set(self, key: str, value: t.Any, ttl: int):
        with self._lock:
            if len(self._store) >= self._max:
                now = time.time()
                expired = [k for k, (e, _) in self._store.items() if e < now]
                for k in expired[:max(1, self._max // 10)]:
                    self._store.pop(k, None)
                if len(self._store) >= self._max:
                    self._store.pop(next(iter(self._store)))
            self._store[key] = (time.time() + ttl, value)


# ============================================================================
# SECTION 13 — HOT RELOADER
# ============================================================================

class HotReloader:
    """
    Spells file watch karo, changes pe auto-reload karo.

    Usage:
        app.serve(port=8765, hot_reload="my_spells.py",
                  reload_fn=register_spells)
    """

    def __init__(self, app: Conjure, spells_file: str,
                 register_fn: t.Callable):
        self._app = app
        self._file = spells_file
        self._register_fn = register_fn
        self._last_mtime: float = 0
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self):
        try:
            self._last_mtime = os.path.getmtime(self._file)
        except OSError:
            self._last_mtime = 0
        self._thread = threading.Thread(target=self._watch, daemon=True,
                                        name="shabd-hot-reload")
        self._thread.start()
        log.info(f"Hot reload watching: {self._file}")

    def _watch(self):
        while True:
            try:
                mtime = os.path.getmtime(self._file)
                if mtime != self._last_mtime and self._last_mtime != 0:
                    self._reload()
                    self._last_mtime = mtime
                elif self._last_mtime == 0:
                    self._last_mtime = mtime
            except OSError:
                pass
            except Exception as e:
                log.warning(f"HotReloader watch error: {e}")
            time.sleep(1.0)

    def _reload(self):
        with self._lock:
            try:
                log.info(f"Hot reload: {self._file} changed, reloading...")
                # Purane non-mcp-proxy spells remove karo
                file_module_name = "user_spells"
                before = set(self._app._spells.keys())

                # Reload module
                spec = importlib.util.spec_from_file_location(
                    file_module_name, self._file)
                if spec is None or spec.loader is None:
                    return
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)  # type: ignore

                # Purane user-defined spells clear karo (mcp-proxy rakho)
                to_remove = [
                    k for k, v in list(self._app._spells.items())
                    if "mcp-proxy" not in v.tags
                ]
                for k in to_remove:
                    del self._app._spells[k]

                # Phir register karo
                if hasattr(module, self._register_fn.__name__):
                    getattr(module, self._register_fn.__name__)(self._app)
                elif hasattr(module, "register_spells"):
                    module.register_spells(self._app)

                after = set(self._app._spells.keys())
                added = after - before
                removed = before - after
                log.info(f"Hot reload complete. Added: {added}, Removed: {removed}")
            except Exception as e:
                log.error(f"Hot reload failed: {e}\n{traceback.format_exc()}")


# ============================================================================
# SECTION 14 — PROJECT GROUP (multi-project namespacing)
# ============================================================================

class ProjectGroup:
    """
    Ek SHABD server pe multiple projects ke spells namespace karo.

    Usage:
        finance = app.group("finance")
        hr = app.group("hr")

        @finance.spell
        def calculate_tax(amount: float) -> float:
            '''Tax calculate karo.'''
            return amount * 0.18

        @hr.spell
        def get_employee(emp_id: str) -> dict:
            '''Employee info.'''
            return db.get_employee(emp_id)

    Spell names:  "finance__calculate_tax", "hr__get_employee"
    Dashboard:    Group ke hisaab se filter hoga
    """

    def __init__(self, app: Conjure, name: str, *,
                 scopes: list[str] = (),
                 rate_limit: int | None = None,
                 description: str = ""):
        self._app = app
        self.name = name
        self._default_scopes = list(scopes)
        self._default_rate = rate_limit
        self.description = description

    def spell(self, _func=None, *,
              name: str | None = None,
              description: str | None = None,
              scopes: t.Iterable[str] = (),
              rate_limit: int | None = None,
              rate_window: float = 60.0,
              cache_ttl: int | None = None,
              timeout: float | None = None,
              retries: int = 0,
              tags: t.Iterable[str] = ()) -> t.Callable:
        """Group ke under spell register karo."""
        def _register(func: t.Callable) -> t.Callable:
            spell_name = f"{self.name}__{name or func.__name__}"
            merged_scopes = list(set(list(scopes) + self._default_scopes))
            merged_rate = rate_limit or self._default_rate
            merged_tags = list(tags) + [f"group:{self.name}"]

            sig = inspect.signature(func)
            wants_ctx = any(p in sig.parameters for p in ("ctx", "context"))
            try:
                hints = t.get_type_hints(func)
            except Exception:
                hints = {}
            return_schema = _type_to_schema(hints.get("return", t.Any))
            spell_obj = Spell(
                name=spell_name, func=func,
                description=description or (inspect.getdoc(func) or "").strip(),
                schema=_build_schema(func), returns_schema=return_schema,
                is_async=asyncio.iscoroutinefunction(func)
                         or inspect.isasyncgenfunction(func),
                is_streaming=inspect.isgeneratorfunction(func)
                             or inspect.isasyncgenfunction(func),
                wants_context=wants_ctx, scopes=merged_scopes,
                rate_limit=merged_rate,
                rate_window=rate_window,
                cache_ttl=cache_ttl,
                timeout=timeout if timeout is not None else self._app.default_timeout,
                retries=int(retries),
                tags=merged_tags,
                group=self.name,
            )
            if spell_obj.name in self._app._spells:
                raise ValueError(f"Spell '{spell_obj.name}' already registered")
            self._app._spells[spell_obj.name] = spell_obj
            log.info("registered group spell",
                     extra={"extra": {"spell": spell_obj.name, "group": self.name}})
            for p in self._app._plugins:
                try:
                    p.on_spell_registered(spell_obj)
                except Exception:
                    pass
            return func

        if _func is not None and callable(_func):
            return _register(_func)
        return _register


# ============================================================================
# SECTION 15 — THE MAIN CONJURE / SHABD CLASS
# ============================================================================

class Conjure:
    """
    SHABD ka main class. Do naam — Conjure (backward compat) aur SHABD (new).

    app = SHABD("my-tools", secret="your-secret")

    @app.spell          → Tool
    @app.resource(uri)  → MCP Resource (NEW)
    @app.prompt(name)   → MCP Prompt (NEW)
    app.chain("a|b|c")  → Spell Pipeline (NEW)
    app.group("finance") → ProjectGroup (NEW)
    app.cpm_config()    → CPM YAML (NEW)
    """

    def __init__(self,
                 name: str,
                 secret: t.Union[str, bytes, None] = None,
                 *,
                 require_auth: bool = True,
                 max_body_bytes: int = 1024 * 1024,
                 default_timeout: float = 30.0,
                 default_rate: int = 100,
                 cors_origin: str = "*",
                 audit_logger: t.Callable[[dict], None] | None = None,
                 plugins: t.Iterable[ConjurePlugin] = (),
                 # Enterprise (v2.2) — all optional, all stdlib-only:
                 additional_secrets: t.Iterable[bytes] = (),
                 grimoire_log_path: str | None = None,
                 idempotency_ttl: int = 86400,
                 audit_webhook_url: str | None = None,
                 audit_webhook_secret: t.Union[str, bytes, None] = None):
        self.name = name
        self.require_auth = require_auth
        self.max_body_bytes = max_body_bytes
        self.default_timeout = default_timeout
        self.cors_origin = cors_origin
        self.audit = audit_logger

        if secret is None:
            secret = os.environ.get("SHABD_SECRET") or os.environ.get("CONJURE_SECRET")
        if isinstance(secret, str):
            secret = secret.encode()
        if not secret:
            log.warning("No SHABD_SECRET — generating ephemeral key. "
                        "Tokens will invalidate on restart.")
            secret = secrets.token_bytes(32)

        normalized_additional: list[bytes] = []
        for s in additional_secrets:
            if isinstance(s, str):
                s = s.encode()
            if s:
                normalized_additional.append(s)
        self.tokens = TokenManager(secret,
                                   additional_verifying_secrets=normalized_additional)
        self.grimoire = Grimoire(secret)
        self._grimoire_log: GrimoireJSONL | None = None
        if grimoire_log_path:
            self._grimoire_log = GrimoireJSONL(grimoire_log_path)
            existing = self._grimoire_log.load()
            if existing:
                # Reload pages and rebuild head — verify integrity along the way.
                for page in existing:
                    self.grimoire._pages.append(page)
                if self.grimoire._pages:
                    self.grimoire._head = self.grimoire._pages[-1]["hash"]
                v = self.grimoire.verify()
                if not v.get("ok"):
                    log.error("grimoire log on disk failed verification",
                              extra={"extra": v})
                else:
                    log.info("grimoire log restored",
                             extra={"extra": {"pages": v["pages"]}})
        self.limiter = RateLimiter(default_rate=default_rate)
        self.breaker = CircuitBreaker()
        self.metrics = Metrics()
        self.cache = TTLCache()
        self.idempotency = IdempotencyStore(ttl=idempotency_ttl)
        self.shutdown_state = _ShutdownState()
        webhook_secret_bytes: bytes | None = None
        if isinstance(audit_webhook_secret, str):
            webhook_secret_bytes = audit_webhook_secret.encode()
        elif isinstance(audit_webhook_secret, (bytes, bytearray)):
            webhook_secret_bytes = bytes(audit_webhook_secret)
        self.audit_webhook: AuditWebhook | None = (
            AuditWebhook(audit_webhook_url, webhook_secret_bytes)
            if audit_webhook_url else None
        )
        self._spells: dict[str, Spell] = {}
        self._resources: dict[str, Resource] = {}     # NEW: resources registry
        self._prompts: dict[str, Prompt] = {}         # NEW: prompts registry
        self._chains: dict[str, SpellChainDef] = {}   # NEW: chains registry
        self._before_hooks: list[t.Callable] = []
        self._after_hooks: list[t.Callable] = []
        self._shutdown = threading.Event()
        self._plugins: list[ConjurePlugin] = list(plugins)
        self._recent_calls: deque = deque(maxlen=500)    # replay ke liye 500 rakho

    # ── @app.spell decorator ─────────────────────────────────────────────

    def spell(self, _func=None, *,
              name: str | None = None,
              description: str | None = None,
              scopes: t.Iterable[str] = (),
              rate_limit: int | None = None,
              rate_window: float = 60.0,
              cache_ttl: int | None = None,
              timeout: float | None = None,
              retries: int = 0,
              tags: t.Iterable[str] = (),
              max_concurrent: int | None = None,
              idempotent: bool = False) -> t.Callable:
        """Register a function as a callable AI tool.

        max_concurrent: cap simultaneous in-flight calls for this spell
                        (semaphore — useful for protecting downstreams).
        idempotent:    declare the spell is safe to retry without an
                        Idempotency-Key.
        """
        def _register(func: t.Callable) -> t.Callable:
            sig = inspect.signature(func)
            wants_ctx = any(p in sig.parameters for p in ("ctx", "context"))
            try:
                hints = t.get_type_hints(func)
            except Exception:
                hints = {}
            return_schema = _type_to_schema(hints.get("return", t.Any))
            spell_obj = Spell(
                name=name or func.__name__, func=func,
                description=description or (inspect.getdoc(func) or "").strip(),
                schema=_build_schema(func), returns_schema=return_schema,
                is_async=asyncio.iscoroutinefunction(func)
                         or inspect.isasyncgenfunction(func),
                is_streaming=inspect.isgeneratorfunction(func)
                             or inspect.isasyncgenfunction(func),
                wants_context=wants_ctx, scopes=list(scopes),
                rate_limit=rate_limit, rate_window=rate_window,
                cache_ttl=cache_ttl,
                timeout=timeout if timeout is not None else self.default_timeout,
                retries=int(retries), tags=list(tags), group="",
                max_concurrent=max_concurrent, idempotent=bool(idempotent),
            )
            if max_concurrent and max_concurrent > 0:
                spell_obj._semaphore = asyncio.Semaphore(max_concurrent)
            if spell_obj.name in self._spells:
                raise ValueError(f"Spell '{spell_obj.name}' already registered")
            self._spells[spell_obj.name] = spell_obj
            log.info("registered spell",
                     extra={"extra": {"spell": spell_obj.name}})
            for p in self._plugins:
                try:
                    p.on_spell_registered(spell_obj)
                except Exception:
                    pass
            return func

        if _func is not None and callable(_func):
            return _register(_func)
        return _register

    # ── @app.resource(uri) decorator — NEW ──────────────────────────────

    def resource(self,
                 uri_template: str,
                 name: str | None = None,
                 description: str | None = None,
                 mime_type: str = "text/plain") -> t.Callable:
        """
        FastMCP ki tarah MCP Resources expose karo.

        @app.resource("/docs/{slug}")
        def get_doc(slug: str) -> str:
            '''Return documentation page.'''
            return open(f"docs/{slug}.md").read()

        @app.resource("db://users/{user_id}", mime_type="application/json")
        async def get_user(user_id: str) -> dict:
            return await db.find_user(user_id)

        Claude Desktop mein ye resources ke roop mein dikhai denge.
        """
        def _register(func: t.Callable) -> t.Callable:
            sig = inspect.signature(func)
            wants_ctx = any(p in sig.parameters for p in ("ctx", "context"))
            res_obj = Resource(
                uri_template=uri_template,
                name=name or func.__name__,
                description=description or (inspect.getdoc(func) or "").strip(),
                mime_type=mime_type,
                func=func,
                is_async=asyncio.iscoroutinefunction(func),
                wants_context=wants_ctx,
            )
            self._resources[uri_template] = res_obj
            log.info("registered resource",
                     extra={"extra": {"resource": uri_template}})
            return func

        return _register

    # ── @app.prompt(name) decorator — NEW ───────────────────────────────

    def prompt(self,
               name: str | None = None,
               description: str | None = None) -> t.Callable:
        """
        FastMCP ki tarah MCP Prompts expose karo.

        @app.prompt("code_review")
        def review_prompt(language: str = "python",
                          focus: str = "bugs") -> str:
            '''Code review ke liye prompt template.'''
            return f"Review this {language} code focusing on {focus}."

        @app.prompt("summarize")
        async def summarize_prompt(text: str, style: str = "brief") -> list:
            '''Ek ya zyada messages return kar sakte ho.'''
            return [
                {"role": "user", "content": f"Summarize ({style}): {text}"}
            ]

        Claude Desktop mein "Prompts" tab mein dikhai denge.
        """
        def _register(func: t.Callable) -> t.Callable:
            prompt_name = name or func.__name__
            sig = inspect.signature(func)
            # MCP argument format mein convert karo
            try:
                hints = t.get_type_hints(func)
            except Exception:
                hints = {}
            arguments = []
            for pname, param in sig.parameters.items():
                if pname in ("self", "cls", "ctx", "context"):
                    continue
                ann = hints.get(pname, param.annotation)
                arg_desc = {"name": pname, "required": param.default is inspect.Parameter.empty}
                if ann is not inspect.Parameter.empty and ann is not t.Any:
                    arg_desc["description"] = str(ann)
                arguments.append(arg_desc)

            prompt_obj = Prompt(
                name=prompt_name,
                description=description or (inspect.getdoc(func) or "").strip(),
                func=func,
                arguments=arguments,
                is_async=asyncio.iscoroutinefunction(func),
            )
            self._prompts[prompt_name] = prompt_obj
            log.info("registered prompt",
                     extra={"extra": {"prompt": prompt_name}})
            return func

        if callable(name):
            # @app.prompt bina arguments ke use hua
            func = name
            name = None
            return _register(func)
        return _register

    # ── app.chain() — NEW ────────────────────────────────────────────────

    def chain(self,
              pipeline: str,
              *,
              name: str | None = None,
              description: str | None = None,
              scopes: t.Iterable[str] = (),
              tags: t.Iterable[str] = ()) -> SpellChainDef:
        """
        Multiple spells ko ek pipeline mein connect karo.

        Output of spell N becomes input of spell N+1.

        Usage:
            app.chain("search_docs | summarize | translate",
                      name="smart_search",
                      description="Search, summarize, aur translate karo")

        Rules:
          - Agar spell N dict return kare → spell N+1 ke kwargs bante hain
          - Agar koi aur return type ho → spell N+1 ke pehle parameter mein jaata hai
          - Sab spells pehle register hone chahiye

        Result as spell bhi callable hai:
            POST /spells/smart_search  body: {"query": "...", "limit": 5}
        """
        steps = [s.strip() for s in pipeline.split("|") if s.strip()]
        if len(steps) < 2:
            raise ValueError("Chain mein kam se kam 2 spells chahiye")

        chain_name = name or "_".join(steps)
        chain_def = SpellChainDef(
            name=chain_name,
            steps=steps,
            description=description or f"Pipeline: {' → '.join(steps)}",
            scopes=list(scopes),
            tags=list(tags) + ["chain"],
        )
        self._chains[chain_name] = chain_def

        # Chain ko ek spell ki tarah bhi register karo
        app_ref = self
        chain_def_ref = chain_def

        async def _chain_fn(**kwargs: t.Any) -> t.Any:
            return await app_ref._execute_chain(chain_def_ref, kwargs)

        _chain_fn.__name__ = chain_name
        _chain_fn.__doc__ = chain_def.description

        chain_spell = Spell(
            name=chain_name,
            func=_chain_fn,
            description=chain_def.description,
            schema=self._spells[steps[0]].schema if steps[0] in self._spells else {},
            returns_schema={},
            is_async=True, is_streaming=False, wants_context=False,
            scopes=list(scopes), rate_limit=None, rate_window=60.0,
            cache_ttl=None, timeout=self.default_timeout * len(steps),
            retries=0, tags=list(tags) + ["chain"], group="",
        )
        self._spells[chain_name] = chain_spell
        log.info("registered chain",
                 extra={"extra": {"chain": chain_name, "steps": steps}})
        return chain_def

    async def _execute_chain(self, chain: SpellChainDef,
                             initial_args: dict) -> t.Any:
        """Chain ke steps ek-ek karke execute karo."""
        result: t.Any = initial_args
        ctx = Context(trace_id=uuid.uuid4().hex, transport="chain")
        for step_name in chain.steps:
            if step_name not in self._spells:
                raise SpellNotFoundError(f"Chain step '{step_name}' not found")
            spell = self._spells[step_name]
            if isinstance(result, dict):
                args = result
            else:
                # Pichla result pehle parameter mein daalo
                sig = inspect.signature(spell.func)
                params = [p for p in sig.parameters
                          if p not in ("self", "cls", "ctx", "context")]
                if params:
                    args = {params[0]: result}
                else:
                    args = {}
            result = await self._invoke_async(spell, args, ctx)
        return result

    # ── app.group() — NEW ───────────────────────────────────────────────

    def group(self, name: str, *,
              scopes: list[str] = (),
              rate_limit: int | None = None,
              description: str = "") -> ProjectGroup:
        """
        Multi-project namespacing ke liye group banao.

        finance = app.group("finance")
        @finance.spell
        def calculate_tax(amount: float) -> float: ...
        # → Registered as "finance__calculate_tax"
        """
        return ProjectGroup(self, name, scopes=scopes,
                            rate_limit=rate_limit, description=description)

    # ── app.cpm_config() — NEW ───────────────────────────────────────────

    def cpm_config(self, base_url: str = "http://localhost:8765") -> str:
        """
        CPM (Conversational Project Mesh) ke liye YAML config auto-generate karo.

        cpm_yaml = app.cpm_config(base_url="https://my-shabd.company.com")
        print(cpm_yaml)

        Output ko apne CPM config mein directly paste karo.
        """
        groups: dict[str, list[dict]] = {}
        all_spells = []
        for s in self._spells.values():
            if "chain" in s.tags and s.group == "":
                continue  # chains separately handle karenge
            entry = {
                "name": s.name,
                "display_name": s.name.replace("__", " → ").replace("_", " ").title(),
                "description": (s.description or s.name)[:120],
                "endpoint": f"/spells/{s.name}",
                "method": "POST",
                "tags": s.tags,
                "streaming": s.is_streaming,
                "stream_endpoint": f"/stream/{s.name}" if s.is_streaming else None,
                "input_schema": s.schema,
            }
            all_spells.append(entry)
            g = s.group or "default"
            groups.setdefault(g, []).append(entry)

        chains_list = [
            {
                "name": c.name,
                "steps": c.steps,
                "description": c.description,
                "endpoint": f"/spells/{c.name}",
            }
            for c in self._chains.values()
        ]
        resources_list = [
            {
                "uri_template": r.uri_template,
                "name": r.name,
                "description": r.description,
                "mime_type": r.mime_type,
            }
            for r in self._resources.values()
        ]
        prompts_list = [
            {
                "name": p.name,
                "description": p.description,
                "arguments": p.arguments,
            }
            for p in self._prompts.values()
        ]

        lines = [
            f"# CPM Config — Auto-generated by SHABD v{__version__}",
            f"# Project: {self.name}",
            "",
            "project:",
            f"  name: {self.name}",
            "  type: shabd",
            f"  base_url: {base_url}",
            f"  manifest_url: {base_url}/manifest",
            f"  openapi_url: {base_url}/openapi.json",
            f"  dashboard_url: {base_url}/dashboard",
            f"  health_url: {base_url}/health",
            "",
        ]
        if all_spells:
            lines.append("  spells:")
            for s in all_spells:
                lines.append(f"    - name: {s['name']}")
                lines.append(f"      description: \"{s['description']}\"")
                lines.append(f"      endpoint: {s['endpoint']}")
                if s["tags"]:
                    lines.append(f"      tags: [{', '.join(s['tags'])}]")
            lines.append("")
        if groups and list(groups.keys()) != ["default"]:
            lines.append("  groups:")
            for gname, gspells in groups.items():
                if gname == "default":
                    continue
                lines.append(f"    - name: {gname}")
                lines.append(f"      spells: [{', '.join(s['name'] for s in gspells)}]")
            lines.append("")
        if chains_list:
            lines.append("  chains:")
            for c in chains_list:
                lines.append(f"    - name: {c['name']}")
                lines.append(f"      steps: [{', '.join(c['steps'])}]")
            lines.append("")
        if resources_list:
            lines.append("  resources:")
            for r in resources_list:
                lines.append(f"    - uri: {r['uri_template']}")
                lines.append(f"      name: {r['name']}")
            lines.append("")
        if prompts_list:
            lines.append("  prompts:")
            for p in prompts_list:
                lines.append(f"    - name: {p['name']}")
                lines.append(f"      description: \"{p['description']}\"")
            lines.append("")

        return "\n".join(lines)

    # ── Hooks ─────────────────────────────────────────────────────────────

    def before(self, fn):
        self._before_hooks.append(fn)
        return fn

    def after(self, fn):
        self._after_hooks.append(fn)
        return fn

    # ── Token issuance ────────────────────────────────────────────────────

    def issue_token(self, subject: str, scopes: t.Iterable[str] = (),
                    ttl: int = 3600) -> str:
        return self.tokens.issue(subject, scopes, ttl)

    # ── Manifest / Discovery ─────────────────────────────────────────────

    def manifest(self) -> dict:
        """Universal AI-friendly manifest — OpenAI, Anthropic, Gemini, local LLMs."""
        return {
            "shabd_version": __version__,
            "name": self.name,
            "spells": [s.to_manifest_entry() for s in self._spells.values()],
            "resources": [
                {
                    "uri_template": r.uri_template,
                    "name": r.name,
                    "description": r.description,
                    "mime_type": r.mime_type,
                }
                for r in self._resources.values()
            ],
            "prompts": [
                {
                    "name": p.name,
                    "description": p.description,
                    "arguments": p.arguments,
                }
                for p in self._prompts.values()
            ],
            "chains": [
                {
                    "name": c.name,
                    "steps": c.steps,
                    "description": c.description,
                }
                for c in self._chains.values()
            ],
        }

    def openapi(self) -> dict:
        paths = {}
        for s in self._spells.values():
            paths[f"/spells/{s.name}"] = {
                "post": {
                    "summary": s.description.split("\n", 1)[0] if s.description else s.name,
                    "description": s.description,
                    "tags": s.tags or [self.name],
                    "security": [{"bearerAuth": []}] if self.require_auth else [],
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {"schema": s.schema}},
                    },
                    "responses": {
                        "200": {"description": "Success",
                                "content": {"application/json": {"schema": {
                                    "type": "object",
                                    "properties": {
                                        "ok": {"type": "boolean"},
                                        "result": s.returns_schema or {},
                                        "trace_id": {"type": "string"},
                                    }
                                }}}},
                        "400": {"description": "Validation error"},
                        "401": {"description": "Unauthorized"},
                        "429": {"description": "Rate limit exceeded"},
                    },
                }
            }
        return {
            "openapi": "3.1.0",
            "info": {"title": self.name, "version": __version__,
                     "description": f"SHABD-generated spec for {self.name}"},
            "components": {
                "securitySchemes": {
                    "bearerAuth": {"type": "http", "scheme": "bearer"}
                }
            },
            "paths": paths,
        }

    # ── Core invocation pipeline ─────────────────────────────────────────

    def _grimoire_record(self, *, ctx: Context, spell: Spell,
                         args: t.Any, result: t.Any, ok: bool) -> None:
        """Append a redacted page to the chain, persist it, stream to SIEM."""
        try:
            page = self.grimoire.append(
                trace_id=ctx.trace_id, spell=spell.name,
                subject=ctx.subject, args=args, result=result, ok=ok,
            )
        except Exception:
            log.exception("grimoire append failed")
            return
        if self._grimoire_log:
            try:
                self._grimoire_log.append(page)
            except Exception:
                log.exception("grimoire log persistence failed")
        if self.audit_webhook is not None:
            try:
                loop = asyncio.get_running_loop()
                self.audit_webhook.submit(page, loop)
            except RuntimeError:
                # No running loop — skip; the webhook is a best-effort.
                pass

    def _make_context(self, *, transport: str,
                      auth_header: str | None,
                      traceparent: str | None = None,
                      idempotency_key: str | None = None) -> Context:
        parent_trace, parent_span, flags = TraceContextCodec.decode(traceparent)
        trace_id = parent_trace or uuid.uuid4().hex
        ctx = Context(
            trace_id=trace_id, transport=transport,
            parent_span_id=parent_span,
            sampled=bool(flags & 0x01) if traceparent else True,
            idempotency_key=idempotency_key,
        )
        if auth_header:
            token = auth_header.removeprefix("Bearer ").strip()
            payload = self.tokens.verify(token)
            ctx.subject = payload.get("sub", "anonymous")
            ctx.scopes = list(payload.get("scopes", []))
        elif self.require_auth:
            raise AuthError("missing Authorization header")
        return ctx

    def _check_authz(self, ctx: Context, spell: Spell):
        if spell.scopes and not any(ctx.has_scope(s) for s in spell.scopes):
            raise ForbiddenError(f"spell '{spell.name}' requires one of {spell.scopes}")

    def _check_rate(self, ctx: Context, spell: Spell):
        if spell.rate_limit:
            key = f"{ctx.subject}:{spell.name}"
            plugin_result = None
            for p in self._plugins:
                plugin_result = p.check_rate_limit(key, spell.rate_limit, spell.rate_window)
                if plugin_result is not None:
                    break
            if plugin_result is not None:
                ok, retry = plugin_result
            else:
                ok, retry = self.limiter.check(key, spell.rate_limit, spell.rate_window)
            if not ok:
                raise RateLimitError(f"rate limit exceeded for {spell.name}",
                                     retry_after=retry)

    def _cache_key(self, spell_name: str, args: dict, subject: str) -> str:
        canonical = json.dumps(args, sort_keys=True, default=str)
        material = f"{subject}|{spell_name}|{canonical}".encode()
        return hashlib.sha256(material).hexdigest()

    async def _invoke_async(self, spell: Spell, args: dict, ctx: Context) -> t.Any:
        if not self.shutdown_state.enter():
            raise ConjureError("server is shutting down",
                               code="shutting_down", retry_after=5)
        try:
            return await self._invoke_async_inner(spell, args, ctx)
        finally:
            self.shutdown_state.exit()

    async def _invoke_async_inner(self, spell: Spell, args: dict, ctx: Context) -> t.Any:
        _validate_against_schema(args, spell.schema)
        self._check_authz(ctx, spell)
        self._check_rate(ctx, spell)
        if self.breaker.is_open(spell.name):
            raise ConjureError(f"circuit open for '{spell.name}'", retry_after=30)

        # Idempotency-Key: if the same (key, body) shows up again within TTL,
        # return the cached result rather than running the spell twice.
        if ctx.idempotency_key:
            replay = self.idempotency.lookup(ctx.idempotency_key, spell.name, args)
            if replay is not None:
                ok, value = replay
                self.metrics.incr(f"spell.{spell.name}.idempotent_replay")
                if ok:
                    return value
                raise ConjureError(
                    "previous call with this Idempotency-Key failed",
                    code="idempotent_replay_error", **{"original_error": value},
                )

        # Per-spell concurrency cap (asyncio semaphore).
        if spell._semaphore is not None:
            return await self._invoke_with_semaphore(spell, args, ctx)
        return await self._invoke_body(spell, args, ctx)

    async def _invoke_with_semaphore(self, spell: Spell, args: dict,
                                     ctx: Context) -> t.Any:
        async with spell._semaphore:
            return await self._invoke_body(spell, args, ctx)

    async def _invoke_body(self, spell: Spell, args: dict, ctx: Context) -> t.Any:

        if spell.cache_ttl and not spell.is_streaming:
            ck = self._cache_key(spell.name, args, ctx.subject)
            cached = _MISS
            for p in self._plugins:
                val = p.cache_get(ck)
                if val is not _MISS:
                    cached = val
                    break
            if cached is _MISS:
                cached = self.cache.get(ck)  # type: ignore
            if cached is not None and cached is not _MISS:
                self.metrics.incr(f"spell.{spell.name}.cache_hit")
                return cached

        for p in self._plugins:
            try:
                p.on_call_start(ctx, spell.name, args)
            except ConjureError:
                raise
            except Exception:
                log.exception("plugin.on_call_start failed")
        for hook in self._before_hooks:
            try:
                hook(ctx, spell.name, args)
            except ConjureError:
                # Policy hooks (RBAC, sanctions block, etc.) signal denial
                # by raising — let it through.
                raise
            except Exception:
                log.exception("before hook failed")

        call_kwargs = dict(args)
        if spell.wants_context:
            for pname in ("ctx", "context"):
                if pname in inspect.signature(spell.func).parameters:
                    call_kwargs[pname] = ctx
                    break

        attempts = 0
        last_exc: BaseException | None = None
        while attempts <= spell.retries:
            attempts += 1
            t0 = time.time()
            try:
                if spell.is_async:
                    coro = spell.func(**call_kwargs)
                    result = await asyncio.wait_for(coro, timeout=spell.timeout)
                else:
                    loop = asyncio.get_running_loop()
                    result = await asyncio.wait_for(
                        loop.run_in_executor(None, lambda: spell.func(**call_kwargs)),
                        timeout=spell.timeout,
                    )
                self.breaker.record_success(spell.name)
                self.metrics.incr(f"spell.{spell.name}.ok")
                self.metrics.time(f"spell.{spell.name}", (time.time() - t0) * 1000)
                if spell.cache_ttl and not spell.is_streaming:
                    ck2 = self._cache_key(spell.name, args, ctx.subject)
                    self.cache.set(ck2, result, spell.cache_ttl)
                    for p in self._plugins:
                        try:
                            p.cache_set(ck2, result, spell.cache_ttl)
                        except Exception:
                            pass
                for hook in self._after_hooks:
                    try:
                        hook(ctx, spell.name, result, None)
                    except Exception:
                        pass
                elapsed = ctx.elapsed_ms()
                for p in self._plugins:
                    try:
                        p.on_call_end(ctx, spell.name, args, result, None, elapsed)
                    except Exception:
                        pass
                # Replay ke liye args snapshot bhi save karo
                self._recent_calls.append(CallRecord(
                    trace_id=ctx.trace_id, spell=spell.name,
                    subject=ctx.subject, transport=ctx.transport,
                    args_size=len(json.dumps(args, default=str)),
                    ok=True, elapsed_ms=elapsed, error_code=None,
                    args_snapshot=args,
                ))
                # Grimoire: append a tamper-evident, PII-redacted leaf to the
                # signed audit chain (and persist + stream to SIEM if wired).
                self._grimoire_record(
                    ctx=ctx, spell=spell,
                    args=_redact_for_audit(args, spell.schema),
                    result=_redact_for_audit(result, spell.returns_schema),
                    ok=True,
                )
                if self.audit:
                    self.audit({"trace_id": ctx.trace_id, "subject": ctx.subject,
                                "spell": spell.name, "args": args, "ok": True,
                                "elapsed_ms": elapsed})
                if ctx.idempotency_key:
                    self.idempotency.store(
                        ctx.idempotency_key, spell.name, args, True, result,
                    )
                return result
            except asyncio.TimeoutError:
                last_exc = TimeoutError_(f"'{spell.name}' timed out after {spell.timeout}s")
            except ConjureError as e:
                if 400 <= e.status < 500:
                    raise
                last_exc = e
            except Exception as e:
                last_exc = e
                log.exception("spell raised",
                              extra={"trace_id": ctx.trace_id, "spell": spell.name})
            self.breaker.record_failure(spell.name)
            self.metrics.incr(f"spell.{spell.name}.error")
            if attempts <= spell.retries:
                await asyncio.sleep(0.1 * (2 ** (attempts - 1)))

        elapsed_err = ctx.elapsed_ms()
        for p in self._plugins:
            try:
                p.on_call_end(ctx, spell.name, args, None, last_exc, elapsed_err)
            except Exception:
                pass
        err_code = (last_exc.code if isinstance(last_exc, ConjureError)
                    else "internal_error")
        self._recent_calls.append(CallRecord(
            trace_id=ctx.trace_id, spell=spell.name,
            subject=ctx.subject, transport=ctx.transport,
            args_size=len(json.dumps(args, default=str)),
            ok=False, elapsed_ms=elapsed_err, error_code=err_code,
            args_snapshot=args,
        ))
        self._grimoire_record(
            ctx=ctx, spell=spell,
            args=_redact_for_audit(args, spell.schema),
            result={"error": err_code,
                    "message": str(last_exc) if last_exc else ""},
            ok=False,
        )
        if ctx.idempotency_key:
            self.idempotency.store(
                ctx.idempotency_key, spell.name, args, False,
                {"code": err_code, "message": str(last_exc) if last_exc else ""},
            )
        if isinstance(last_exc, ConjureError):
            raise last_exc
        raise ConjureError(str(last_exc) or "spell failed")

    def _suggest_spell(self, spell_name: str) -> list[str]:
        import difflib
        return difflib.get_close_matches(
            spell_name, list(self._spells.keys()), n=3, cutoff=0.5,
        )

    def invoke(self, spell_name: str, args: dict, *,
               token: str | None = None) -> t.Any:
        if spell_name not in self._spells:
            matches = self._suggest_spell(spell_name)
            raise SpellNotFoundError(
                f"no such spell: {spell_name}",
                did_you_mean=matches or None,
                hint=(f"Did you mean '{matches[0]}'?" if matches
                      else "Call GET /manifest for the full spell list."),
            )
        spell = self._spells[spell_name]
        auth = f"Bearer {token}" if token else None
        ctx = self._make_context(transport="local", auth_header=auth)
        return asyncio.run(self._invoke_async(spell, args, ctx))

    async def invoke_async(self, spell_name: str, args: dict, *,
                           token: str | None = None) -> t.Any:
        if spell_name not in self._spells:
            matches = self._suggest_spell(spell_name)
            raise SpellNotFoundError(
                f"no such spell: {spell_name}",
                did_you_mean=matches or None,
                hint=(f"Did you mean '{matches[0]}'?" if matches
                      else "Call GET /manifest for the full spell list."),
            )
        spell = self._spells[spell_name]
        auth = f"Bearer {token}" if token else None
        ctx = self._make_context(transport="local", auth_header=auth)
        return await self._invoke_async(spell, args, ctx)

    async def invoke_stream(self, spell_name: str, args: dict, *,
                            token: str | None = None,
                            _ctx: Context | None = None) -> t.AsyncIterator:
        if spell_name not in self._spells:
            raise SpellNotFoundError(f"no such spell: {spell_name}")
        spell = self._spells[spell_name]
        if not spell.is_streaming:
            raise ConjureError(f"spell '{spell_name}' is not streaming")
        if _ctx is not None:
            ctx = _ctx
        else:
            auth = f"Bearer {token}" if token else None
            ctx = self._make_context(transport="local", auth_header=auth)
        _validate_against_schema(args, spell.schema)
        self._check_authz(ctx, spell)
        self._check_rate(ctx, spell)
        call_kwargs = dict(args)
        if spell.wants_context:
            call_kwargs["ctx"] = ctx
        if inspect.isasyncgenfunction(spell.func):
            async for item in spell.func(**call_kwargs):
                yield item
        else:
            gen = spell.func(**call_kwargs)
            loop = asyncio.get_running_loop()
            while True:
                item = await loop.run_in_executor(None, lambda: next(gen, _STOP))
                if item is _STOP:
                    return
                yield item

    # ── Transports ─────────────────────────────────────────────────────────

    def serve(self, host: str = "0.0.0.0", port: int = 8765,
              tls_cert: str | None = None,
              tls_key: str | None = None,
              hot_reload: str | None = None,
              reload_fn: t.Callable | None = None,
              shutdown_grace_s: float = 30.0):
        """
        HTTP/SSE/WebSocket server start karo.

        hot_reload="my_spells.py" → File change pe auto-reload
        reload_fn=register_spells → reload pe call hone wala function
        shutdown_grace_s    → on SIGTERM, wait this long for in-flight to drain
        """
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        server = _AsyncHttpServer(self, host, port, tls_cert, tls_key, loop)

        # Hot reload setup
        if hot_reload and reload_fn:
            reloader = HotReloader(self, hot_reload, reload_fn)
            reloader.start()

        # Graceful shutdown — SIGTERM (k8s, systemd) and SIGINT (Ctrl-C) both
        # flip the shutdown flag so /readyz returns 503 immediately, then we
        # wait up to `shutdown_grace_s` for in-flight calls to finish.
        import signal as _signal

        def _request_drain(*_a):
            if not self.shutdown_state.draining:
                log.info("shutdown signal received — draining",
                         extra={"extra": {"in_flight":
                                          self.shutdown_state.in_flight}})
                self.shutdown_state.begin_drain()

        try:
            loop.add_signal_handler(_signal.SIGTERM, _request_drain)
        except (NotImplementedError, RuntimeError):
            # Windows / non-main thread — fall back to default behavior.
            pass

        try:
            for p in self._plugins:
                try:
                    p.on_startup(self)
                except Exception:
                    log.exception("plugin.on_startup failed")
            loop.run_until_complete(server.start())
            log.info("shabd listening",
                     extra={"extra": {"host": host, "port": port,
                                      "spells": len(self._spells),
                                      "resources": len(self._resources),
                                      "prompts": len(self._prompts)}})
            loop.run_until_complete(server.serve_forever())
        except KeyboardInterrupt:
            log.info("shutting down (SIGINT)")
        finally:
            self.shutdown_state.begin_drain()
            deadline = time.time() + shutdown_grace_s
            while (self.shutdown_state.in_flight > 0
                   and time.time() < deadline):
                try:
                    loop.run_until_complete(asyncio.sleep(0.1))
                except Exception:
                    break
            if self.shutdown_state.in_flight > 0:
                log.warning("forcing shutdown with in-flight calls",
                            extra={"extra":
                                   {"in_flight":
                                    self.shutdown_state.in_flight}})
            loop.run_until_complete(server.shutdown())
            if self._grimoire_log:
                try:
                    self._grimoire_log.close()
                except Exception:
                    pass
            for p in self._plugins:
                try:
                    p.close()
                except Exception:
                    pass
            loop.close()

    def stdio(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_stdio_loop(self))
        except KeyboardInterrupt:
            pass
        finally:
            loop.close()

    def mcp_stdio(self, *, default_scopes: t.Iterable[str] = ("*",),
                  default_subject: str = "mcp-client"):
        """
        MCP-compatible stdio server.
        Ab tools + resources + prompts sab support karta hai.
        Claude Desktop, Cursor, aur sab MCP clients ke saath compatible.
        """
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_mcp_stdio_loop(
                self, default_subject, list(default_scopes)))
        except KeyboardInterrupt:
            pass
        finally:
            loop.close()

    @classmethod
    def from_config(cls, path: str) -> Conjure:
        cfg = ConfigLoader.load(path)
        return ConfigLoader.build_conjure(cfg)


_STOP = object()

# SHABD is the canonical name, Conjure is the backward-compat alias
SHABD = Conjure


# ============================================================================
# SECTION 16 — HTTP / WEBSOCKET / SSE TRANSPORT
# ============================================================================

class _AsyncHttpServer:
    def __init__(self, app: Conjure, host: str, port: int,
                 tls_cert: str | None, tls_key: str | None,
                 loop: asyncio.AbstractEventLoop):
        self.app = app
        self.host = host
        self.port = port
        self.tls_cert = tls_cert
        self.tls_key = tls_key
        self.loop = loop
        self._server: asyncio.AbstractServer | None = None

    async def start(self):
        ssl_ctx = None
        if self.tls_cert and self.tls_key:
            import ssl
            ssl_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
            ssl_ctx.load_cert_chain(self.tls_cert, self.tls_key)
        self._server = await asyncio.start_server(
            self._handle, host=self.host, port=self.port, ssl=ssl_ctx)

    async def serve_forever(self):
        if self._server:
            async with self._server:
                await self._server.serve_forever()

    async def shutdown(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter):
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=15)
            if not request_line:
                return
            try:
                method, path, _ = request_line.decode("iso-8859-1").rstrip().split(" ", 2)
            except ValueError:
                await self._send_error(writer, 400, "malformed request line")
                return
            headers: dict = {}
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=15)
                if line in (b"\r\n", b"\n", b""):
                    break
                k, _, v = line.decode("iso-8859-1").partition(":")
                headers[k.strip().lower()] = v.strip()
            if (headers.get("upgrade", "").lower() == "websocket"
                    and "upgrade" in headers.get("connection", "").lower()):
                await self._handle_websocket(reader, writer, path, headers)
                return
            body = b""
            length = int(headers.get("content-length", "0") or "0")
            if length > self.app.max_body_bytes:
                await self._send_error(writer, 413, "request body too large")
                return
            if length:
                body = await asyncio.wait_for(reader.readexactly(length), timeout=30)
            await self._route(method, path, headers, body, writer)
        except (asyncio.TimeoutError, ConnectionResetError, BrokenPipeError):
            pass
        except Exception:
            log.exception("connection handler crashed")
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _route(self, method: str, raw_path: str, headers: dict,
                     body: bytes, writer: asyncio.StreamWriter):
        url = urllib.parse.urlparse(raw_path)
        path = url.path

        if method == "OPTIONS":
            await self._send(writer, 204, "", b"", extra_headers={
                "Access-Control-Allow-Origin": self.app.cors_origin,
                "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                "Access-Control-Allow-Headers": "Authorization, Content-Type",
                "Access-Control-Max-Age": "86400",
            })
            return

        if method == "GET" and path == "/":
            await self._send_json(writer, 200, {
                "service": self.app.name, "shabd_version": __version__,
                "spells": len(self.app._spells),
                "resources": len(self.app._resources),
                "prompts": len(self.app._prompts),
                "endpoints": ["/manifest", "/openapi.json", "/health",
                              "/spells/<name>", "/stream/<name>", "/ws",
                              "/replay/<trace_id>", "/cpm-config",
                              "/grimoire/verify", "/grimoire/head",
                              "/grimoire/pages"],
            })
            return
        if method == "GET" and path == "/health":
            await self._send_json(writer, 200, {"status": "ok", "uptime_s": time.time()})
            return
        # Kubernetes-style probes:
        #   /healthz  — process is alive (always 200 if we can reply at all)
        #   /readyz   — accepting new requests (503 during shutdown drain)
        #   /startupz — finished warmup (same as readyz today; kept distinct
        #               so future warmup logic doesn't break orchestration)
        if method == "GET" and path == "/healthz":
            await self._send_json(writer, 200, {
                "status": "ok",
                "uptime_s": round(self.app.shutdown_state.uptime_s, 3),
                "in_flight": self.app.shutdown_state.in_flight,
            })
            return
        if method == "GET" and path in ("/readyz", "/startupz"):
            ready = not self.app.shutdown_state.draining
            await self._send_json(writer, 200 if ready else 503, {
                "ready": ready,
                "draining": self.app.shutdown_state.draining,
                "in_flight": self.app.shutdown_state.in_flight,
            })
            return
        if method == "GET" and path == "/manifest":
            await self._send_json(writer, 200, self.app.manifest())
            return
        if method == "GET" and path == "/openapi.json":
            await self._send_json(writer, 200, self.app.openapi())
            return
        if method == "GET" and path == "/metrics":
            accept = (headers.get("accept") or "").lower()
            # Prometheus scrape advertises "text/plain;version=0.0.4" — give
            # it the exposition format. Browser/curl get JSON.
            wants_prom = ("text/plain" in accept and "version=0.0.4" in accept) \
                         or accept.startswith("text/plain") \
                         or urllib.parse.parse_qs(url.query).get("format", [""])[0] == "prom"
            if wants_prom:
                body = PromExporter.render(self.app.metrics, self.app.name)
                await self._send(writer, 200, "OK", body.encode(),
                                 content_type="text/plain; version=0.0.4; charset=utf-8")
            else:
                await self._send_json(writer, 200, self.app.metrics.snapshot())
            return
        if method == "GET" and path == "/grimoire/verify":
            await self._send_json(writer, 200, self.app.grimoire.verify())
            return
        if method == "GET" and path == "/grimoire/head":
            await self._send_json(writer, 200,
                                  {"head": self.app.grimoire.head(),
                                   "pages": len(self.app.grimoire._pages)})
            return
        if method == "GET" and path == "/grimoire/pages":
            qs = urllib.parse.parse_qs(url.query)
            since = int(qs.get("since", ["0"])[0])
            limit = int(qs.get("limit", ["100"])[0])
            await self._send_json(writer, 200,
                                  {"pages": self.app.grimoire.pages(since, limit)})
            return
        if method == "GET" and path == "/calls":
            limit = int(urllib.parse.parse_qs(url.query).get("n", ["100"])[0])
            calls = [asdict(c) for c in list(self.app._recent_calls)[-limit:]]
            await self._send_json(writer, 200, {"calls": calls[::-1]})
            return
        if method == "GET" and path == "/dashboard":
            html = _build_dashboard_html(self.app)
            await self._send(writer, 200, "OK", html.encode(),
                             content_type="text/html; charset=utf-8")
            return

        # NEW: CPM Config endpoint
        if method == "GET" and path == "/cpm-config":
            qs = urllib.parse.parse_qs(url.query)
            base_url = qs.get("base_url", ["http://localhost:8765"])[0]
            yaml_str = self.app.cpm_config(base_url=base_url)
            await self._send(writer, 200, "OK", yaml_str.encode(),
                             content_type="text/yaml; charset=utf-8")
            return

        # NEW: Replay endpoint — trace_id se call dobara chalao
        if method == "POST" and path.startswith("/replay/"):
            trace_id = path[len("/replay/"):]
            await self._handle_replay(trace_id, headers, writer)
            return

        # Resources endpoint
        if method == "GET" and path.startswith("/resources"):
            resource_uri = urllib.parse.parse_qs(url.query).get("uri", [None])[0]
            if resource_uri:
                await self._handle_resource_read(resource_uri, headers, writer)
            else:
                resources = [
                    {"uri_template": r.uri_template, "name": r.name,
                     "description": r.description, "mime_type": r.mime_type}
                    for r in self.app._resources.values()
                ]
                await self._send_json(writer, 200, {"resources": resources})
            return

        # Prompts endpoint
        if method == "GET" and path == "/prompts":
            prompts = [
                {"name": p.name, "description": p.description, "arguments": p.arguments}
                for p in self.app._prompts.values()
            ]
            await self._send_json(writer, 200, {"prompts": prompts})
            return
        if method == "POST" and path.startswith("/prompts/"):
            prompt_name = path[len("/prompts/"):]
            await self._handle_prompt_get(prompt_name, headers, body, writer)
            return

        if path.startswith("/spells/") and method == "POST":
            spell_name = path[len("/spells/"):]
            await self._handle_call(spell_name, headers, body, writer, streaming=False)
            return
        if path.startswith("/stream/") and method == "POST":
            spell_name = path[len("/stream/"):]
            await self._handle_call(spell_name, headers, body, writer, streaming=True)
            return

        await self._send_error(writer, 404, "not found")

    async def _handle_replay(self, trace_id: str, headers: dict,
                              writer: asyncio.StreamWriter):
        """Pichli call ko trace_id se find karo aur dobara chalao."""
        record = next(
            (c for c in self.app._recent_calls if c.trace_id == trace_id), None)
        if record is None:
            await self._send_json(writer, 404,
                                  {"error": {"code": "not_found",
                                             "message": f"trace {trace_id} not found"}})
            return
        if record.args_snapshot is None:
            await self._send_json(writer, 400,
                                  {"error": {"code": "no_snapshot",
                                             "message": "args not stored for replay"}})
            return
        if record.spell not in self.app._spells:
            await self._send_json(writer, 404,
                                  {"error": {"code": "spell_not_found",
                                             "message": record.spell}})
            return
        spell = self.app._spells[record.spell]
        try:
            ctx = self.app._make_context(transport="replay",
                                         auth_header=headers.get("authorization"))
        except AuthError as e:
            await self._send_json(writer, e.status, e.to_dict())
            return
        log.info("replay call",
                 extra={"trace_id": ctx.trace_id, "spell": record.spell,
                        "original_trace": trace_id})
        try:
            result = await self.app._invoke_async(spell, record.args_snapshot, ctx)
            payload: t.Any
            if isinstance(result, SpellImage):
                payload = {"type": "image", "data": result.to_base64(),
                           "mime_type": result.mime_type}
            elif isinstance(result, SpellFile):
                payload = {"type": "file", "data": result.to_base64(),
                           "name": result.name, "mime_type": result.mime_type}
            else:
                payload = result
            await self._send_json(writer, 200,
                                  {"ok": True, "result": payload,
                                   "trace_id": ctx.trace_id,
                                   "replayed_from": trace_id,
                                   "original_spell": record.spell})
        except ConjureError as e:
            p = e.to_dict()
            p["trace_id"] = ctx.trace_id
            await self._send_json(writer, e.status, p)

    async def _handle_resource_read(self, uri: str, headers: dict,
                                    writer: asyncio.StreamWriter):
        """Resource read karo URI se."""
        matched_resource = None
        matched_vars: dict = {}
        for r in self.app._resources.values():
            vars_ = r.match(uri)
            if vars_ is not None:
                matched_resource = r
                matched_vars = vars_
                break
        if matched_resource is None:
            await self._send_json(writer, 404,
                                  {"error": {"code": "not_found",
                                             "message": f"no resource for URI: {uri}"}})
            return
        try:
            if matched_resource.is_async:
                content = await matched_resource.func(**matched_vars)
            else:
                loop = asyncio.get_running_loop()
                content = await loop.run_in_executor(
                    None, lambda: matched_resource.func(**matched_vars))
            if isinstance(content, bytes):
                await self._send(writer, 200, "OK", content,
                                content_type=matched_resource.mime_type)
            else:
                text = content if isinstance(content, str) else json.dumps(content)
                await self._send(writer, 200, "OK", text.encode(),
                                content_type=matched_resource.mime_type)
        except Exception as e:
            await self._send_json(writer, 500,
                                  {"error": {"code": "resource_error",
                                             "message": str(e)}})

    async def _handle_prompt_get(self, prompt_name: str, headers: dict,
                                 body: bytes, writer: asyncio.StreamWriter):
        """Prompt execute karo."""
        if prompt_name not in self.app._prompts:
            await self._send_json(writer, 404,
                                  {"error": {"code": "not_found",
                                             "message": f"prompt '{prompt_name}' not found"}})
            return
        prompt = self.app._prompts[prompt_name]
        try:
            args = json.loads(body or b"{}") if body else {}
        except json.JSONDecodeError:
            args = {}
        try:
            if prompt.is_async:
                result = await prompt.func(**args)
            else:
                loop = asyncio.get_running_loop()
                result = await loop.run_in_executor(None, lambda: prompt.func(**args))
            # Normalize to MCP messages format
            if isinstance(result, str):
                messages = [{"role": "user", "content": [{"type": "text", "text": result}]}]
            elif isinstance(result, list):
                messages = result
            else:
                messages = [{"role": "user",
                             "content": [{"type": "text",
                                          "text": json.dumps(result)}]}]
            await self._send_json(writer, 200,
                                  {"prompt": prompt_name, "messages": messages})
        except Exception as e:
            await self._send_json(writer, 500,
                                  {"error": {"code": "prompt_error", "message": str(e)}})

    async def _handle_call(self, spell_name: str, headers: dict,
                           body: bytes, writer: asyncio.StreamWriter,
                           streaming: bool):
        try:
            args = json.loads(body or b"{}")
            if not isinstance(args, dict):
                raise ValidationError("body must be a JSON object")
        except (json.JSONDecodeError, ValidationError) as e:
            await self._send_json(writer, 400,
                                  {"error": {"code": "bad_json", "message": str(e)}})
            return
        if spell_name not in self.app._spells:
            matches = self.app._suggest_spell(spell_name)
            err = {"code": "spell_not_found", "message": f"no such spell: {spell_name}"}
            if matches:
                err["did_you_mean"] = matches
                err["hint"] = f"Did you mean '{matches[0]}'?"
            else:
                err["hint"] = "Call GET /manifest for the full spell list."
            await self._send_json(writer, 404, {"error": err})
            return
        spell = self.app._spells[spell_name]
        if self.app.shutdown_state.draining:
            await self._send_json(writer, 503,
                                  {"error": {"code": "shutting_down",
                                             "message": "server is draining"}})
            return
        try:
            ctx = self.app._make_context(
                transport="http",
                auth_header=headers.get("authorization"),
                traceparent=headers.get("traceparent"),
                idempotency_key=headers.get("idempotency-key"),
            )
        except AuthError as e:
            await self._send_json(writer, e.status, e.to_dict())
            return

        if streaming:
            if not spell.is_streaming:
                await self._send_json(writer, 400,
                                      {"error": {"code": "not_streaming",
                                                 "message": f"{spell_name} is not streaming"}})
                return
            await self._send_sse_start(writer, ctx)
            try:
                async for chunk in self.app.invoke_stream(spell_name, args, _ctx=ctx):
                    await self._send_sse_event(writer, "chunk", chunk)
                await self._send_sse_event(writer, "done", {"trace_id": ctx.trace_id})
            except ConjureError as e:
                await self._send_sse_event(writer, "error", e.to_dict()["error"])
            except Exception as e:
                await self._send_sse_event(writer, "error",
                                           {"code": "internal_error", "message": str(e)})
            return

        try:
            result = await self.app._invoke_async(spell, args, ctx)
            # SpellImage / SpellFile special handling
            if isinstance(result, SpellImage):
                await self._send_json(writer, 200, {
                    "ok": True,
                    "result": {"type": "image", "data": result.to_base64(),
                               "mime_type": result.mime_type,
                               "alt_text": result.alt_text},
                    "trace_id": ctx.trace_id,
                })
            elif isinstance(result, SpellFile):
                await self._send_json(writer, 200, {
                    "ok": True,
                    "result": {"type": "file", "data": result.to_base64(),
                               "name": result.name, "mime_type": result.mime_type},
                    "trace_id": ctx.trace_id,
                })
            else:
                await self._send_json(writer, 200,
                                      {"ok": True, "result": result,
                                       "trace_id": ctx.trace_id})
        except ConjureError as e:
            p = e.to_dict()
            p["trace_id"] = ctx.trace_id
            await self._send_json(writer, e.status, p)
        except Exception:
            log.exception("unhandled spell exception",
                          extra={"trace_id": ctx.trace_id})
            await self._send_json(writer, 500,
                                  {"error": {"code": "internal_error",
                                             "message": "see server logs"},
                                   "trace_id": ctx.trace_id})

    async def _handle_websocket(self, reader, writer, path, headers):
        key = headers.get("sec-websocket-key", "")
        if not key:
            await self._send_error(writer, 400, "missing Sec-WebSocket-Key")
            return
        magic = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
        accept = base64.b64encode(
            hashlib.sha1((key + magic).encode()).digest()).decode()
        resp = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
        ).encode()
        writer.write(resp)
        await writer.drain()
        try:
            ctx: Context | None = None
            while True:
                frame = await _ws_read_frame(reader)
                if frame is None:
                    return
                opcode, payload = frame
                if opcode == 0x8:
                    await _ws_send_frame(writer, 0x8, b"")
                    return
                if opcode == 0x9:
                    await _ws_send_frame(writer, 0xA, payload)
                    continue
                if opcode != 0x1:
                    continue
                try:
                    msg = json.loads(payload.decode("utf-8"))
                except Exception:
                    await _ws_send_frame(writer, 0x1,
                                         json.dumps({"error": "bad json"}).encode())
                    continue
                if ctx is None:
                    try:
                        ctx = self.app._make_context(
                            transport="ws",
                            auth_header=(f"Bearer {msg.get('token')}"
                                         if msg.get("token") else None))
                        await _ws_send_frame(writer, 0x1, json.dumps({
                            "hello": self.app.name, "trace_id": ctx.trace_id,
                            "spells": [s.name for s in self.app._spells.values()]
                        }).encode())
                    except AuthError as e:
                        await _ws_send_frame(writer, 0x1,
                                             json.dumps(e.to_dict()).encode())
                        await _ws_send_frame(writer, 0x8, b"")
                        return
                    continue
                req_id = msg.get("id")
                spell_name = msg.get("spell")
                args = msg.get("args") or {}
                stream = bool(msg.get("stream"))
                if spell_name not in self.app._spells:
                    await _ws_send_frame(writer, 0x1, json.dumps({
                        "id": req_id,
                        "error": {"code": "spell_not_found", "message": spell_name}
                    }).encode())
                    continue
                spell = self.app._spells[spell_name]
                msg_ctx = Context(trace_id=uuid.uuid4().hex,
                                  subject=ctx.subject, scopes=list(ctx.scopes),
                                  transport="ws")
                try:
                    if stream and spell.is_streaming:
                        async for chunk in self.app.invoke_stream(spell_name, args, _ctx=msg_ctx):
                            await _ws_send_frame(writer, 0x1,
                                                 json.dumps({"id": req_id, "chunk": chunk}).encode())
                        await _ws_send_frame(writer, 0x1,
                                             json.dumps({"id": req_id, "done": True}).encode())
                    else:
                        result = await self.app._invoke_async(spell, args, msg_ctx)
                        await _ws_send_frame(writer, 0x1,
                                             json.dumps({"id": req_id, "result": result}).encode())
                except ConjureError as e:
                    await _ws_send_frame(writer, 0x1,
                                         json.dumps({"id": req_id,
                                                     "error": e.to_dict()["error"]}).encode())
                except Exception as e:
                    await _ws_send_frame(writer, 0x1,
                                         json.dumps({"id": req_id,
                                                     "error": {"code": "internal_error",
                                                                "message": str(e)}}).encode())
        except (asyncio.IncompleteReadError, ConnectionResetError):
            return

    async def _send(self, writer, status: int, status_text: str, body: bytes,
                    extra_headers: dict | None = None,
                    content_type: str = "application/json"):
        headers = {
            "Content-Type": content_type,
            "Content-Length": str(len(body)),
            "Connection": "close",
            "Server": f"shabd/{__version__}",
            "Access-Control-Allow-Origin": self.app.cors_origin,
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
        }
        if extra_headers:
            headers.update(extra_headers)
        line = f"HTTP/1.1 {status} {status_text}\r\n".encode()
        head = "".join(f"{k}: {v}\r\n" for k, v in headers.items()).encode()
        writer.write(line + head + b"\r\n" + body)
        await writer.drain()

    async def _send_json(self, writer, status: int, payload: dict):
        body = json.dumps(payload, default=str).encode()
        await self._send(writer, status, _STATUS_TEXT.get(status, "OK"), body)

    async def _send_error(self, writer, status: int, msg: str):
        await self._send_json(writer, status,
                              {"error": {"code": _STATUS_TEXT.get(status, "error"),
                                         "message": msg}})

    async def _send_sse_start(self, writer, ctx: Context):
        head = (
            "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
            "Cache-Control: no-cache\r\nConnection: keep-alive\r\n"
            f"Access-Control-Allow-Origin: {self.app.cors_origin}\r\n"
            f"X-Trace-Id: {ctx.trace_id}\r\n\r\n"
        )
        writer.write(head.encode())
        await writer.drain()

    async def _send_sse_event(self, writer, event: str, data: t.Any):
        chunk = f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"
        try:
            writer.write(chunk.encode())
            await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass


_STATUS_TEXT = {
    200: "OK", 204: "No Content", 400: "Bad Request", 401: "Unauthorized",
    403: "Forbidden", 404: "Not Found", 413: "Payload Too Large",
    429: "Too Many Requests", 500: "Internal Server Error",
    504: "Gateway Timeout",
}


async def _ws_read_frame(reader: asyncio.StreamReader) -> tuple[int, bytes] | None:
    try:
        header = await reader.readexactly(2)
    except asyncio.IncompleteReadError:
        return None
    b1, b2 = header[0], header[1]
    opcode = b1 & 0x0F
    masked = (b2 & 0x80) != 0
    plen = b2 & 0x7F
    if plen == 126:
        plen = struct.unpack(">H", await reader.readexactly(2))[0]
    elif plen == 127:
        plen = struct.unpack(">Q", await reader.readexactly(8))[0]
    if plen > 16 * 1024 * 1024:
        raise ConjureError("websocket frame too large")
    mask = await reader.readexactly(4) if masked else b""
    payload = await reader.readexactly(plen) if plen else b""
    if masked:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return opcode, payload


async def _ws_send_frame(writer: asyncio.StreamWriter, opcode: int, payload: bytes):
    header = bytearray()
    header.append(0x80 | (opcode & 0x0F))
    n = len(payload)
    if n < 126:
        header.append(n)
    elif n < 65536:
        header.append(126)
        header += struct.pack(">H", n)
    else:
        header.append(127)
        header += struct.pack(">Q", n)
    writer.write(bytes(header) + payload)
    await writer.drain()


# ============================================================================
# SECTION 17 — STDIO TRANSPORT
# ============================================================================

async def _stdio_loop(app: Conjure):
    loop = asyncio.get_running_loop()

    async def readline() -> str:
        return await loop.run_in_executor(None, sys.stdin.readline)

    while True:
        line = await readline()
        if not line:
            return
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            sys.stdout.write(json.dumps({"jsonrpc": "2.0",
                                         "error": {"code": -32700,
                                                   "message": "parse error"}}) + "\n")
            sys.stdout.flush()
            continue
        rpc_id = req.get("id")
        method = req.get("method")
        params = req.get("params") or {}
        try:
            if method == "manifest":
                result = app.manifest()
            elif method == "invoke":
                spell_name = params.get("spell")
                args = params.get("args") or {}
                token = params.get("token")
                if spell_name not in app._spells:
                    raise SpellNotFoundError(f"no such spell: {spell_name}")
                spell = app._spells[spell_name]
                ctx = app._make_context(transport="stdio",
                                        auth_header=f"Bearer {token}" if token else None)
                result = await app._invoke_async(spell, args, ctx)
            else:
                raise ConjureError(f"unknown method: {method}")
            response = {"jsonrpc": "2.0", "id": rpc_id, "result": result}
        except ConjureError as e:
            response = {"jsonrpc": "2.0", "id": rpc_id,
                        "error": {"code": -32000, "message": e.message, "data": e.details}}
        except Exception as e:
            response = {"jsonrpc": "2.0", "id": rpc_id,
                        "error": {"code": -32000, "message": str(e)}}
        sys.stdout.write(json.dumps(response, default=str) + "\n")
        sys.stdout.flush()


# ============================================================================
# SECTION 18 — MCP STDIO (UPDATED: Resources + Prompts support)
# ============================================================================

_MCP_PROTOCOL_VERSION = "2024-11-05"


def _spell_to_mcp_tool(spell: Spell) -> dict:
    return {
        "name": spell.name,
        "description": spell.description or spell.name,
        "inputSchema": spell.schema,
        "annotations": {
            "title": spell.name,
            "readOnlyHint": "read" in spell.tags,
            "destructiveHint": "destructive" in spell.tags,
            "idempotentHint": "idempotent" in spell.tags,
        },
    }


def _resource_to_mcp(resource: Resource, uri: str) -> dict:
    return {
        "uri": uri,
        "name": resource.name,
        "description": resource.description,
        "mimeType": resource.mime_type,
    }


def _prompt_to_mcp(prompt: Prompt) -> dict:
    return {
        "name": prompt.name,
        "description": prompt.description,
        "arguments": prompt.arguments,
    }


def _mcp_format_result(value: t.Any) -> dict:
    """Python value → MCP content blocks format."""
    if isinstance(value, SpellImage):
        # NEW: SpellImage → MCP image block
        return {
            "content": [{
                "type": "image",
                "data": value.to_base64(),
                "mimeType": value.mime_type,
            }],
            "isError": False,
        }
    if isinstance(value, SpellFile):
        # NEW: SpellFile → base64 text block with metadata
        return {
            "content": [{
                "type": "text",
                "text": json.dumps({
                    "type": "file",
                    "name": value.name,
                    "mime_type": value.mime_type,
                    "data_base64": value.to_base64(),
                }),
            }],
            "isError": False,
        }
    if isinstance(value, (dict, list)):
        text = json.dumps(value, default=str, ensure_ascii=False, indent=2)
    elif isinstance(value, bytes):
        return {
            "content": [{"type": "image", "data": base64.b64encode(value).decode(),
                         "mimeType": "application/octet-stream"}],
            "isError": False,
        }
    else:
        text = str(value) if value is not None else ""
    return {"content": [{"type": "text", "text": text}], "isError": False}


def _mcp_format_error(message: str) -> dict:
    return {"content": [{"type": "text", "text": message}], "isError": True}


async def _mcp_stdio_loop(app: Conjure, default_subject: str,
                          default_scopes: list[str]):
    """
    Full MCP protocol over stdio.
    Ab tools + resources + prompts sab handle karta hai.
    """
    loop = asyncio.get_running_loop()

    async def readline() -> str:
        return await loop.run_in_executor(None, sys.stdin.readline)

    def write(payload: dict):
        sys.stdout.write(json.dumps(payload, default=str, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    def respond(rpc_id: t.Any, result: t.Any):
        write({"jsonrpc": "2.0", "id": rpc_id, "result": result})

    def fail(rpc_id: t.Any, code: int, message: str, data: t.Any = None):
        err: dict = {"code": code, "message": message}
        if data is not None:
            err["data"] = data
        write({"jsonrpc": "2.0", "id": rpc_id, "error": err})

    while True:
        line = await readline()
        if not line:
            return
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            write({"jsonrpc": "2.0", "id": None,
                   "error": {"code": -32700, "message": "parse error"}})
            continue

        method = msg.get("method")
        rpc_id = msg.get("id")
        params = msg.get("params") or {}
        is_notification = "id" not in msg

        try:
            # ── Initialize ──────────────────────────────────────────────
            if method == "initialize":
                client_pv = params.get("protocolVersion", _MCP_PROTOCOL_VERSION)
                pv = client_pv if client_pv >= "2024-01-01" else _MCP_PROTOCOL_VERSION
                respond(rpc_id, {
                    "protocolVersion": pv,
                    "capabilities": {
                        "tools": {"listChanged": False},
                        "resources": {"subscribe": False, "listChanged": False},
                        "prompts": {"listChanged": False},
                    },
                    "serverInfo": {"name": app.name, "version": __version__},
                })
                continue

            if method == "notifications/initialized":
                continue

            if method and method.startswith("notifications/"):
                continue

            if method == "ping":
                if not is_notification:
                    respond(rpc_id, {})
                continue

            # ── Tools ────────────────────────────────────────────────────
            if method == "tools/list":
                tools = [_spell_to_mcp_tool(s) for s in app._spells.values()]
                respond(rpc_id, {"tools": tools})
                continue

            if method == "tools/call":
                tool_name = params.get("name")
                args = params.get("arguments") or {}
                if not isinstance(args, dict):
                    fail(rpc_id, -32602, "arguments must be an object")
                    continue
                if tool_name not in app._spells:
                    respond(rpc_id, _mcp_format_error(f"unknown tool: {tool_name}"))
                    continue
                spell = app._spells[tool_name]
                ctx = Context(trace_id=uuid.uuid4().hex, subject=default_subject,
                              scopes=list(default_scopes), transport="mcp")
                try:
                    value = await app._invoke_async(spell, args, ctx)
                    respond(rpc_id, _mcp_format_result(value))
                except ConjureError as e:
                    respond(rpc_id, _mcp_format_error(f"{e.code}: {e.message}"))
                except Exception as e:
                    log.exception("mcp tool crash")
                    respond(rpc_id, _mcp_format_error(f"internal error: {e}"))
                continue

            # ── Resources (NEW — pehle empty tha) ────────────────────────
            if method == "resources/list":
                # Template-based resources expand karke list karo
                resources_list = []
                for r in app._resources.values():
                    # Agar URI mein variables nahi hain to directly list karo
                    if not r.uri_vars():
                        resources_list.append({
                            "uri": r.uri_template,
                            "name": r.name,
                            "description": r.description,
                            "mimeType": r.mime_type,
                        })
                    else:
                        # Template resources
                        resources_list.append({
                            "uri": r.uri_template,
                            "name": r.name,
                            "description": f"{r.description} (template: {r.uri_template})",
                            "mimeType": r.mime_type,
                        })
                respond(rpc_id, {"resources": resources_list,
                                 "resourceTemplates": [
                                     {"uriTemplate": r.uri_template,
                                      "name": r.name,
                                      "description": r.description,
                                      "mimeType": r.mime_type}
                                     for r in app._resources.values()
                                     if r.uri_vars()
                                 ]})
                continue

            if method == "resources/read":
                uri = params.get("uri", "")
                matched_resource = None
                matched_vars: dict = {}
                for r in app._resources.values():
                    vars_ = r.match(uri)
                    if vars_ is not None:
                        matched_resource = r
                        matched_vars = vars_
                        break
                if matched_resource is None:
                    fail(rpc_id, -32602, f"no resource matches URI: {uri}")
                    continue
                call_kwargs = dict(matched_vars)
                if matched_resource.wants_context:
                    ctx = Context(trace_id=uuid.uuid4().hex,
                                  subject=default_subject,
                                  scopes=list(default_scopes),
                                  transport="mcp")
                    call_kwargs["ctx"] = ctx
                try:
                    if matched_resource.is_async:
                        content = await matched_resource.func(**call_kwargs)
                    else:
                        content = await loop.run_in_executor(
                            None, lambda: matched_resource.func(**call_kwargs))
                    if isinstance(content, bytes):
                        # Binary → base64 blob
                        blob_data = base64.b64encode(content).decode()
                        respond(rpc_id, {
                            "contents": [{
                                "uri": uri, "mimeType": matched_resource.mime_type,
                                "blob": blob_data,
                            }]
                        })
                    else:
                        text = content if isinstance(content, str) else json.dumps(content)
                        respond(rpc_id, {
                            "contents": [{
                                "uri": uri, "mimeType": matched_resource.mime_type,
                                "text": text,
                            }]
                        })
                except Exception as e:
                    fail(rpc_id, -32603, f"resource read failed: {e}")
                continue

            if method in ("resources/subscribe", "resources/unsubscribe"):
                respond(rpc_id, {})
                continue

            # ── Prompts (NEW — pehle empty tha) ──────────────────────────
            if method == "prompts/list":
                prompts_list = [_prompt_to_mcp(p) for p in app._prompts.values()]
                respond(rpc_id, {"prompts": prompts_list})
                continue

            if method == "prompts/get":
                prompt_name = params.get("name")
                if not prompt_name or prompt_name not in app._prompts:
                    fail(rpc_id, -32602, f"unknown prompt: {prompt_name}")
                    continue
                prompt = app._prompts[prompt_name]
                prompt_args = params.get("arguments") or {}
                try:
                    if prompt.is_async:
                        result = await prompt.func(**prompt_args)
                    else:
                        result = await loop.run_in_executor(
                            None, lambda: prompt.func(**prompt_args))
                    # Normalize to MCP messages format
                    if isinstance(result, str):
                        messages = [{"role": "user",
                                     "content": {"type": "text", "text": result}}]
                    elif isinstance(result, list):
                        # List of dicts ya list of strings
                        messages = []
                        for item in result:
                            if isinstance(item, dict):
                                messages.append(item)
                            else:
                                messages.append({"role": "user",
                                                 "content": {"type": "text",
                                                             "text": str(item)}})
                    else:
                        messages = [{"role": "user",
                                     "content": {"type": "text",
                                                 "text": json.dumps(result)}}]
                    respond(rpc_id, {
                        "description": prompt.description,
                        "messages": messages,
                    })
                except Exception as e:
                    fail(rpc_id, -32603, f"prompt failed: {e}")
                continue

            # ── Misc ─────────────────────────────────────────────────────
            if method in ("completion/complete", "logging/setLevel"):
                respond(rpc_id, {})
                continue

            if not is_notification:
                fail(rpc_id, -32601, f"method not found: {method}")

        except Exception as e:
            log.exception("mcp dispatch crash")
            if not is_notification:
                fail(rpc_id, -32603, f"internal error: {e}")


# ============================================================================
# SECTION 19 — LIVE DASHBOARD (UPDATED: Resources + Prompts tabs + Playground)
# ============================================================================

def _build_dashboard_html(app: Conjure) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{app.name} — SHABD Dashboard</title>
<style>
  :root {{
    --bg: #0d1117; --surface: #161b22; --border: #30363d;
    --text: #e6edf3; --muted: #8b949e; --accent: #7c3aed;
    --green: #3fb950; --red: #f85149; --yellow: #d29922;
    --blue: #58a6ff; --teal: #39d0d8; --font: 'Segoe UI', system-ui, sans-serif;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: var(--bg); color: var(--text); font-family: var(--font); font-size: 14px; }}
  header {{
    padding: 14px 24px; background: var(--surface); border-bottom: 1px solid var(--border);
    display: flex; align-items: center; justify-content: space-between;
  }}
  .logo {{ display: flex; align-items: center; gap: 10px; }}
  .logo-icon {{ font-size: 20px; }}
  .logo-name {{ font-size: 17px; font-weight: 600; }}
  .logo-badge {{
    font-size: 11px; background: var(--accent); color: #fff; border-radius: 20px;
    padding: 2px 10px; font-weight: 600;
  }}
  .main-tabs {{ display: flex; gap: 4px; padding: 12px 24px 0; background: var(--surface); border-bottom: 1px solid var(--border); }}
  .mtab {{
    padding: 8px 18px; border-radius: 6px 6px 0 0; cursor: pointer; font-size: 13px;
    color: var(--muted); background: transparent; border: none; font-family: inherit;
    transition: all .15s;
  }}
  .mtab.active {{ color: var(--text); background: var(--bg); border: 1px solid var(--border); border-bottom: 1px solid var(--bg); }}
  .mtab-badge {{
    font-size: 10px; background: var(--border); color: var(--muted); border-radius: 10px;
    padding: 1px 6px; margin-left: 4px;
  }}
  .main {{ padding: 20px 24px; display: grid; gap: 16px; }}
  .tab-section {{ display: none; }}
  .tab-section.active {{ display: block; }}
  .row-2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
  .row-3 {{ display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 16px; }}
  @media (max-width: 900px) {{ .row-2, .row-3 {{ grid-template-columns: 1fr; }} }}
  .card {{
    background: var(--surface); border: 1px solid var(--border); border-radius: 8px;
    padding: 18px;
  }}
  .card-title {{
    font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 1px;
    color: var(--muted); margin-bottom: 14px; display: flex; align-items: center; gap: 8px;
  }}
  .stats {{ display: flex; gap: 10px; flex-wrap: wrap; }}
  .stat {{
    flex: 1; min-width: 90px; background: var(--bg); border: 1px solid var(--border);
    border-radius: 6px; padding: 10px 14px; text-align: center;
  }}
  .stat-value {{ font-size: 24px; font-weight: 700; color: var(--blue); }}
  .stat-label {{ font-size: 11px; color: var(--muted); margin-top: 3px; }}
  .spell-card {{
    background: var(--bg); border: 1px solid var(--border); border-radius: 6px;
    padding: 12px 14px; margin-bottom: 8px; display: flex; align-items: flex-start; gap: 10px;
  }}
  .spell-icon {{ font-size: 16px; margin-top: 2px; flex-shrink: 0; }}
  .spell-info {{ flex: 1; min-width: 0; }}
  .spell-name {{ font-weight: 600; color: var(--text); font-size: 13px; }}
  .spell-desc {{ color: var(--muted); font-size: 12px; margin-top: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
  .spell-tags {{ display: flex; gap: 5px; margin-top: 6px; flex-wrap: wrap; }}
  .tag {{ font-size: 10px; font-weight: 600; padding: 2px 6px; border-radius: 20px; }}
  .tag-read {{ background: #0e4429; color: var(--green); }}
  .tag-destructive {{ background: #2d0a0a; color: var(--red); }}
  .tag-chain {{ background: #1a1f3a; color: #a78bfa; }}
  .tag-group {{ background: #1a2a2a; color: var(--teal); }}
  .tag-normal {{ background: #1a1f2e; color: var(--blue); }}
  .spell-metrics {{ font-size: 11px; color: var(--muted); text-align: right; flex-shrink: 0; }}
  .spell-metrics span {{ display: block; }}
  .metric-ok {{ color: var(--green); }}
  .metric-err {{ color: var(--red); }}
  .calls-table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
  .calls-table th {{
    text-align: left; padding: 6px 10px; color: var(--muted); font-weight: 600;
    font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px;
    border-bottom: 1px solid var(--border);
  }}
  .calls-table td {{ padding: 7px 10px; border-bottom: 1px solid #1c2128; }}
  .calls-table tr:hover td {{ background: #1c2128; }}
  .replay-btn {{
    font-size: 11px; padding: 3px 8px; border-radius: 4px; cursor: pointer;
    background: transparent; border: 1px solid var(--border); color: var(--muted);
    font-family: inherit;
  }}
  .replay-btn:hover {{ background: var(--border); color: var(--text); }}
  .badge-ok {{ color: var(--green); font-weight: 600; }}
  .badge-err {{ color: var(--red); font-weight: 600; }}
  .trace {{ font-family: monospace; font-size: 10px; color: var(--muted); }}
  .lat-row {{ display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }}
  .lat-name {{ width: 120px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--muted); font-size: 12px; }}
  .lat-bar-wrap {{ flex: 1; background: var(--bg); border-radius: 4px; height: 7px; overflow: hidden; }}
  .lat-bar {{ height: 100%; background: linear-gradient(90deg, var(--accent), var(--blue)); border-radius: 4px; transition: width .3s; }}
  .lat-val {{ width: 56px; text-align: right; font-size: 12px; color: var(--blue); }}
  .empty {{ color: var(--muted); font-size: 12px; padding: 16px 0; text-align: center; }}
  /* Playground */
  .playground {{
    background: var(--bg); border: 1px solid var(--border); border-radius: 8px; padding: 16px;
  }}
  .pg-select {{
    width: 100%; padding: 8px 10px; background: var(--surface); border: 1px solid var(--border);
    color: var(--text); border-radius: 6px; font-family: inherit; font-size: 13px;
    margin-bottom: 12px; cursor: pointer;
  }}
  .pg-fields {{ display: grid; gap: 10px; margin-bottom: 14px; }}
  .pg-field label {{ display: block; font-size: 11px; color: var(--muted); margin-bottom: 4px; text-transform: uppercase; letter-spacing: 0.5px; }}
  .pg-field input, .pg-field textarea, .pg-field select {{
    width: 100%; padding: 7px 10px; background: var(--surface); border: 1px solid var(--border);
    color: var(--text); border-radius: 6px; font-family: inherit; font-size: 13px;
  }}
  .pg-run-btn {{
    padding: 8px 22px; background: var(--accent); color: #fff; border: none; border-radius: 6px;
    cursor: pointer; font-size: 13px; font-weight: 600; font-family: inherit;
    transition: opacity .15s;
  }}
  .pg-run-btn:hover {{ opacity: 0.88; }}
  .pg-result {{
    margin-top: 14px; background: var(--bg); border: 1px solid var(--border); border-radius: 6px;
    padding: 12px; font-family: monospace; font-size: 12px; color: var(--text);
    white-space: pre-wrap; max-height: 300px; overflow-y: auto;
    display: none;
  }}
  .pg-result.visible {{ display: block; }}
  /* Resource/Prompt cards */
  .res-card {{
    background: var(--bg); border: 1px solid var(--border); border-radius: 6px;
    padding: 12px 14px; margin-bottom: 8px;
  }}
  .res-uri {{ font-family: monospace; font-size: 12px; color: var(--teal); }}
  .res-name {{ font-weight: 600; font-size: 13px; }}
  .res-desc {{ color: var(--muted); font-size: 12px; margin-top: 3px; }}
  .res-mime {{ font-size: 11px; color: var(--muted); background: var(--border); padding: 2px 6px; border-radius: 4px; }}
  .chain-card {{
    background: var(--bg); border: 1px solid var(--border); border-radius: 6px;
    padding: 12px 14px; margin-bottom: 8px;
  }}
  .chain-steps {{ display: flex; gap: 6px; flex-wrap: wrap; margin-top: 6px; align-items: center; }}
  .chain-step {{ background: #1a1f3a; color: #a78bfa; font-size: 11px; padding: 3px 8px; border-radius: 4px; font-weight: 600; }}
  .chain-arrow {{ color: var(--muted); font-size: 12px; }}
  .refresh-bar {{
    font-size: 11px; color: var(--muted); display: flex; align-items: center;
    gap: 8px; margin-bottom: 16px;
  }}
  .dot-pulse {{
    display: inline-block; width: 7px; height: 7px; border-radius: 50%;
    background: var(--accent); animation: pulse 1.4s infinite;
  }}
  @keyframes pulse {{
    0% {{ box-shadow: 0 0 0 0 rgba(124,58,237,0.7); }}
    70% {{ box-shadow: 0 0 0 7px rgba(124,58,237,0); }}
    100% {{ box-shadow: 0 0 0 0 rgba(124,58,237,0); }}
  }}
</style>
</head>
<body>
<header>
  <div class="logo">
    <div class="logo-icon">🔮</div>
    <div class="logo-name">{app.name}</div>
    <div class="logo-badge">SHABD v{__version__}</div>
  </div>
  <div style="font-size:12px;color:var(--muted)"><span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--green);margin-right:6px;box-shadow:0 0 8px var(--green)"></span>Live</div>
</header>

<div class="main-tabs">
  <button class="mtab active" onclick="switchTab('overview')">Overview</button>
  <button class="mtab" onclick="switchTab('spells')">Spells <span class="mtab-badge" id="badge-spells">—</span></button>
  <button class="mtab" onclick="switchTab('resources')">Resources <span class="mtab-badge" id="badge-res">—</span></button>
  <button class="mtab" onclick="switchTab('prompts')">Prompts <span class="mtab-badge" id="badge-prom">—</span></button>
  <button class="mtab" onclick="switchTab('playground')">🎮 Playground</button>
  <button class="mtab" onclick="switchTab('calls')">Recent Calls</button>
</div>

<div class="main">

<!-- ── OVERVIEW ── -->
<div id="tab-overview" class="tab-section active">
  <div class="refresh-bar">
    <div class="dot-pulse"></div>Auto-refreshing every 2s
    <span style="margin-left:auto;font-size:11px" id="last-refresh">—</span>
  </div>
  <div class="stats" id="top-stats">
    <div class="stat"><div class="stat-value" id="s-spells">—</div><div class="stat-label">Spells</div></div>
    <div class="stat"><div class="stat-value" id="s-res" style="color:var(--teal)">—</div><div class="stat-label">Resources</div></div>
    <div class="stat"><div class="stat-value" id="s-prom" style="color:var(--yellow)">—</div><div class="stat-label">Prompts</div></div>
    <div class="stat"><div class="stat-value" id="s-calls">—</div><div class="stat-label">Total Calls</div></div>
    <div class="stat"><div class="stat-value" id="s-errors" style="color:var(--red)">—</div><div class="stat-label">Errors</div></div>
    <div class="stat"><div class="stat-value" id="s-chains" style="color:#a78bfa">—</div><div class="stat-label">Chains</div></div>
  </div>
  <div class="row-2" style="margin-top:16px">
    <div class="card">
      <div class="card-title">⚡ p95 Latency</div>
      <div id="latency-bars"><div class="empty">no calls yet</div></div>
    </div>
    <div class="card">
      <div class="card-title">📊 Counters</div>
      <div id="counters-view"><div class="empty">no events yet</div></div>
    </div>
  </div>
</div>

<!-- ── SPELLS ── -->
<div id="tab-spells" class="tab-section">
  <div class="card">
    <div class="card-title">✨ Registered Spells</div>
    <div id="spells-list"><div class="empty">loading…</div></div>
  </div>
  <div class="card" style="margin-top:12px">
    <div class="card-title">🔗 Spell Chains</div>
    <div id="chains-list"><div class="empty">no chains registered</div></div>
  </div>
</div>

<!-- ── RESOURCES ── -->
<div id="tab-resources" class="tab-section">
  <div class="card">
    <div class="card-title">📂 MCP Resources</div>
    <div id="resources-list"><div class="empty">loading…</div></div>
  </div>
</div>

<!-- ── PROMPTS ── -->
<div id="tab-prompts" class="tab-section">
  <div class="card">
    <div class="card-title">💬 MCP Prompts</div>
    <div id="prompts-list"><div class="empty">loading…</div></div>
  </div>
</div>

<!-- ── PLAYGROUND ── -->
<div id="tab-playground" class="tab-section">
  <div class="card">
    <div class="card-title">🎮 Spell Playground — Live Testing</div>
    <div class="playground">
      <select class="pg-select" id="pg-spell-select" onchange="buildPlaygroundForm()">
        <option value="">— Spell chunno —</option>
      </select>
      <div id="pg-fields" class="pg-fields"></div>
      <div style="display:flex;gap:12px;align-items:center">
        <button class="pg-run-btn" onclick="runPlayground()">▶ Run Spell</button>
        <span id="pg-status" style="font-size:12px;color:var(--muted)"></span>
      </div>
      <pre id="pg-result" class="pg-result"></pre>
    </div>
  </div>
</div>

<!-- ── CALLS ── -->
<div id="tab-calls" class="tab-section">
  <div class="card">
    <div class="card-title">📋 Recent Calls (last 100)</div>
    <div style="overflow-x:auto">
    <table class="calls-table">
      <thead><tr>
        <th>Trace</th><th>Spell</th><th>Subject</th><th>Transport</th>
        <th>Status</th><th>ms</th><th>Time</th><th>Replay</th>
      </tr></thead>
      <tbody id="calls-body"><tr><td colspan="8" style="text-align:center;padding:20px;color:var(--muted)">loading…</td></tr></tbody>
    </table>
    </div>
  </div>
</div>

</div><!-- /main -->

<script>
let manifestData = null;
const _em={60:'&lt;',62:'&gt;',38:'&amp;',34:'&quot;'};const esc=s=>String(s).replace(/[<>&"]/g,c=>_em[c.charCodeAt(0)]);
const fmt = v => v==null?'—':(v>=1000?(v/1000).toFixed(2)+'s':v.toFixed(1)+'ms');

function switchTab(id) {{
  document.querySelectorAll('.mtab').forEach((t,i)=>{{
    const ids=['overview','spells','resources','prompts','playground','calls'];
    t.classList.toggle('active', ids[i]===id);
  }});
  document.querySelectorAll('.tab-section').forEach(s=>{{
    s.classList.toggle('active', s.id==='tab-'+id);
  }});
}}

async function fetchJSON(p) {{
  try {{ const r=await fetch(p); return r.ok?await r.json():null; }} catch {{ return null; }}
}}

function buildPlaygroundForm() {{
  if (!manifestData) return;
  const name = document.getElementById('pg-spell-select').value;
  const fieldsDiv = document.getElementById('pg-fields');
  document.getElementById('pg-result').classList.remove('visible');
  document.getElementById('pg-status').textContent = '';
  if (!name) {{ fieldsDiv.innerHTML = ''; return; }}
  const spell = manifestData.spells.find(s=>s.name===name);
  if (!spell || !spell.input_schema) {{ fieldsDiv.innerHTML=''; return; }}
  const props = spell.input_schema.properties || {{}};
  const required = spell.input_schema.required || [];
  let html = '';
  for (const [pname, pschema] of Object.entries(props)) {{
    const req = required.includes(pname);
    const typeHint = pschema.type || 'any';
    const def = pschema.default !== undefined ? pschema.default : '';
    if (pschema.enum) {{
      const opts = pschema.enum.map(v=>`<option value="${{esc(v)}}">${{esc(v)}}</option>`).join('');
      html += `<div class="pg-field"><label>${{esc(pname)}} <span style="color:var(--teal)">(enum)</span>${{req?' <span style="color:var(--red)">*</span>':''}}</label><select id="pg-field-${{esc(pname)}}">${{opts}}</select></div>`;
    }} else if (typeHint === 'boolean') {{
      html += `<div class="pg-field"><label>${{esc(pname)}} <span style="color:var(--teal)">(boolean)</span></label><select id="pg-field-${{esc(pname)}}"><option value="true">true</option><option value="false">false</option></select></div>`;
    }} else if (typeHint === 'object' || typeHint === 'array') {{
      html += `<div class="pg-field"><label>${{esc(pname)}} <span style="color:var(--teal)">(JSON)</span>${{req?' <span style="color:var(--red)">*</span>':''}}</label><textarea id="pg-field-${{esc(pname)}}" rows="3" placeholder='${{typeHint==="array"?"[...]":'{{...}}'}}'>${{esc(JSON.stringify(def)!=='null'?JSON.stringify(def):'') }}</textarea></div>`;
    }} else {{
      html += `<div class="pg-field"><label>${{esc(pname)}} <span style="color:var(--teal)">(${{typeHint}})</span>${{req?' <span style="color:var(--red)">*</span>':''}}</label><input type="text" id="pg-field-${{esc(pname)}}" value="${{esc(String(def))}}" placeholder="${{typeHint}}"></div>`;
    }}
  }}
  if (!html) html = '<div style="color:var(--muted);font-size:12px">Yeh spell koi parameter nahi leta.</div>';
  fieldsDiv.innerHTML = html;
}}

async function runPlayground() {{
  const name = document.getElementById('pg-spell-select').value;
  if (!name) return;
  const spell = manifestData?.spells.find(s=>s.name===name);
  if (!spell) return;
  const props = spell.input_schema?.properties || {{}};
  const args = {{}};
  for (const pname of Object.keys(props)) {{
    const el = document.getElementById('pg-field-'+pname);
    if (!el) continue;
    const v = el.value.trim();
    const typeHint = props[pname].type||'';
    if (typeHint==='integer') args[pname]=parseInt(v)||0;
    else if (typeHint==='number') args[pname]=parseFloat(v)||0;
    else if (typeHint==='boolean') args[pname]=v==='true';
    else if (typeHint==='object'||typeHint==='array') {{
      try {{ args[pname]=JSON.parse(v); }} catch {{ args[pname]=v; }}
    }} else args[pname]=v;
  }}
  const statusEl = document.getElementById('pg-status');
  const resultEl = document.getElementById('pg-result');
  statusEl.textContent = '⏳ calling...';
  resultEl.classList.remove('visible');
  const t0 = Date.now();
  try {{
    const r = await fetch('/spells/'+name, {{
      method:'POST', headers:{{'Content-Type':'application/json'}},
      body: JSON.stringify(args)
    }});
    const data = await r.json();
    const ms = Date.now()-t0;
    statusEl.textContent = (data.ok?'✅':'❌')+' '+ms+'ms';
    resultEl.textContent = JSON.stringify(data, null, 2);
    resultEl.classList.add('visible');
  }} catch(e) {{
    statusEl.textContent = '❌ Network error';
    resultEl.textContent = String(e);
    resultEl.classList.add('visible');
  }}
}}

async function replayCall(traceId) {{
  const btn = document.getElementById('replay-'+traceId);
  if (btn) btn.textContent = '⏳';
  try {{
    const r = await fetch('/replay/'+traceId, {{method:'POST'}});
    const data = await r.json();
    alert(JSON.stringify(data.result ?? data.error, null, 2).slice(0,800));
  }} catch(e) {{ alert('Replay failed: '+e); }}
  if (btn) btn.textContent = '↺ Replay';
}}

async function refresh() {{
  const [manifest, metrics, calls] = await Promise.all([
    fetchJSON('/manifest'), fetchJSON('/metrics'), fetchJSON('/calls?n=100'),
  ]);
  const now = new Date().toLocaleTimeString();
  document.getElementById('last-refresh').textContent = 'Updated '+now;

  if (manifest) {{
    manifestData = manifest;
    const ns = manifest.spells?.length||0;
    const nr = manifest.resources?.length||0;
    const np = manifest.prompts?.length||0;
    const nc = manifest.chains?.length||0;
    document.getElementById('s-spells').textContent = ns;
    document.getElementById('s-res').textContent = nr;
    document.getElementById('s-prom').textContent = np;
    document.getElementById('s-chains').textContent = nc;
    document.getElementById('badge-spells').textContent = ns;
    document.getElementById('badge-res').textContent = nr;
    document.getElementById('badge-prom').textContent = np;

    // Playground spell list update
    const sel = document.getElementById('pg-spell-select');
    const prev = sel.value;
    sel.innerHTML = '<option value="">— Spell chunno —</option>';
    (manifest.spells||[]).forEach(s=>{{
      const opt = document.createElement('option');
      opt.value=s.name; opt.textContent=s.name+(s.group?' ['+s.group+']':'');
      sel.appendChild(opt);
    }});
    if (prev) sel.value=prev;
  }}

  if (metrics) {{
    const ctrs=metrics.counters||{{}};
    let ok=0,err=0;
    Object.entries(ctrs).forEach(([k,v])=>{{if(k.endsWith('.ok'))ok+=v;if(k.endsWith('.error'))err+=v;}});
    document.getElementById('s-calls').textContent=ok+err;
    document.getElementById('s-errors').textContent=err;
    // Latency bars
    const timings=metrics.timings||{{}};
    const keys=Object.keys(timings).filter(k=>k.startsWith('spell.'));
    if(keys.length===0) {{
      document.getElementById('latency-bars').innerHTML='<div class="empty">no calls yet</div>';
    }} else {{
      const maxP95=Math.max(...keys.map(k=>timings[k].p95||0),1);
      document.getElementById('latency-bars').innerHTML=keys.map(k=>{{
        const name=k.replace('spell.','');
        const p95=timings[k].p95||0;
        const pct=Math.min(100,(p95/maxP95)*100);
        return `<div class="lat-row"><div class="lat-name" title="${{esc(name)}}">${{esc(name)}}</div><div class="lat-bar-wrap"><div class="lat-bar" style="width:${{pct}}%"></div></div><div class="lat-val">${{fmt(p95)}}</div></div>`;
      }}).join('');
    }}
    const interesting=Object.entries(ctrs).filter(([k])=>k.includes('cache_hit')||k.includes('rate'));
    if(interesting.length===0) {{
      document.getElementById('counters-view').innerHTML='<div class="empty">no events yet</div>';
    }} else {{
      document.getElementById('counters-view').innerHTML=interesting.map(([k,v])=>
        `<div style="display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid #1c2128"><span style="color:var(--muted);font-size:12px">${{esc(k.replace('spell.',''))}}</span><span style="color:var(--blue)">${{v}}</span></div>`
      ).join('');
    }}
  }}

  if (manifest) {{
    // Spells tab
    const ctrs=metrics?.counters||{{}};
    const timings=metrics?.timings||{{}};
    document.getElementById('spells-list').innerHTML = (manifest.spells||[]).map(s=>{{
      const icon='chain' in (s.tags||[])?'🔗':s.streaming?'🌊':s.tags?.includes('destructive')?'⚠️':s.tags?.includes('read')?'📖':'✨';
      const ok=ctrs['spell.'+s.name+'.ok']||0;
      const err=ctrs['spell.'+s.name+'.error']||0;
      const p95=timings['spell.'+s.name]?.p95;
      let tagHtml=(s.tags||[]).map(t=>`<span class="tag tag-${{t.startsWith('group:')?'group':t}}">${{esc(t)}}</span>`).join('');
      if(s.async) tagHtml+='<span class="tag tag-normal">async</span>';
      if(s.streaming) tagHtml+='<span class="tag tag-normal">stream</span>';
      return `<div class="spell-card"><div class="spell-icon">${{icon}}</div><div class="spell-info"><div class="spell-name">${{esc(s.name)}}</div><div class="spell-desc">${{esc(s.description||'')}}</div><div class="spell-tags">${{tagHtml}}</div></div><div class="spell-metrics"><span class="metric-ok">✓ ${{ok}}</span><span class="metric-err">✗ ${{err}}</span>${{p95!=null?`<span>${{p95.toFixed(1)}}ms</span>`:''}}</div></div>`;
    }}).join('')||'<div class="empty">no spells</div>';

    // Chains tab
    document.getElementById('chains-list').innerHTML = (manifest.chains||[]).map(c=>{{
      const steps=c.steps||[];
      let stepsHtml='';
      steps.forEach((s,i)=>{{
        stepsHtml+=`<span class="chain-step">${{esc(s)}}</span>`;
        if(i<steps.length-1) stepsHtml+='<span class="chain-arrow">→</span>';
      }});
      return `<div class="chain-card"><div style="font-weight:600;font-size:13px">${{esc(c.name)}}</div><div style="color:var(--muted);font-size:12px;margin-top:3px">${{esc(c.description||'')}}</div><div class="chain-steps">${{stepsHtml}}</div></div>`;
    }}).join('')||'<div class="empty">no chains registered</div>';

    // Resources tab
    document.getElementById('resources-list').innerHTML = (manifest.resources||[]).map(r=>
      `<div class="res-card"><div style="display:flex;justify-content:space-between;align-items:flex-start"><div><div class="res-uri">${{esc(r.uri_template)}}</div><div class="res-name">${{esc(r.name)}}</div><div class="res-desc">${{esc(r.description||'')}}</div></div><span class="res-mime">${{esc(r.mime_type)}}</span></div></div>`
    ).join('')||'<div class="empty">koi resources register nahi hain<br><small>@app.resource("/docs/{{slug}}") se register karo</small></div>';

    // Prompts tab
    document.getElementById('prompts-list').innerHTML = (manifest.prompts||[]).map(p=>{{
      const args=(p.arguments||[]).map(a=>`<span class="tag tag-normal">${{esc(a.name)}}${{a.required?'*':''}}</span>`).join(' ');
      return `<div class="res-card"><div class="res-name">${{esc(p.name)}}</div><div class="res-desc">${{esc(p.description||'')}}</div><div style="margin-top:6px">${{args}}</div></div>`;
    }}).join('')||'<div class="empty">koi prompts register nahi hain<br><small>@app.prompt("name") se register karo</small></div>';
  }}

  if (calls) {{
    const rows=calls.calls||[];
    document.getElementById('calls-body').innerHTML=rows.map(c=>{{
      const t=new Date(c.ts*1000);
      const ts=t.toLocaleTimeString()+'.'+String(t.getMilliseconds()).padStart(3,'0');
      return `<tr><td class="trace">${{esc(c.trace_id.slice(0,8))}}</td><td>${{esc(c.spell)}}</td><td style="color:var(--blue);font-size:11px">${{esc(c.subject)}}</td><td style="color:var(--muted)">${{esc(c.transport)}}</td><td>${{c.ok?'<span class="badge-ok">✓</span>':'<span class="badge-err">✗ '+esc(c.error_code||'')+'</span>'}}</td><td style="color:var(--yellow)">${{c.elapsed_ms.toFixed(1)}}</td><td style="color:var(--muted);font-size:11px">${{esc(ts)}}</td><td><button class="replay-btn" id="replay-${{esc(c.trace_id)}}" onclick="replayCall('${{esc(c.trace_id)}}')">↺ Replay</button></td></tr>`;
    }}).join('')||'<tr><td colspan="8" style="text-align:center;padding:20px;color:var(--muted)">no calls yet</td></tr>';
  }}
}}

refresh();
setInterval(refresh,2000);
</script>
</body></html>"""


# ============================================================================
# SECTION 20 — REDIS PLUGIN
# ============================================================================

class RedisPlugin(ConjurePlugin):
    """Distributed rate limiting + caching via Redis."""

    def __init__(self, url: str = "redis://localhost:6379",
                 key_prefix: str = "shabd:"):
        self.url = url
        self.prefix = key_prefix
        self._r = None

    def _connect(self) -> bool:
        if self._r is not None:
            return True
        try:
            import redis  # type: ignore
            self._r = redis.Redis.from_url(self.url, decode_responses=True,
                                           socket_connect_timeout=2, socket_timeout=1)
            self._r.ping()
            log.info("RedisPlugin connected", extra={"extra": {"url": self.url}})
            return True
        except Exception as e:
            log.warning(f"RedisPlugin: cannot connect ({e}), fallback to in-memory")
            return False

    def on_startup(self, app):
        self._connect()

    def check_rate_limit(self, key: str, rate: int,
                         window: float) -> tuple[bool, int] | None:
        if not self._connect():
            return None
        try:
            r = self._r
            rk = f"{self.prefix}rl:{key}"
            pipe = r.pipeline()
            pipe.incr(rk)
            pipe.ttl(rk)
            count, ttl = pipe.execute()
            if count == 1:
                r.expire(rk, int(window) + 1)
            if count > rate:
                return False, max(int(ttl), 1) if ttl > 0 else int(window)
            return True, 0
        except Exception as e:
            log.warning(f"RedisPlugin.check_rate_limit: {e}")
            return None

    def cache_get(self, key: str) -> t.Any:
        if not self._connect():
            return _MISS
        try:
            raw = self._r.get(f"{self.prefix}cache:{key}")
            if raw is None:
                return _MISS
            return json.loads(raw)
        except Exception:
            return _MISS

    def cache_set(self, key: str, value: t.Any, ttl: int):
        if not self._connect():
            return
        try:
            self._r.setex(f"{self.prefix}cache:{key}", int(ttl),
                          json.dumps(value, default=str))
        except Exception as e:
            log.warning(f"RedisPlugin.cache_set: {e}")

    def close(self):
        if self._r:
            try:
                self._r.close()
            except Exception:
                pass


# ============================================================================
# SECTION 21 — MCP CLIENT (proxy existing MCP servers)
# ============================================================================

class MCPClient:
    """
    Kisi bhi existing MCP server ko SHABD ke through proxy karo.

    stdio example:
        MCPClient("github", transport="stdio",
                  command="npx", args=["-y", "@modelcontextprotocol/server-github"],
                  env={"GITHUB_TOKEN": "..."})

    http example:
        MCPClient("remote", transport="http",
                  url="https://mcp.example.com", auth_token="sk-...")
    """

    def __init__(self, name: str, *,
                 transport: str = "stdio",
                 command: str | None = None,
                 args: list[str] | None = None,
                 env: dict | None = None,
                 url: str = "",
                 auth_token: str = "",
                 prefix: bool = True,
                 timeout: float = 15.0):
        self.name = name
        self.transport = transport.lower()
        self.command = command
        self.args = args or []
        self.env_extra = env or {}
        self.url = (url or "").rstrip("/")
        self.auth_token = auth_token
        self.prefix = prefix
        self.timeout = timeout
        self._proc: _subprocess.Popen | None = None
        self._tools: list[dict] = []
        self._connected: bool = False
        self._req_id = 0
        self._lock = threading.Lock()
        self._session_id: str = ""

    def _next_id(self) -> int:
        with self._lock:
            self._req_id += 1
            return self._req_id

    def _stdio_send(self, msg: dict):
        if not self._proc:
            raise ConjureError("MCP stdio client not started")
        line = json.dumps(msg, default=str) + "\n"
        self._proc.stdin.write(line.encode())
        self._proc.stdin.flush()

    def _stdio_recv(self) -> dict:
        if not self._proc:
            raise ConjureError("MCP stdio client not started")
        line = self._proc.stdout.readline()
        if not line:
            raise ConjureError(f"MCP client '{self.name}' closed connection")
        return json.loads(line)

    def _http_rpc(self, method: str, params: dict | None = None,
                  rpc_id: int | None = None) -> dict:
        msg: dict = {"jsonrpc": "2.0", "method": method}
        if rpc_id is not None:
            msg["id"] = rpc_id
        if params:
            msg["params"] = params
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.auth_token:
            tok = self.auth_token
            headers["Authorization"] = tok if tok.startswith("Bearer ") else f"Bearer {tok}"
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        req = urllib.request.Request(
            self.url, data=json.dumps(msg, default=str).encode("utf-8"),
            headers=headers, method="POST")
        if rpc_id is None:
            try:
                urllib.request.urlopen(req, timeout=5)
            except Exception:
                pass
            return {}
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read()
                ct = r.headers.get("Content-Type", "")
                sid = r.headers.get("Mcp-Session-Id") or r.headers.get("mcp-session-id") or ""
                if sid:
                    self._session_id = sid
                return self._parse_http_response(raw, ct)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            raise ConjureError(f"MCP HTTP {e.code}: {body[:300]}")
        except OSError as e:
            raise ConjureError(f"MCP connection failed: {e}")

    @staticmethod
    def _parse_http_response(raw: bytes, content_type: str) -> dict:
        if not raw:
            return {}
        text = raw.decode("utf-8", errors="replace")
        if "event-stream" in content_type or text.lstrip().startswith("data:"):
            for line in text.splitlines():
                line = line.strip()
                if line.startswith("data:"):
                    data = line[5:].strip()
                    if data and data != "[DONE]":
                        try:
                            return json.loads(data)
                        except json.JSONDecodeError:
                            continue
            return {}
        return json.loads(text)

    def _rpc(self, method: str, params: dict | None = None,
             rpc_id: int | None = None) -> dict:
        if self.transport == "stdio":
            msg: dict = {"jsonrpc": "2.0", "method": method}
            if rpc_id is not None:
                msg["id"] = rpc_id
            if params:
                msg["params"] = params
            self._stdio_send(msg)
            return {} if rpc_id is None else self._stdio_recv()
        return self._http_rpc(method, params, rpc_id)

    def connect(self) -> list[dict]:
        if self.transport == "stdio":
            env = os.environ.copy()
            env.update(self.env_extra)
            cmd = [self.command] + self.args if self.command else self.args
            self._proc = _subprocess.Popen(
                cmd, stdin=_subprocess.PIPE, stdout=_subprocess.PIPE,
                stderr=_subprocess.DEVNULL, env=env)
        self._rpc("initialize", {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "shabd-mcp-client", "version": __version__},
        }, rpc_id=self._next_id())
        self._rpc("notifications/initialized")
        resp = self._rpc("tools/list", rpc_id=self._next_id())
        self._tools = resp.get("result", {}).get("tools", [])
        self._connected = True
        log.info(f"MCPClient '{self.name}': {len(self._tools)} tools",
                 extra={"extra": {"mcp_client": self.name}})
        return self._tools

    def call_tool(self, tool_name: str, arguments: dict) -> t.Any:
        resp = self._rpc("tools/call", {"name": tool_name, "arguments": arguments},
                         rpc_id=self._next_id())
        result = resp.get("result", {})
        if result.get("isError"):
            msg = ""
            for c in result.get("content", []):
                if c.get("type") == "text":
                    msg = c["text"]
                    break
            raise ConjureError(f"MCP tool '{tool_name}' error: {msg}")
        for c in result.get("content", []):
            if c.get("type") == "text":
                try:
                    return json.loads(c["text"])
                except Exception:
                    return c["text"]
        return result

    def register_on(self, app: Conjure):
        # If connect() has already run (even if it found zero tools),
        # don't retry it here — the caller has decided what to do.
        if not self._tools and not self._connected:
            self.connect()
        tools = self._tools
        client_ref = self
        for tool in tools:
            remote_name = tool["name"]
            spell_name = f"{self.name}__{remote_name}" if self.prefix else remote_name
            description = (tool.get("description") or remote_name).strip()
            schema = tool.get("inputSchema") or {"type": "object", "properties": {}}

            def _make_proxy(cref: MCPClient, rname: str):
                async def _proxy(**kwargs: t.Any) -> t.Any:
                    loop = asyncio.get_running_loop()
                    return await loop.run_in_executor(
                        None, lambda: cref.call_tool(rname, kwargs))
                _proxy.__name__ = rname
                _proxy.__doc__ = description
                return _proxy

            proxy_fn = _make_proxy(client_ref, remote_name)
            spell_obj = Spell(
                name=spell_name, func=proxy_fn, description=description,
                schema=schema, returns_schema={}, is_async=True,
                is_streaming=False, wants_context=False, scopes=[],
                rate_limit=None, rate_window=60.0, cache_ttl=None,
                timeout=self.timeout, retries=1, tags=["mcp-proxy"], group="",
            )
            if spell_name in app._spells:
                log.warning(f"MCPClient: spell '{spell_name}' already exists, skipping")
                continue
            app._spells[spell_name] = spell_obj
            log.info(f"Registered MCP proxy: {spell_name}")

    def close(self):
        if self._proc:
            try:
                self._proc.terminate()
            except Exception:
                pass


# ============================================================================
# SECTION 22 — CONFIG LOADER (UPDATED: yaml_spells support)
# ============================================================================

class ConfigLoader:
    """
    YAML ya JSON config se SHABD app banao.
    Ab yaml_spells bhi support karta hai — Python likhe bina tools define karo.
    """

    @staticmethod
    def _substitute_env(text: str) -> str:
        def _sub(m: _re.Match) -> str:
            inner = m.group(1)
            if ":-" in inner:
                key, default = inner.split(":-", 1)
                return os.environ.get(key.strip(), default.strip())
            return os.environ.get(inner.strip(), "")
        return _re.sub(r"\$\{([^}]+)\}", _sub, text)

    @classmethod
    def load(cls, path: str) -> dict:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
        raw = cls._substitute_env(raw)
        ext = os.path.splitext(path)[1].lower()
        if ext in (".yaml", ".yml"):
            try:
                import yaml  # type: ignore
                return yaml.safe_load(raw) or {}
            except ImportError:
                raise ImportError(
                    "pip install pyyaml  —  YAML config ke liye PyYAML chahiye.")
        return json.loads(raw)

    @classmethod
    def build_conjure(cls, cfg: dict) -> Conjure:
        srv = cfg.get("server", {})
        plugins_list: list[ConjurePlugin] = []
        redis_cfg = cfg.get("plugins", {}).get("redis", {})
        if redis_cfg.get("enabled"):
            plugins_list.append(
                RedisPlugin(redis_cfg.get("url", "redis://localhost:6379"),
                            redis_cfg.get("key_prefix", "shabd:")))

        app = Conjure(
            name=srv.get("name", "shabd"),
            secret=srv.get("secret") or None,
            require_auth=srv.get("require_auth", True),
            max_body_bytes=int(srv.get("max_body_bytes", 1024 * 1024)),
            default_timeout=float(srv.get("timeout", 30.0)),
            default_rate=int(srv.get("rate_limit", 100)),
            cors_origin=srv.get("cors_origin", "*"),
            plugins=plugins_list,
        )

        # MCP clients
        for mc_cfg in cfg.get("mcp_clients", []):
            if not mc_cfg.get("enabled", True):
                continue
            client = MCPClient(
                name=mc_cfg["name"],
                transport=mc_cfg.get("transport", "stdio"),
                command=mc_cfg.get("command"),
                args=mc_cfg.get("args", []),
                env=mc_cfg.get("env", {}),
                url=mc_cfg.get("url", ""),
                auth_token=mc_cfg.get("auth_token", ""),
                prefix=mc_cfg.get("prefix", True),
                timeout=float(mc_cfg.get("timeout", 15.0)),
            )
            try:
                client.register_on(app)
            except Exception as exc:
                log.warning(f"MCPClient '{mc_cfg['name']}' connect failed: {exc}")

        # NEW: YAML-defined spells — Python likhne ki zaroorat nahi!
        for ys in cfg.get("yaml_spells", []):
            cls._register_yaml_spell(app, ys)

        # Chains
        for chain_cfg in cfg.get("chains", []):
            try:
                app.chain(
                    chain_cfg["pipeline"],
                    name=chain_cfg.get("name"),
                    description=chain_cfg.get("description"),
                    scopes=chain_cfg.get("scopes", []),
                    tags=chain_cfg.get("tags", []),
                )
            except Exception as exc:
                log.warning(f"Chain config failed: {exc}")

        return app

    @classmethod
    def _register_yaml_spell(cls, app: Conjure, ys: dict):
        """
        YAML se ek spell register karo — koi Python likhne ki zaroorat nahi.

        yaml_spells:
          - name: get_weather
            description: "City ka weather lo"
            url: "https://wttr.in/{city}?format=j1"
            method: GET
            params:
              city:
                type: string
                required: true
                description: "City ka naam"
            headers:
              User-Agent: "SHABD/2.0"
            transform: ".current_condition[0]"  # jq-style (future)

          - name: create_jira_ticket
            description: "Jira ticket banao"
            url: "https://your-domain.atlassian.net/rest/api/3/issue"
            method: POST
            auth_env: "JIRA_TOKEN"  # Bearer token env var
            body_template: |
              {{"fields": {{"summary": "{{title}}", "project": {{"key": "{{project}}"}}}}}}
            params:
              title: {{type: string, required: true}}
              project: {{type: string, required: true, default: "PROJ"}}
        """
        name = ys.get("name")
        if not name:
            log.warning("yaml_spell: name missing, skip")
            return
        url_template = ys.get("url", "")
        method = ys.get("method", "GET").upper()
        params_cfg = ys.get("params", {})
        headers_extra = ys.get("headers", {})
        auth_env = ys.get("auth_env", "")
        body_template = ys.get("body_template", "")
        description = ys.get("description", f"YAML spell: {name}")
        tags = ys.get("tags", ["yaml"])

        # Build JSON schema from params config
        properties = {}
        required = []
        for pname, pconf in (params_cfg or {}).items():
            if isinstance(pconf, str):
                pconf = {"type": pconf}
            elif pconf is None:
                pconf = {}
            properties[pname] = {"type": pconf.get("type", "string")}
            if pconf.get("description"):
                properties[pname]["description"] = pconf["description"]
            if pconf.get("default") is not None:
                properties[pname]["default"] = pconf["default"]
            if pconf.get("required", False):
                required.append(pname)
        schema = {"type": "object", "properties": properties,
                  "required": required, "additionalProperties": False}

        # Dynamic async function generate karo
        async def _yaml_spell_fn(**kwargs: t.Any) -> t.Any:
            import urllib.request as _ur
            # URL mein parameters substitute karo
            final_url = url_template
            remaining_params = {}
            for k, v in kwargs.items():
                placeholder = "{" + k + "}"
                if placeholder in final_url:
                    final_url = final_url.replace(placeholder, urllib.parse.quote(str(v)))
                else:
                    remaining_params[k] = v

            # Query params add karo (GET ke liye)
            if method == "GET" and remaining_params:
                qs = urllib.parse.urlencode(remaining_params)
                final_url = final_url + ("&" if "?" in final_url else "?") + qs

            req_headers = {"Content-Type": "application/json", **headers_extra}
            if auth_env:
                token = os.environ.get(auth_env, "")
                if token:
                    req_headers["Authorization"] = (
                        token if token.startswith("Bearer ") else f"Bearer {token}")

            body_bytes = b""
            if method in ("POST", "PUT", "PATCH"):
                if body_template:
                    # Simple template substitution
                    body_str = body_template
                    for k, v in kwargs.items():
                        body_str = body_str.replace("{{" + k + "}}", str(v))
                    body_bytes = body_str.encode()
                elif remaining_params:
                    body_bytes = json.dumps(remaining_params).encode()

            req = _ur.Request(final_url, data=body_bytes or None,
                              headers=req_headers, method=method)
            loop = asyncio.get_running_loop()
            try:
                def _do_request():
                    with _ur.urlopen(req, timeout=30) as r:
                        raw = r.read()
                        ct = r.headers.get("Content-Type", "")
                        if "json" in ct:
                            return json.loads(raw)
                        return raw.decode("utf-8", "replace")
                return await loop.run_in_executor(None, _do_request)
            except Exception as e:
                raise ConjureError(f"yaml_spell '{name}' HTTP error: {e}")

        _yaml_spell_fn.__name__ = name
        _yaml_spell_fn.__doc__ = description

        spell_obj = Spell(
            name=name, func=_yaml_spell_fn, description=description,
            schema=schema, returns_schema={}, is_async=True,
            is_streaming=False, wants_context=False, scopes=[],
            rate_limit=None, rate_window=60.0, cache_ttl=None,
            timeout=30.0, retries=0, tags=tags + ["yaml"], group="",
        )
        if name in app._spells:
            log.warning(f"yaml_spell '{name}' already registered, skipping")
            return
        app._spells[name] = spell_obj
        log.info(f"Registered YAML spell: {name}")


# ============================================================================
# SECTION 22b — ENTERPRISE EXTENSIONS (v2.2)
# ============================================================================
# Production-grade pieces aimed at banking / trading / regulated systems:
#
#   IdempotencyStore       — replay-safe POSTs via Idempotency-Key header
#   GrimoireJSONL          — append-only on-disk persistence for the audit chain
#   PromExporter           — Prometheus exposition format for /metrics
#   TraceContextCodec      — W3C traceparent / tracestate propagation
#   AuditWebhook           — push every Grimoire page to an external SIEM
#   ConcurrencyLimiter     — per-spell asyncio semaphore (max in-flight)
#   GracefulShutdown       — SIGTERM-aware drain controller
#
# All optional, all stdlib-only.

class IdempotencyStore:
    """
    In-memory `Idempotency-Key` cache.

    Banking / trading APIs require that a client can safely retry a write
    after a network blip without double-charging. The first call with a
    given key is executed; every subsequent call with the same key within
    `ttl` seconds returns the *same* recorded response, including errors.
    """

    def __init__(self, ttl: int = 86400, max_entries: int = 100_000):
        self._ttl = ttl
        self._max = max_entries
        self._store: dict[str, tuple[float, str, t.Any]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def fingerprint(spell: str, args: dict) -> str:
        canonical = json.dumps(args, sort_keys=True, default=str,
                               separators=(",", ":"))
        return hashlib.sha256(f"{spell}|{canonical}".encode()).hexdigest()

    def lookup(self, key: str, spell: str, args: dict
               ) -> tuple[bool, t.Any] | None:
        """
        Returns (ok, value) for a replay, or None for a fresh request.

        Raises if the same key is reused with a different request body —
        that's a client bug worth surfacing loudly.
        """
        fp = self.fingerprint(spell, args)
        with self._lock:
            entry = self._store.get(key)
            if not entry:
                return None
            ts, stored_fp, payload = entry
            if time.time() - ts > self._ttl:
                self._store.pop(key, None)
                return None
            if not hmac.compare_digest(stored_fp, fp):
                raise ConjureError(
                    "idempotency key reused with a different request body",
                    code="idempotency_conflict",
                    hint=("Send a fresh Idempotency-Key for a different "
                          "request, or re-send the original body."),
                )
            return payload  # (ok: bool, value: any)

    def store(self, key: str, spell: str, args: dict,
              ok: bool, value: t.Any) -> None:
        fp = self.fingerprint(spell, args)
        with self._lock:
            if len(self._store) >= self._max:
                # Evict oldest entries
                now = time.time()
                self._store = {k: v for k, v in self._store.items()
                               if now - v[0] < self._ttl}
                if len(self._store) >= self._max:
                    # Force-evict ~10%
                    evict_n = max(1, self._max // 10)
                    for k in list(self._store)[:evict_n]:
                        self._store.pop(k, None)
            self._store[key] = (time.time(), fp, (ok, value))


class GrimoireJSONL:
    """
    Append-only on-disk persistence for the Grimoire chain.

    Why JSONL and not SQLite?
      * The Grimoire is *append-only by design* — JSONL files match that.
      * Single-machine recovery is `cat audit.jsonl`.
      * Trivial to mirror, ship to S3 / GCS / object lock buckets.
      * No schema migrations.

    The file is opened in line-buffered text mode and fsync'd on every
    append, so a crash loses at most the in-flight call (which the caller
    also lost). For higher throughput, drop the fsync via `fsync=False`.
    """

    def __init__(self, path: str, *, fsync: bool = True):
        self.path = path
        self._fsync = fsync
        self._lock = threading.Lock()
        self._fh: t.IO[str] | None = None
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._fh = open(path, "a", encoding="utf-8")

    def append(self, page: dict) -> None:
        line = json.dumps(page, separators=(",", ":"), default=str)
        with self._lock:
            assert self._fh is not None
            self._fh.write(line + "\n")
            self._fh.flush()
            if self._fsync:
                try:
                    os.fsync(self._fh.fileno())
                except OSError:
                    pass

    def load(self) -> list[dict]:
        if not os.path.exists(self.path):
            return []
        pages = []
        with open(self.path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    pages.append(json.loads(line))
                except json.JSONDecodeError:
                    log.warning("skipping corrupt audit line")
        return pages

    def close(self) -> None:
        with self._lock:
            if self._fh:
                try:
                    self._fh.close()
                except Exception:
                    pass
                self._fh = None


class PromExporter:
    """Render `Metrics` in Prometheus 0.0.4 exposition format."""

    @staticmethod
    def _escape_label(s: str) -> str:
        return s.replace("\\", "\\\\").replace("\"", "\\\"").replace("\n", "\\n")

    @classmethod
    def render(cls, m: Metrics, app_name: str) -> str:
        snap = m.snapshot()
        lines: list[str] = []
        lines.append("# HELP shabd_info SHABD server info")
        lines.append("# TYPE shabd_info gauge")
        lines.append(f'shabd_info{{app="{cls._escape_label(app_name)}",version="{__version__}"}} 1')

        lines.append("# HELP shabd_calls_total Total spell invocations")
        lines.append("# TYPE shabd_calls_total counter")
        for key, n in snap.get("counters", {}).items():
            parts = key.split(".")
            if len(parts) >= 3 and parts[0] == "spell":
                spell, status = parts[1], parts[-1]
                if status in ("ok", "error", "cache_hit"):
                    lines.append(
                        f'shabd_calls_total{{spell="{cls._escape_label(spell)}",'
                        f'status="{status}"}} {n}'
                    )

        lines.append("# HELP shabd_call_duration_ms Spell duration (ms) quantiles")
        lines.append("# TYPE shabd_call_duration_ms summary")
        for key, stats in snap.get("timings", {}).items():
            if not key.startswith("spell."):
                continue
            spell = key.split(".", 1)[1]
            for q, label in (("p50", "0.5"), ("p95", "0.95"), ("p99", "0.99")):
                v = stats.get(q)
                if v is not None:
                    lines.append(
                        f'shabd_call_duration_ms{{spell="{cls._escape_label(spell)}",'
                        f'quantile="{label}"}} {v:.4f}'
                    )
            lines.append(
                f'shabd_call_duration_ms_count{{spell="{cls._escape_label(spell)}"}} '
                f'{stats.get("count", 0)}'
            )
        return "\n".join(lines) + "\n"


class TraceContextCodec:
    """
    W3C TraceContext encoder / decoder.

    https://www.w3.org/TR/trace-context/

    Format: `version-traceid-parentid-flags`, e.g.
        00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01
    """
    _RE = _re.compile(
        r"^([0-9a-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$"
    )

    @classmethod
    def decode(cls, header: str | None
               ) -> tuple[str | None, str | None, int]:
        """Returns (trace_id, parent_span_id, flags)."""
        if not header:
            return None, None, 0
        m = cls._RE.match(header.strip())
        if not m:
            return None, None, 0
        _ver, trace_id, parent_span, flags = m.groups()
        return trace_id, parent_span, int(flags, 16)

    @staticmethod
    def encode(trace_id: str, span_id: str, *, sampled: bool = True) -> str:
        flags = "01" if sampled else "00"
        return f"00-{trace_id}-{span_id}-{flags}"


class AuditWebhook:
    """
    Fire-and-forget HTTP POST of every Grimoire page to an external URL.

    Useful for streaming the audit chain into a SIEM (Splunk, Datadog,
    Elastic, OpenSearch). The payload is HMAC-signed with `secret` so the
    receiver can verify it really came from this SHABD instance.

    Failures are logged but never block the call path.
    """

    def __init__(self, url: str, secret: bytes | None = None,
                 *, timeout: float = 5.0):
        self.url = url
        self.secret = secret
        self.timeout = timeout
        self._queue: asyncio.Queue[dict] = None  # type: ignore
        self._task: asyncio.Task | None = None

    def _start(self, loop: asyncio.AbstractEventLoop) -> None:
        if self._task is not None:
            return
        self._queue = asyncio.Queue(maxsize=10_000)
        self._task = loop.create_task(self._worker())

    def submit(self, page: dict, loop: asyncio.AbstractEventLoop) -> None:
        self._start(loop)
        try:
            self._queue.put_nowait(page)
        except asyncio.QueueFull:
            log.warning("audit webhook queue full; dropping page")

    async def _worker(self) -> None:
        while True:
            page = await self._queue.get()
            try:
                await self._post(page)
            except Exception:
                log.exception("audit webhook delivery failed")

    async def _post(self, page: dict) -> None:
        body = json.dumps(page, separators=(",", ":"), default=str).encode()
        headers = {"content-type": "application/json",
                   "user-agent": f"shabd/{__version__}"}
        if self.secret:
            sig = hmac.new(self.secret, body, hashlib.sha256).hexdigest()
            headers["x-shabd-signature"] = f"sha256={sig}"
        try:
            from urllib.parse import urlparse
            p = urlparse(self.url)
            host = p.hostname or "localhost"
            port = p.port or (443 if p.scheme == "https" else 80)
            ssl_ctx = None
            if p.scheme == "https":
                import ssl as _ssl
                ssl_ctx = _ssl.create_default_context()
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port, ssl=ssl_ctx),
                timeout=self.timeout,
            )
            path = p.path or "/"
            req = (f"POST {path} HTTP/1.1\r\nHost: {host}\r\n" +
                   "".join(f"{k}: {v}\r\n" for k, v in headers.items()) +
                   f"content-length: {len(body)}\r\nConnection: close\r\n\r\n")
            writer.write(req.encode() + body)
            await writer.drain()
            await asyncio.wait_for(reader.read(1024), timeout=self.timeout)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
        except Exception:
            raise


class _ShutdownState:
    """Tracks in-flight requests so SIGTERM can drain cleanly."""

    def __init__(self):
        self._in_flight = 0
        self._draining = False
        self._started_at = time.time()
        self._lock = threading.Lock()
        self._idle = threading.Event()
        self._idle.set()

    def enter(self) -> bool:
        with self._lock:
            if self._draining:
                return False
            self._in_flight += 1
            self._idle.clear()
            return True

    def exit(self) -> None:
        with self._lock:
            self._in_flight = max(0, self._in_flight - 1)
            if self._in_flight == 0:
                self._idle.set()

    def begin_drain(self) -> None:
        with self._lock:
            self._draining = True

    @property
    def draining(self) -> bool:
        return self._draining

    @property
    def in_flight(self) -> int:
        return self._in_flight

    @property
    def uptime_s(self) -> float:
        return time.time() - self._started_at

    def wait_idle(self, timeout: float) -> bool:
        return self._idle.wait(timeout=timeout)


# ============================================================================
# SECTION 23 — DEMO
# ============================================================================

def _demo():
    """python shabd.py se run karo — demo server."""
    app = SHABD("shabd-demo", secret="demo-secret-change-me-32-bytes!",
                require_auth=False)

    # Basic spell
    @app.spell
    def add(a: int, b: int) -> int:
        """Do numbers add karo."""
        return a + b

    # Async + cached
    @app.spell(cache_ttl=60, rate_limit=10)
    async def weather(city: str) -> dict:
        """City ka fake weather."""
        await asyncio.sleep(0.01)
        return {"city": city, "temp_c": 22, "conditions": "sunny"}

    # Streaming
    @app.spell
    def countdown(n: int = 5):
        """Countdown stream karo."""
        for i in range(n, 0, -1):
            yield {"i": i}
            time.sleep(0.1)
        yield {"done": True}

    # Image return type
    @app.spell
    def make_image(text: str = "Hello SHABD!") -> SpellImage:
        """Simple PNG image banao."""
        # 1x1 red pixel PNG (demo ke liye)
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8/5+hHgAHggJ/PchI6QAAAABJRU5ErkJggg==")
        return SpellImage(data=png, mime_type="image/png", alt_text=text)

    # Context injection
    @app.spell
    def whoami(ctx: Context) -> dict:
        """Calling user ki info."""
        return {"trace_id": ctx.trace_id, "subject": ctx.subject,
                "transport": ctx.transport}

    # MCP Resource — NEW
    @app.resource("/docs/{slug}", mime_type="text/markdown")
    def get_doc(slug: str) -> str:
        """Documentation page return karo."""
        return f"# {slug}\n\nYe `{slug}` ke baare mein documentation hai.\n"

    # MCP Prompt — NEW
    @app.prompt("code_review")
    def review_prompt(language: str = "python",
                      focus: str = "bugs") -> str:
        """Code review ka prompt template."""
        return f"Please review this {language} code, focusing on {focus}."

    # Multi-project group — NEW
    finance = app.group("finance")

    @finance.spell
    def calculate_gst(amount: float, rate: float = 18.0) -> dict:
        """GST calculate karo."""
        gst = amount * rate / 100
        return {"amount": amount, "gst": gst, "total": amount + gst}

    # Spell chain — NEW
    @app.spell
    def parse_number(text: str) -> dict:
        """String se number parse karo."""
        return {"value": float(text.strip())}

    @app.spell
    def double_value(value: float) -> float:
        """Number double karo."""
        return value * 2

    app.chain("parse_number | double_value", name="parse_and_double",
              description="String parse karo aur double karo")

    if "--stdio" in sys.argv:
        app.stdio()
    elif "--mcp" in sys.argv:
        app.mcp_stdio()
    elif "--manifest" in sys.argv:
        print(json.dumps(app.manifest(), indent=2))
    elif "--cpm" in sys.argv:
        print(app.cpm_config())
    elif "--token" in sys.argv:
        print(app.issue_token("demo-user", scopes=["*"], ttl=3600))
    else:
        print(f"\n🔮 SHABD v{__version__} Demo")
        print(f"   Spells    : {len(app._spells)}")
        print(f"   Resources : {len(app._resources)}")
        print(f"   Prompts   : {len(app._prompts)}")
        print("   Dashboard : http://127.0.0.1:8765/dashboard")
        print("   Manifest  : http://127.0.0.1:8765/manifest")
        print("   CPM Config: http://127.0.0.1:8765/cpm-config")
        print("   Resources : http://127.0.0.1:8765/resources")
        print("   Prompts   : http://127.0.0.1:8765/prompts")
        print()
        app.serve(host="127.0.0.1", port=8765)


if __name__ == "__main__":
    _demo()
