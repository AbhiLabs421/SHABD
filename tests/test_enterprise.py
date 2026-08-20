"""
Enterprise extension tests (v2.2): idempotency, multi-secret rotation,
Grimoire persistence, Prometheus exposition, traceparent codec, audit
webhook, per-spell concurrency, graceful drain.

Run:
    python tests/test_enterprise.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shabd import (  # noqa: E402
    SHABD,
    AuthError,
    ConjureError,
    Email,
    IdempotencyStore,
    PromExporter,
    TraceContextCodec,
)
from shabd_client import SHABDClient, SHABDClientError  # noqa: E402


# ---------------------------------------------------------------------------
# 1. Idempotency-Key
# ---------------------------------------------------------------------------
class IdempotencyTests(unittest.TestCase):
    def test_same_key_replays_result(self):
        app = SHABD("ide", secret="x" * 32, require_auth=False)
        counter = {"n": 0}

        @app.spell
        def charge(amount: float) -> dict:
            counter["n"] += 1
            return {"ok": True, "txn": counter["n"], "amount": amount}

        async def go():
            ctx = app._make_context(
                transport="http", auth_header=None,
                idempotency_key="k1",
            )
            r1 = await app._invoke_async(app._spells["charge"], {"amount": 100.0}, ctx)
            ctx2 = app._make_context(
                transport="http", auth_header=None,
                idempotency_key="k1",
            )
            r2 = await app._invoke_async(app._spells["charge"], {"amount": 100.0}, ctx2)
            return r1, r2

        r1, r2 = asyncio.run(go())
        self.assertEqual(r1, r2)
        self.assertEqual(counter["n"], 1)

    def test_same_key_different_body_raises(self):
        store = IdempotencyStore()
        store.store("k1", "pay", {"amount": 100}, True, {"ok": True})
        with self.assertRaises(ConjureError):
            store.lookup("k1", "pay", {"amount": 200})


# ---------------------------------------------------------------------------
# 2. Multi-secret rotation
# ---------------------------------------------------------------------------
class RotationTests(unittest.TestCase):
    def test_old_token_still_valid_during_rotation(self):
        old_secret = b"old-secret-" + b"0" * 32
        new_secret = b"new-secret-" + b"0" * 32

        # Server-1: only old secret
        app_old = SHABD("a", secret=old_secret, require_auth=True)
        token = app_old.issue_token("alice", scopes=["*"])

        # Server-2: rotated to new, but still accepts old
        app_new = SHABD("a", secret=new_secret, require_auth=True,
                        additional_secrets=[old_secret])
        payload = app_new.tokens.verify(token)
        self.assertEqual(payload["sub"], "alice")

    def test_rejects_unrelated_token(self):
        app = SHABD("a", secret=b"x" * 32, require_auth=True,
                    additional_secrets=[b"old" + b"0" * 32])
        fake = "garbage.garbage"
        with self.assertRaises(AuthError):
            app.tokens.verify(fake)


# ---------------------------------------------------------------------------
# 3. Grimoire JSONL persistence
# ---------------------------------------------------------------------------
class PersistenceTests(unittest.TestCase):
    def test_survives_restart(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "audit.jsonl")
            app1 = SHABD("p", secret="x" * 32, require_auth=False,
                         grimoire_log_path=path)

            @app1.spell
            def add(a: int, b: int) -> int:
                return a + b

            app1.invoke("add", {"a": 1, "b": 2})
            app1.invoke("add", {"a": 3, "b": 4})

            head1 = app1.grimoire.head()
            self.assertEqual(len(app1.grimoire._pages), 2)
            if app1._grimoire_log:
                app1._grimoire_log.close()

            # Restart — same secret, same log file
            app2 = SHABD("p", secret="x" * 32, require_auth=False,
                         grimoire_log_path=path)
            self.assertEqual(len(app2.grimoire._pages), 2)
            self.assertEqual(app2.grimoire.head(), head1)
            self.assertTrue(app2.grimoire.verify()["ok"])


# ---------------------------------------------------------------------------
# 4. Prometheus exposition
# ---------------------------------------------------------------------------
class PrometheusTests(unittest.TestCase):
    def test_format_includes_required_lines(self):
        app = SHABD("prom", secret="x" * 32, require_auth=False)

        @app.spell
        def echo(x: str) -> str:
            return x

        app.invoke("echo", {"x": "hi"})
        text = PromExporter.render(app.metrics, app.name)
        self.assertIn("# HELP shabd_info", text)
        self.assertIn("# TYPE shabd_calls_total counter", text)
        self.assertIn('shabd_calls_total{spell="echo",status="ok"}', text)
        # Must end with a newline (Prometheus parser requirement)
        self.assertTrue(text.endswith("\n"))


# ---------------------------------------------------------------------------
# 5. W3C TraceContext codec
# ---------------------------------------------------------------------------
class TraceTests(unittest.TestCase):
    def test_decode_valid_header(self):
        h = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
        trace, parent, flags = TraceContextCodec.decode(h)
        self.assertEqual(trace, "0af7651916cd43dd8448eb211c80319c")
        self.assertEqual(parent, "b7ad6b7169203331")
        self.assertEqual(flags, 1)

    def test_decode_invalid_header(self):
        self.assertEqual(TraceContextCodec.decode(None), (None, None, 0))
        self.assertEqual(TraceContextCodec.decode("nope"), (None, None, 0))

    def test_encode_roundtrips(self):
        tid = "0" * 32
        sid = "1" * 16
        h = TraceContextCodec.encode(tid, sid, sampled=True)
        self.assertEqual(TraceContextCodec.decode(h), (tid, sid, 1))

    def test_context_inherits_trace_id_from_header(self):
        app = SHABD("tr", secret="x" * 32, require_auth=False)
        parent_tid = "abc123" + "0" * 26
        parent_sid = "0123456789abcdef"
        h = TraceContextCodec.encode(parent_tid, parent_sid)
        ctx = app._make_context(transport="http", auth_header=None,
                                traceparent=h)
        self.assertEqual(ctx.trace_id, parent_tid)
        self.assertEqual(ctx.parent_span_id, parent_sid)


# ---------------------------------------------------------------------------
# 6. Per-spell concurrency cap
# ---------------------------------------------------------------------------
class ConcurrencyTests(unittest.TestCase):
    def test_semaphore_serializes_calls(self):
        app = SHABD("cc", secret="x" * 32, require_auth=False)
        observed: list = []

        @app.spell(max_concurrent=1)
        async def heavy(i: int) -> int:
            observed.append(("start", i))
            await asyncio.sleep(0.05)
            observed.append(("end", i))
            return i

        async def go():
            await asyncio.gather(
                app.invoke_async("heavy", {"i": 1}),
                app.invoke_async("heavy", {"i": 2}),
                app.invoke_async("heavy", {"i": 3}),
            )

        asyncio.run(go())
        # With max_concurrent=1, no two "start" events can be adjacent
        # without an "end" in between.
        for i in range(0, len(observed) - 1, 2):
            self.assertEqual(observed[i][0], "start")
            self.assertEqual(observed[i + 1][0], "end")


# ---------------------------------------------------------------------------
# 7. HTTP probes & client SDK
# ---------------------------------------------------------------------------
class HTTPClientTests(unittest.TestCase):
    PORT = 18790

    @classmethod
    def setUpClass(cls):
        cls.app = SHABD("http", secret="x" * 32, require_auth=False)

        @cls.app.spell
        def add(a: int, b: int) -> int:
            return a + b

        @cls.app.spell
        def kyc(email: Email) -> dict:
            return {"domain": str(email).split("@")[1]}

        cls.thread = threading.Thread(
            target=cls.app.serve,
            kwargs={"host": "127.0.0.1", "port": cls.PORT,
                    "shutdown_grace_s": 1.0},
            daemon=True,
        )
        cls.thread.start()
        for _ in range(40):
            try:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{cls.PORT}/healthz", timeout=1)
                return
            except Exception:
                time.sleep(0.1)
        raise RuntimeError("server did not come up")

    def test_healthz_and_readyz(self):
        c = SHABDClient(f"http://127.0.0.1:{self.PORT}", retries=0)
        h = c.health()
        self.assertEqual(h["status"], "ok")
        self.assertTrue(c.ready())

    def test_cast_and_didyoumean(self):
        c = SHABDClient(f"http://127.0.0.1:{self.PORT}", retries=0)
        self.assertEqual(c.cast("add", {"a": 4, "b": 5}), 9)
        try:
            c.cast("addd", {"a": 1, "b": 2})
            self.fail("expected SHABDClientError")
        except SHABDClientError as e:
            self.assertEqual(e.code, "spell_not_found")
            self.assertIn("add", e.did_you_mean or [])

    def test_prometheus_format_over_http(self):
        c = SHABDClient(f"http://127.0.0.1:{self.PORT}", retries=0)
        c.cast("add", {"a": 1, "b": 1})
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.PORT}/metrics?format=prom",
            headers={"accept": "text/plain"},
        )
        with urllib.request.urlopen(req) as r:
            body = r.read().decode()
            ct = r.headers.get("content-type", "")
        self.assertIn("text/plain", ct)
        self.assertIn("shabd_calls_total", body)

    def test_traceparent_in_propagates(self):
        c = SHABDClient(f"http://127.0.0.1:{self.PORT}", retries=0)
        # Just call — we can't easily inspect the server-side ctx, but the
        # path must not error.
        c.cast("add", {"a": 1, "b": 2},
               traceparent="00-" + "a" * 32 + "-" + "b" * 16 + "-01")

    def test_idempotency_over_http(self):
        c = SHABDClient(f"http://127.0.0.1:{self.PORT}", retries=0)
        c.cast("add", {"a": 1, "b": 1}, idempotency_key="ide-1")
        c.cast("add", {"a": 1, "b": 1}, idempotency_key="ide-1")  # replay
        # Same key + different body must return a structured error
        try:
            c.cast("add", {"a": 5, "b": 5}, idempotency_key="ide-1")
            self.fail("expected idempotency_conflict")
        except SHABDClientError as e:
            self.assertEqual(e.code, "idempotency_conflict")

    def test_tools_for_openai_format(self):
        c = SHABDClient(f"http://127.0.0.1:{self.PORT}", retries=0)
        tools = c.tools_for_openai()
        names = {t["function"]["name"] for t in tools}
        self.assertIn("add", names)
        self.assertIn("kyc", names)

    def test_grimoire_verify_endpoint(self):
        c = SHABDClient(f"http://127.0.0.1:{self.PORT}", retries=0)
        c.cast("add", {"a": 1, "b": 2})
        v = c.grimoire_verify()
        self.assertTrue(v["ok"])


def main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for cls in (IdempotencyTests, RotationTests, PersistenceTests,
                PrometheusTests, TraceTests, ConcurrencyTests,
                HTTPClientTests):
        suite.addTests(loader.loadTestsFromTestCase(cls))
    runner = unittest.TextTestRunner(verbosity=2, stream=sys.stdout)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
