"""Tests for shabd_smriti — the built-in Redis-like cache/coordination server.

Easy    : RESP encode/decode, store ops in-process.
Medium  : full client<->server round trip over TCP (SET/GET/DEL/INCR/…).
Hard    : TTL expiry, AUTH, AOF persistence across restart.
Complex : SmritiCache as a ConjurePlugin (cache + distributed rate-limit).
"""
from __future__ import annotations

import io
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shabd_smriti import (  # noqa: E402
    SmritiCache,
    SmritiClient,
    SmritiServer,
    SmritiStore,
    resp_encode,
    resp_read,
)


class RespTests(unittest.TestCase):
    def test_encode_array_of_bulk(self):
        self.assertEqual(resp_encode("SET", "k", "v"),
                         b"*3\r\n$3\r\nSET\r\n$1\r\nk\r\n$1\r\nv\r\n")

    def test_read_roundtrip(self):
        buf = io.BytesIO(resp_encode("GET", "key"))
        self.assertEqual(resp_read(buf), [b"GET", b"key"])

    def test_read_integer_and_bulk_and_null(self):
        self.assertEqual(resp_read(io.BytesIO(b":42\r\n")), 42)
        self.assertEqual(resp_read(io.BytesIO(b"$3\r\nabc\r\n")), b"abc")
        self.assertIsNone(resp_read(io.BytesIO(b"$-1\r\n")))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.s = SmritiStore()

    def _do(self, *parts):
        return self.s.execute([p.encode() if isinstance(p, str) else p
                               for p in parts])

    def test_set_get(self):
        self.assertEqual(self._do("SET", "a", "1"), b"OK")
        self.assertEqual(self._do("GET", "a"), b"1")

    def test_incr(self):
        self.assertEqual(self._do("INCR", "n"), 1)
        self.assertEqual(self._do("INCR", "n"), 2)

    def test_del_and_exists(self):
        self._do("SET", "a", "1")
        self.assertEqual(self._do("EXISTS", "a"), 1)
        self.assertEqual(self._do("DEL", "a"), 1)
        self.assertEqual(self._do("EXISTS", "a"), 0)

    def test_ttl_expiry(self):
        self._do("SET", "a", "1", "EX", "1")
        self.assertEqual(self._do("GET", "a"), b"1")
        self.assertIn(self._do("TTL", "a"), (0, 1))
        time.sleep(1.1)
        self.assertIsNone(self._do("GET", "a"))
        self.assertEqual(self._do("TTL", "a"), -2)

    def test_expire_command(self):
        self._do("SET", "a", "1")
        self.assertEqual(self._do("TTL", "a"), -1)     # no expiry
        self.assertEqual(self._do("EXPIRE", "a", "100"), 1)
        self.assertGreater(self._do("TTL", "a"), 90)


class ServerClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = SmritiServer(bind="127.0.0.1", port=0).start_background()
        cls.port = cls.srv.port
        time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def _client(self):
        return SmritiClient("127.0.0.1", self.port)

    def test_ping(self):
        self.assertTrue(self._client().ping())

    def test_set_get_over_tcp(self):
        c = self._client()
        c.set("greeting", "namaste")
        self.assertEqual(c.get("greeting"), b"namaste")

    def test_incr_del_over_tcp(self):
        c = self._client()
        c.delete("counter")
        self.assertEqual(c.incr("counter"), 1)
        self.assertEqual(c.incr("counter"), 2)
        self.assertEqual(c.delete("counter"), 1)

    def test_ex_ttl_over_tcp(self):
        c = self._client()
        c.set("temp", "x", ex=100)
        self.assertGreater(c.ttl("temp"), 90)

    def test_two_clients_share_state(self):
        a, b = self._client(), self._client()
        a.set("shared", "yes")
        self.assertEqual(b.get("shared"), b"yes")  # different connection


class AuthTests(unittest.TestCase):
    def test_auth_required(self):
        srv = SmritiServer(bind="127.0.0.1", port=0,
                           password="s3cr3t").start_background()
        try:
            time.sleep(0.1)
            # correct password
            good = SmritiClient("127.0.0.1", srv.port, password="s3cr3t")
            self.assertTrue(good.ping())
            # wrong password -> command refused
            bad = SmritiClient("127.0.0.1", srv.port, password="wrong")
            r = bad._command(b"GET", b"x")
            self.assertIsInstance(r, tuple)  # ('ERR', ...)
        finally:
            srv.shutdown()


class AofTests(unittest.TestCase):
    def test_persists_across_restart(self):
        path = os.path.join(tempfile.mkdtemp(), "smriti.aof")
        s1 = SmritiStore(aof_path=path)
        s1.execute([b"SET", b"k", b"v"])
        s1.execute([b"INCR", b"n"])
        s1.execute([b"INCR", b"n"])
        # new store replays the AOF
        s2 = SmritiStore(aof_path=path)
        self.assertEqual(s2.execute([b"GET", b"k"]), b"v")
        self.assertEqual(s2.execute([b"GET", b"n"]), b"2")


class CachePluginTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = SmritiServer(bind="127.0.0.1", port=0).start_background()
        time.sleep(0.1)
        cls.cache = SmritiCache(SmritiClient("127.0.0.1", cls.srv.port))

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_cache_get_set_json(self):
        self.cache.cache_set("user:1", {"name": "amit", "n": 3}, ttl=60)
        self.assertEqual(self.cache.cache_get("user:1"),
                         {"name": "amit", "n": 3})

    def test_cache_miss_returns_none(self):
        self.assertIsNone(self.cache.cache_get("nope"))

    def test_distributed_rate_limit(self):
        # allow 3 per window; 4th is denied
        allowed = [self.cache.check_rate_limit("ip:1.2.3.4", 3, 60)
                   for _ in range(4)]
        self.assertEqual(allowed, [True, True, True, False])


def main() -> int:
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    ok = unittest.TextTestRunner(verbosity=2, stream=sys.stdout).run(suite)
    return 0 if ok.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
