"""Tests for the SQL Intelligence connector — SHABD proxies to an
EXTERNAL RAG/SQL service via its API and exposes it as a tool."""
from __future__ import annotations

import http.server
import json
import os
import socketserver
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shabd import SHABD  # noqa: E402
from shabd_ui import UIError, UIServer  # noqa: E402

PORT_BASE = 30900
_MOCK_PORT = 0  # ephemeral; real port read after bind


class _Mock(http.server.BaseHTTPRequestHandler):
    """Stand-in for the external SQL Intelligence service."""
    last_auth = ""
    last_body: dict = {}

    def log_message(self, *a):
        pass

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        _Mock.last_auth = self.headers.get("authorization", "")
        _Mock.last_body = body
        q = body.get("query", "")
        resp = {"answer": f"SQL for: {q}",
                "answer_md": "x",
                "thread_id": "thread-abc",
                "sources": [{"text": "schema", "score": 0.9}]}
        b = json.dumps(resp).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


def _start_mock():
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    srv = socketserver.ThreadingTCPServer(
        ("127.0.0.1", 0), _Mock)   # ephemeral port
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.2)
    return srv, srv.server_address[1]


class SqlServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mock, port = _start_mock()
        cls.base = f"http://127.0.0.1:{port}"

    def setUp(self):
        os.environ["SHABD_UI_BOOTSTRAP_USER"] = "root"
        os.environ["SHABD_UI_BOOTSTRAP_PASSWORD"] = "rootpw"
        self.app = SHABD("s", secret="x" * 32, require_auth=False)
        self.ui = UIServer(self.app, bind="127.0.0.1",
                            port=PORT_BASE + 20, superusers=["root"])
        self.sess = self.ui._login("root", "rootpw")

    def tearDown(self):
        os.environ.pop("SHABD_UI_BOOTSTRAP_PASSWORD", None)

    def test_create_and_list(self):
        self.ui.create_sql_service(
            self.sess, name="svc", base_url=self.base)
        svcs = self.ui.list_sql_services()
        self.assertEqual(len(svcs), 1)
        self.assertEqual(svcs[0]["name"], "svc")
        self.assertFalse(svcs[0]["exposed"])

    def test_create_bad_url(self):
        with self.assertRaises(UIError) as ctx:
            self.ui.create_sql_service(
                self.sess, name="svc", base_url="ftp://x")
        self.assertEqual(ctx.exception.status, 400)

    def test_test_proxies_and_sends_auth(self):
        self.ui.create_sql_service(
            self.sess, name="svc", base_url=self.base,
            api_key="tok123", auth_style="bearer")
        r = self.ui.test_sql_service("svc", "show members")
        self.assertIn("SQL for: show members", r["answer"])
        self.assertEqual(_Mock.last_auth, "Bearer tok123")

    def test_test_unknown_service(self):
        with self.assertRaises(UIError) as ctx:
            self.ui.test_sql_service("ghost", "x")
        self.assertEqual(ctx.exception.status, 404)

    def test_expose_registers_spell(self):
        self.ui.create_sql_service(
            self.sess, name="svc", base_url=self.base)
        self.ui.expose_sql_service(self.sess, "svc")
        self.assertIn("sql_svc", self.app._spells)

    def test_exposed_spell_proxies(self):
        self.ui.create_sql_service(
            self.sess, name="svc", base_url=self.base)
        self.ui.expose_sql_service(self.sess, "svc")
        out = self.app.invoke("sql_svc", {"question": "count rows"})
        self.assertIn("SQL for: count rows", out["answer"])

    def test_exposed_spell_in_manifest(self):
        self.ui.create_sql_service(
            self.sess, name="svc", base_url=self.base)
        self.ui.expose_sql_service(self.sess, "svc")
        names = {s["name"] for s in self.app.manifest()["spells"]}
        self.assertIn("sql_svc", names)

    def test_unreachable_service_errors(self):
        self.ui.create_sql_service(
            self.sess, name="down",
            base_url="http://127.0.0.1:1")  # nothing there
        with self.assertRaises(UIError) as ctx:
            self.ui.test_sql_service("down", "x")
        self.assertIn(ctx.exception.status, (502, 504))

    def test_delete_removes_spell(self):
        self.ui.create_sql_service(
            self.sess, name="svc", base_url=self.base)
        self.ui.expose_sql_service(self.sess, "svc")
        self.ui.delete_sql_service(self.sess, "svc")
        self.assertNotIn("sql_svc", self.app._spells)
        self.assertEqual(self.ui.list_sql_services(), [])

    def test_api_key_not_leaked_in_list(self):
        self.ui.create_sql_service(
            self.sess, name="svc", base_url=self.base,
            api_key="supersecret")
        svc = self.ui.list_sql_services()[0]
        self.assertNotIn("supersecret", json.dumps(svc))
        self.assertTrue(svc["has_key"])

    def test_extra_fields_sent_in_body(self):
        self.ui.create_sql_service(
            self.sess, name="svc", base_url=self.base,
            extra={"top_k": "7", "platform": "web",
                   "collection": "col1", "table": ""})
        self.ui.test_sql_service("svc", "hi")
        body = _Mock.last_body
        self.assertEqual(body.get("top_k"), 7)          # coerced to int
        self.assertEqual(body.get("platform"), "web")
        self.assertEqual(body.get("collection"), "col1")
        self.assertNotIn("table", body)                 # empty dropped

    def test_thread_id_round_trips(self):
        self.ui.create_sql_service(
            self.sess, name="svc", base_url=self.base)
        r1 = self.ui.test_sql_service("svc", "first")
        self.assertEqual(r1.get("thread_id"), "thread-abc")
        # sending it back attaches it to the outgoing body
        self.ui.test_sql_service("svc", "second",
                                 thread_id="thread-abc")
        self.assertEqual(_Mock.last_body.get("thread_id"), "thread-abc")


def main() -> int:
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromTestCase(SqlServiceTests)
    return 0 if unittest.TextTestRunner(
        verbosity=2, stream=sys.stdout).run(suite).wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
