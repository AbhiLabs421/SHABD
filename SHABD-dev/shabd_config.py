"""
shabd_config.py — the single production control surface for SHABD.

One `config.yaml` (or `.json`) decides the whole production posture: which
identity provider, which cache, which persistence, where the signing secret
comes from, and TLS. It builds the concrete providers so business code depends
only on interfaces (see docs/PRODUCTION-READINESS.md §1, §3).

Zero-dependency honesty
-----------------------
YAML is parsed with PyYAML when it's installed; otherwise a small built-in
parser handles the subset this config needs (nested maps, scalar lists, inline
`[]`/`{}`, comments) so a plain `config.yaml` still works with *nothing*
installed. `.json` always works (stdlib). This keeps the air-gap promise.

Example
-------
    from shabd_config import ProductionConfig
    pc = ProductionConfig.load("config.yaml")
    secret   = pc.secret()
    identity = pc.build_identity(audit=my_grimoire_audit)
    cache    = pc.build_cache()          # a ConjurePlugin, or None for builtin
"""
from __future__ import annotations

import json
import os
import typing as t

__all__ = ["ConfigError", "load_config", "resolve_secret", "ProductionConfig"]


class ConfigError(Exception):
    pass


# ===========================================================================
# Loading — YAML (PyYAML if present, else a small subset parser) or JSON
# ===========================================================================

def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    if path.endswith(".json"):
        return json.loads(text or "{}")
    try:
        import yaml  # type: ignore
        return yaml.safe_load(text) or {}
    except ImportError:
        return _mini_yaml(text)


def _scalar(s: str):
    s = s.strip()
    if s == "" or s in ("~", "null", "Null", "NULL"):
        return None
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    low = s.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1].strip()
        return [_scalar(x) for x in _split_flow(inner)] if inner else []
    if s.startswith("{") and s.endswith("}"):
        inner = s[1:-1].strip()
        if not inner:
            return {}
        out = {}
        for part in _split_flow(inner):
            k, _, v = part.partition(":")
            out[k.strip()] = _scalar(v)
        return out
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return s


def _split_flow(inner: str) -> list[str]:
    """Split `a, b, c` respecting nothing fancy (flat scalars only)."""
    return [p.strip() for p in inner.split(",") if p.strip() != ""]


def _strip_comment(line: str) -> str:
    out, in_q, q = [], False, ""
    for ch in line:
        if in_q:
            out.append(ch)
            if ch == q:
                in_q = False
        elif ch in "\"'":
            in_q = True
            q = ch
            out.append(ch)
        elif ch == "#":
            break
        else:
            out.append(ch)
    return "".join(out).rstrip()


def _mini_yaml(text: str) -> dict:
    """A deliberately small block-YAML parser: nested maps + scalar block
    lists + inline `[]`/`{}` + comments. Enough for SHABD's config; not a
    general YAML implementation.

    A `key:` with an empty value is *deferred* — its container becomes a list
    if the first child is a `- item`, otherwise a map.
    """
    root: dict = {}
    # stack of frames [child_indent, container]; container is a dict or list.
    stack: list[list] = [[-1, root]]
    pending: tuple[int, t.Any, str] | None = None  # (indent, container, key)

    for raw in text.splitlines():
        line = _strip_comment(raw)
        if line.strip() == "":
            continue
        indent = len(line) - len(line.lstrip(" "))
        content = line.strip()

        descended = False
        if pending is not None:
            p_indent, p_container, p_key = pending
            if indent > p_indent:
                new_container: t.Any = [] if content.startswith("- ") else {}
                p_container[p_key] = new_container
                stack.append([indent, new_container])
                pending = None
                descended = True
            else:
                p_container[p_key] = {}      # empty section, no children
                pending = None
        if not descended:
            while len(stack) > 1 and indent < stack[-1][0]:
                stack.pop()
        container = stack[-1][1]

        if content.startswith("- "):
            if not isinstance(container, list):
                raise ConfigError(f"list item not in a list: {content!r}")
            container.append(_scalar(content[2:]))
        else:
            key, sep, val = content.partition(":")
            if not sep:
                raise ConfigError(f"bad config line: {content!r}")
            if not isinstance(container, dict):
                raise ConfigError(f"key in a list context: {content!r}")
            key, val = key.strip(), val.strip()
            if val == "":
                pending = (indent, container, key)
            else:
                container[key] = _scalar(val)

    if pending is not None:
        pending[1][pending[2]] = {}
    return root


# ===========================================================================
# Secret resolution — never inline in plaintext if avoidable
# ===========================================================================

def resolve_secret(source: dict | None, *, default_env: str = "SHABD_SECRET") -> bytes:
    """Resolve a `*_source` block to raw bytes.

    { provider: env,  key: SHABD_SECRET }
    { provider: file, path: /etc/shabd/secret.key }
    { provider: inline, value: "..." }        # dev only
    """
    source = source or {"provider": "env", "key": default_env}
    prov = source.get("provider", "env")
    if prov == "env":
        raw = os.environ.get(source.get("key", default_env), "")
        if not raw:
            raise ConfigError(
                f"secret env var {source.get('key', default_env)} is not set")
        return _decode_secret(raw)
    if prov == "file":
        with open(source["path"], "rb") as fh:
            return _decode_secret(fh.read().decode().strip())
    if prov == "inline":
        return _decode_secret(str(source.get("value", "")))
    raise ConfigError(f"unknown secret provider: {prov}")


def _decode_secret(raw: str) -> bytes:
    raw = raw.strip()
    # accept hex (even length, hex chars) or raw utf-8
    if len(raw) >= 32 and len(raw) % 2 == 0 and all(
            c in "0123456789abcdefABCDEF" for c in raw):
        try:
            return bytes.fromhex(raw)
        except ValueError:
            pass
    return raw.encode()


# ===========================================================================
# ProductionConfig — builds the selected providers
# ===========================================================================

class ProductionConfig:
    def __init__(self, cfg: dict):
        self.cfg = cfg or {}

    @classmethod
    def load(cls, path: str) -> ProductionConfig:
        return cls(load_config(path))

    # ---- secret -------------------------------------------------------
    def secret(self) -> bytes:
        shabd = self.cfg.get("shabd", {})
        return resolve_secret(shabd.get("secret_source"))

    # ---- identity -----------------------------------------------------
    def build_identity(self, *,
                       app: t.Any = None,
                       audit: t.Callable[[str, dict], None] | None = None,
                       realm_path: str | None = None):
        """Build the configured identity provider. Pass `app` to auto-wire the
        Grimoire audit bridge so auth events are tamper-evident (unless an
        explicit `audit` is given)."""
        from shabd_praman import grimoire_audit_bridge, identity_from_config
        if audit is None and app is not None:
            audit = grimoire_audit_bridge(app)
        return identity_from_config(
            self.cfg.get("identity", {}), secret=self.secret(),
            audit=audit, realm_path=realm_path)

    # ---- cache --------------------------------------------------------
    def build_cache(self):
        """Returns a ConjurePlugin (Smriti/Redis) or None for the in-process
        builtin cache."""
        from shabd_smriti import cache_from_config
        return cache_from_config(self.cfg.get("cache", {}))

    # ---- persistence --------------------------------------------------
    def persistence(self) -> dict:
        return self.cfg.get("persistence", {"provider": "jsonl"})

    # ---- server / TLS -------------------------------------------------
    def server(self) -> dict:
        return self.cfg.get("server", {})

    def tls(self) -> tuple[str | None, str | None]:
        tls = self.server().get("tls", {})
        return tls.get("cert"), tls.get("key")

    def summary(self) -> dict:
        """A redacted, human-readable snapshot of the active posture."""
        return {
            "identity": self.cfg.get("identity", {}).get("provider", "builtin"),
            "cache": self.cfg.get("cache", {}).get("provider", "builtin"),
            "persistence": self.persistence().get("provider", "jsonl"),
            "tls": bool(self.tls()[0]),
        }

    # ---- standalone server construction (operational glue) ------------
    def identity_server(self, *, bind: str = "127.0.0.1", port: int = 8899,
                        realm_path: str | None = None,
                        audit: t.Callable[[str, dict], None] | None = None):
        """A PramanServer if identity.provider == builtin, else None (an
        external Keycloak needs no server from us)."""
        prov = self.cfg.get("identity", {}).get("provider", "builtin")
        if prov != "builtin":
            return None
        from shabd_praman import PramanServer
        idp = self.build_identity(audit=audit, realm_path=realm_path)
        return PramanServer(idp.praman, bind=bind, port=port)

    def cache_server(self, *, bind: str = "127.0.0.1", port: int = 6390):
        """A SmritiServer if cache.provider == smriti, else None (in-process
        builtin needs no server; external Redis is the customer's)."""
        if self.cfg.get("cache", {}).get("provider") != "smriti":
            return None
        from shabd_smriti import SmritiServer
        s = self.cfg.get("cache", {}).get("smriti", {})
        return SmritiServer(bind=bind, port=int(s.get("port", port)),
                            password=s.get("password"))


# ===========================================================================
# CLI — launch the built-in servers from a config file (no Docker/images)
# ===========================================================================

def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        prog="shabd_config",
        description="Launch SHABD's built-in production servers from a config.")
    ap.add_argument("--config", required=True, help="config.yaml or .json")
    ap.add_argument("--identity-port", type=int, default=8899)
    ap.add_argument("--cache-port", type=int, default=6390)
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--print-summary", action="store_true",
                    help="print the resolved posture and exit")
    args = ap.parse_args(argv)

    pc = ProductionConfig.load(args.config)
    print("SHABD production posture:", json.dumps(pc.summary()))
    if args.print_summary:
        return 0

    import threading
    started = []
    ident = pc.identity_server(bind=args.bind, port=args.identity_port)
    if ident is not None:
        ident.start_background() if hasattr(ident, "start_background") \
            else threading.Thread(target=ident.serve, daemon=True).start()
        print(f"Praman identity server on {args.bind}:{ident.port}")
        started.append("identity")
    cache = pc.cache_server(bind=args.bind, port=args.cache_port)
    if cache is not None:
        cache.start_background()
        print(f"Smriti cache server on {args.bind}:{cache.port}")
        started.append("cache")

    if not started:
        print("Nothing to start (all providers are external or in-process).")
        return 0
    print(f"Running: {', '.join(started)}. Ctrl-C to stop.")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        print("\nshutting down")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
