"""
shabd_smriti.py — SHABD's built-in cache / coordination server ("Smriti").

Smriti (स्मृति = memory) is the pure-standard-library stand-in for Redis when
you need **shared** state across more than one SHABD process — rate-limits,
sessions, idempotency keys, cache — without installing Redis or running a
container.

The pluggable pattern (config.yaml `cache` block):

    cache.provider: builtin   ->  in-process TTLCache (single instance)
    cache.provider: smriti    ->  this server (own process, shared, no deps)
    cache.provider: redis     ->  the customer's real Redis

Design
------
* Speaks a **RESP subset** (the Redis wire protocol) for the commands SHABD
  actually needs — PING, SET (+EX), GET, DEL, INCR, EXPIRE, TTL, FLUSHALL,
  EXISTS. Because it's RESP, a real `redis-py` client can also talk to Smriti,
  and our own pure-stdlib `SmritiClient` needs zero dependencies.
* Optional append-only file (AOF) so state survives a restart.
* Honest scope: this targets SHABD's needs (cache, counters, locks, TTL) — it
  is not full Redis. That keeps it small and reviewable.

Everything is standard library: socket, socketserver, threading, time, os.
"""
from __future__ import annotations

import os
import socket
import socketserver
import threading
import time
import typing as t

__all__ = [
    "SmritiStore", "SmritiServer", "SmritiClient", "SmritiCache",
    "cache_from_config",
]

_CRLF = b"\r\n"


# ===========================================================================
# RESP encode / decode (the Redis wire format, subset)
# ===========================================================================

def resp_encode(*parts: t.Any) -> bytes:
    """Encode a command as a RESP array of bulk strings."""
    out = [b"*" + str(len(parts)).encode() + _CRLF]
    for p in parts:
        b = p if isinstance(p, bytes) else str(p).encode()
        out.append(b"$" + str(len(b)).encode() + _CRLF + b + _CRLF)
    return b"".join(out)


def _read_line(rfile) -> bytes:
    line = rfile.readline()
    if not line:
        raise ConnectionError("peer closed")
    return line.rstrip(_CRLF)


def resp_read(rfile) -> t.Any:
    """Read one RESP value from a buffered reader. Returns bytes / int /
    list / None / ('ERR', msg)."""
    line = _read_line(rfile)
    if not line:
        return None
    tag, rest = line[:1], line[1:]
    if tag == b"+":
        return rest
    if tag == b"-":
        return ("ERR", rest.decode(errors="replace"))
    if tag == b":":
        return int(rest)
    if tag == b"$":
        n = int(rest)
        if n < 0:
            return None
        data = rfile.read(n)
        rfile.read(2)  # trailing CRLF
        return data
    if tag == b"*":
        n = int(rest)
        if n < 0:
            return None
        return [resp_read(rfile) for _ in range(n)]
    raise ValueError(f"bad RESP tag: {tag!r}")


# ===========================================================================
# The store — key -> (value_bytes, expiry_ts | None)
# ===========================================================================

class SmritiStore:
    def __init__(self, *, aof_path: str | None = None):
        self._d: dict[bytes, tuple[bytes, float | None]] = {}
        self._lock = threading.RLock()
        self.aof_path = aof_path
        self._aof = None
        if aof_path:
            self._replay_aof()
            self._aof = open(aof_path, "ab", buffering=0)

    # ---- AOF persistence ----------------------------------------------
    def _replay_aof(self) -> None:
        if not os.path.exists(self.aof_path):
            return
        try:
            with open(self.aof_path, "rb") as fh:
                while True:
                    try:
                        cmd = resp_read(fh)
                    except (ConnectionError, ValueError):
                        break
                    if not cmd:
                        break
                    self._apply(cmd, persist=False)
        except Exception:
            pass

    def _persist(self, *parts) -> None:
        if self._aof:
            try:
                self._aof.write(resp_encode(*parts))
            except Exception:
                pass

    # ---- expiry --------------------------------------------------------
    def _live(self, key: bytes):
        v = self._d.get(key)
        if v is None:
            return None
        val, exp = v
        if exp is not None and exp < time.time():
            self._d.pop(key, None)
            return None
        return v

    def sweep(self) -> int:
        now = time.time()
        with self._lock:
            dead = [k for k, (_, e) in self._d.items()
                    if e is not None and e < now]
            for k in dead:
                self._d.pop(k, None)
        return len(dead)

    # ---- command application ------------------------------------------
    def _apply(self, cmd: list, *, persist: bool = True):
        if not cmd:
            return ("ERR", "empty")
        op = cmd[0].upper() if isinstance(cmd[0], bytes) else b""
        args = cmd[1:]
        with self._lock:
            if op == b"PING":
                return b"PONG"
            if op == b"SET":
                key, val = args[0], args[1]
                exp = None
                if len(args) >= 4 and args[2].upper() == b"EX":
                    exp = time.time() + int(args[3])
                self._d[key] = (val, exp)
                if persist:
                    self._persist(*cmd)
                return b"OK"
            if op == b"GET":
                v = self._live(args[0])
                return v[0] if v else None
            if op == b"EXISTS":
                return 1 if self._live(args[0]) else 0
            if op == b"DEL":
                n = 0
                for k in args:
                    if self._d.pop(k, None) is not None:
                        n += 1
                if persist and n:
                    self._persist(*cmd)
                return n
            if op == b"INCR":
                v = self._live(args[0])
                cur = int(v[0]) if v else 0
                cur += 1
                exp = v[1] if v else None
                self._d[args[0]] = (str(cur).encode(), exp)
                if persist:
                    self._persist(*cmd)
                return cur
            if op == b"EXPIRE":
                v = self._live(args[0])
                if not v:
                    return 0
                self._d[args[0]] = (v[0], time.time() + int(args[1]))
                if persist:
                    self._persist(*cmd)
                return 1
            if op == b"TTL":
                v = self._live(args[0])
                if not v:
                    return -2          # key does not exist
                if v[1] is None:
                    return -1          # no expiry
                return max(0, int(v[1] - time.time()))
            if op == b"FLUSHALL":
                self._d.clear()
                if persist:
                    self._persist(*cmd)
                return b"OK"
            return ("ERR", f"unknown command {op!r}")

    def execute(self, cmd: list):
        return self._apply(cmd, persist=True)


# ===========================================================================
# Server
# ===========================================================================

def _make_request_handler(store: SmritiStore, *,
                          password: bytes | None = None):
    class _Handler(socketserver.StreamRequestHandler):
        def handle(self):
            authed = password is None
            while True:
                try:
                    cmd = resp_read(self.rfile)
                except (ConnectionError, ValueError):
                    return
                if cmd is None:
                    return
                if not isinstance(cmd, list) or not cmd:
                    self._write(("ERR", "protocol"))
                    continue
                op = cmd[0].upper() if isinstance(cmd[0], bytes) else b""
                if op == b"AUTH":
                    if password is not None and len(cmd) >= 2 \
                            and cmd[1] == password:
                        authed = True
                        self._write(b"OK")
                    else:
                        self._write(("ERR", "invalid password"))
                    continue
                if not authed:
                    self._write(("ERR", "NOAUTH authentication required"))
                    continue
                if op == b"QUIT":
                    self._write(b"OK")
                    return
                self._write(store.execute(cmd))

        def _write(self, result):
            self.wfile.write(_encode_reply(result))

    return _Handler


def _encode_reply(result) -> bytes:
    if result is None:
        return b"$-1\r\n"
    if isinstance(result, tuple) and result and result[0] == "ERR":
        return b"-ERR " + str(result[1]).encode() + _CRLF
    if isinstance(result, bool):
        return b":1\r\n" if result else b":0\r\n"
    if isinstance(result, int):
        return b":" + str(result).encode() + _CRLF
    if isinstance(result, bytes):
        # simple-string style for OK/PONG, bulk for data
        if result in (b"OK", b"PONG"):
            return b"+" + result + _CRLF
        return b"$" + str(len(result)).encode() + _CRLF + result + _CRLF
    b = str(result).encode()
    return b"$" + str(len(b)).encode() + _CRLF + b + _CRLF


class _ThreadingTCPServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


class SmritiServer:
    """Runs Smriti as its own cache server — no Docker, no image."""

    def __init__(self, *, bind: str = "127.0.0.1", port: int = 6390,
                 store: SmritiStore | None = None,
                 password: str | None = None,
                 sweep_interval: float = 30.0):
        self.bind = bind
        self.port = port
        self.store = store or SmritiStore()
        self._password = password.encode() if password else None
        self.sweep_interval = sweep_interval
        self._srv: _ThreadingTCPServer | None = None

    def _start_sweeper(self):
        def loop():
            while self._srv is not None:
                time.sleep(self.sweep_interval)
                try:
                    self.store.sweep()
                except Exception:
                    pass
        threading.Thread(target=loop, daemon=True).start()

    def serve(self) -> None:
        self._srv = _ThreadingTCPServer(
            (self.bind, self.port),
            _make_request_handler(self.store, password=self._password))
        self.port = self._srv.server_address[1]
        self._start_sweeper()
        self._srv.serve_forever()

    def start_background(self) -> SmritiServer:
        self._srv = _ThreadingTCPServer(
            (self.bind, self.port),
            _make_request_handler(self.store, password=self._password))
        self.port = self._srv.server_address[1]
        self._start_sweeper()
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()
        return self

    def shutdown(self) -> None:
        if self._srv:
            srv, self._srv = self._srv, None
            srv.shutdown()
            srv.server_close()


# ===========================================================================
# Client (pure stdlib, RESP)
# ===========================================================================

class SmritiClient:
    """A tiny pure-stdlib RESP client — works against Smriti *or* real Redis."""

    def __init__(self, host: str = "127.0.0.1", port: int = 6390, *,
                 password: str | None = None, timeout: float = 5.0):
        self.host, self.port = host, port
        self.password = password
        self.timeout = timeout
        self._lock = threading.Lock()
        self._sock: socket.socket | None = None
        self._rfile = None

    def _connect(self):
        s = socket.create_connection((self.host, self.port), self.timeout)
        self._sock = s
        self._rfile = s.makefile("rb")
        if self.password:
            # Send AUTH directly here — NOT via _command(), which would try to
            # re-acquire the (non-reentrant) lock we may already hold and
            # deadlock. This is the connection-setup handshake.
            self._sock.sendall(resp_encode(b"AUTH", self.password.encode()))
            resp_read(self._rfile)

    def _ensure(self):
        if self._sock is None:
            self._connect()

    def _command(self, *parts):
        with self._lock:
            self._ensure()
            try:
                self._sock.sendall(resp_encode(*parts))
                return resp_read(self._rfile)
            except (ConnectionError, OSError):
                # one reconnect attempt
                self.close()
                self._connect()
                self._sock.sendall(resp_encode(*parts))
                return resp_read(self._rfile)

    # convenience API
    def ping(self) -> bool:
        return self._command(b"PING") == b"PONG"

    def set(self, key: str, value: bytes | str, ex: int | None = None):
        v = value if isinstance(value, bytes) else str(value).encode()
        if ex:
            return self._command(b"SET", key, v, b"EX", ex)
        return self._command(b"SET", key, v)

    def get(self, key: str):
        return self._command(b"GET", key)

    def delete(self, *keys: str) -> int:
        return self._command(b"DEL", *keys)

    def incr(self, key: str) -> int:
        return self._command(b"INCR", key)

    def expire(self, key: str, seconds: int) -> int:
        return self._command(b"EXPIRE", key, seconds)

    def ttl(self, key: str) -> int:
        return self._command(b"TTL", key)

    def exists(self, key: str) -> bool:
        return self._command(b"EXISTS", key) == 1

    def flushall(self):
        return self._command(b"FLUSHALL")

    def close(self):
        try:
            if self._rfile:
                self._rfile.close()
            if self._sock:
                self._sock.close()
        except Exception:
            pass
        self._sock = None
        self._rfile = None


# ===========================================================================
# ConjurePlugin adapter — slots into SHABD's existing cache/rate-limit hooks
# ===========================================================================

class SmritiCache:
    """Implements the SHABD ConjurePlugin cache + rate-limit interface backed
    by a Smriti (or Redis) server, so distributed cache/rate-limit works with
    zero external dependencies.

    Duck-typed against ConjurePlugin — importing shabd is not required here.
    """

    def __init__(self, client: SmritiClient, *, prefix: str = "shabd:"):
        self.c = client
        self.prefix = prefix

    def on_startup(self, app):
        pass

    def cache_get(self, key: str):
        import json
        raw = self.c.get(self.prefix + "c:" + key)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except Exception:
            return None

    def cache_set(self, key: str, value, ttl: int):
        import json
        try:
            self.c.set(self.prefix + "c:" + key,
                       json.dumps(value).encode(), ex=int(ttl) or None)
        except Exception:
            pass

    def check_rate_limit(self, key: str, rate: int, per_seconds: int) -> bool:
        """Fixed-window counter via INCR+EXPIRE. Returns True if allowed."""
        k = self.prefix + "r:" + key
        try:
            n = self.c.incr(k)
            if n == 1:
                self.c.expire(k, per_seconds)
            return n <= rate
        except Exception:
            return True  # fail-open on cache outage

    def close(self):
        self.c.close()


# ===========================================================================
# Config selection
# ===========================================================================

def cache_from_config(cfg: dict):
    """Build the cache provider selected in config.yaml's `cache` block.

    Returns a plugin object (or None for the in-process builtin, in which case
    the caller keeps SHABD's default TTLCache)."""
    provider = (cfg or {}).get("provider", "builtin")
    if provider == "smriti":
        s = (cfg or {}).get("smriti", {})
        client = SmritiClient(host=s.get("host", "127.0.0.1"),
                              port=int(s.get("port", 6390)),
                              password=s.get("password"))
        return SmritiCache(client)
    if provider == "redis":
        # Defer to the existing RedisPlugin (optional `redis` dependency).
        from shabd import RedisPlugin
        r = (cfg or {}).get("redis", {})
        return RedisPlugin(r.get("url", "redis://localhost:6379"))
    return None  # builtin in-process cache
