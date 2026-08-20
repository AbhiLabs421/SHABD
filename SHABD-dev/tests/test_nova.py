"""Tests for the Nova connector — SHABD drives an external RAG pipeline
service (Tenants -> Pipelines -> Ingest -> Query) and exposes a pipeline
as a tool. Uses a mock implementing the RAG Generic Service OpenAPI."""
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

PORT_BASE = 31200


class _Nova(http.server.BaseHTTPRequestHandler):
    tenants: list = []
    pipelines: list = []
    last_ingest_body = b""

    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):  # noqa: N802
        p = self.path.split("?")[0]
        if p == "/tenants":
            return self._send(200, {"items": _Nova.tenants,
                                    "total": len(_Nova.tenants),
                                    "offset": 0, "limit": 100})
        if p == "/pipelines":
            return self._send(200, {"items": _Nova.pipelines,
                                    "total": len(_Nova.pipelines),
                                    "offset": 0, "limit": 100})
        if p.endswith("/stats"):
            return self._send(200, {"pipeline_id": "p1",
                                    "chunk_count": 3})
        self._send(404, {"detail": "nf"})

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(n)
        p = self.path.split("?")[0]
        if p == "/tenants":
            body = json.loads(raw)
            t = {"id": f"t{len(_Nova.tenants) + 1}",
                 "name": body["name"],
                 "description": body.get("description")}
            _Nova.tenants.append(t)
            return self._send(201, t)
        if p == "/pipelines":
            body = json.loads(raw)
            pl = {"id": f"p{len(_Nova.pipelines) + 1}",
                  "tenant_id": body["tenant_id"],
                  "name": body["name"],
                  "config": body.get("config", {}),
                  "document_count": 0, "chunk_count": 0}
            _Nova.pipelines.append(pl)
            return self._send(201, pl)
        if p.endswith("/ingest"):
            _Nova.last_ingest_body = raw
            return self._send(202, {"document_id": "d1",
                                    "filename": "f.txt",
                                    "status": "indexed",
                                    "chunks_indexed": 3})
        if p.endswith("/query"):
            body = json.loads(raw)
            return self._send(200, {
                "pipeline_id": "p1", "query": body["query"],
                "results": [{"id": "c1", "score": 0.9,
                             "text": "12 casual leaves per year.",
                             "metadata": {}, "filename": "policy.txt"}],
                "total": 1})
        self._send(404, {"detail": "nf"})

    def do_DELETE(self):  # noqa: N802
        self._send(204, {})


def _start_mock():
    _Nova.tenants = []
    _Nova.pipelines = []
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Nova)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.2)
    return srv, srv.server_address[1]


class NovaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mock, port = _start_mock()
        cls.base = f"http://127.0.0.1:{port}"

    def setUp(self):
        os.environ["SHABD_UI_BOOTSTRAP_USER"] = "root"
        os.environ["SHABD_UI_BOOTSTRAP_PASSWORD"] = "rootpw"
        _Nova.tenants = []
        _Nova.pipelines = []
        self.app = SHABD("n", secret="x" * 32, require_auth=False)
        self.ui = UIServer(self.app, bind="127.0.0.1",
                            port=PORT_BASE + 20, superusers=["root"])
        self.sess = self.ui._login("root", "rootpw")
        self.ui.nova_set_config(self.sess, base_url=self.base)

    def tearDown(self):
        os.environ.pop("SHABD_UI_BOOTSTRAP_PASSWORD", None)

    def test_config_redacts_key(self):
        self.ui.nova_set_config(
            self.sess, base_url=self.base, api_key="secret")
        c = self.ui.nova_get_config()
        self.assertEqual(c["api_key"], "***")
        self.assertTrue(c["configured"])

    def test_config_bad_url(self):
        with self.assertRaises(UIError) as ctx:
            self.ui.nova_set_config(self.sess, base_url="ftp://x")
        self.assertEqual(ctx.exception.status, 400)

    def test_call_without_config(self):
        ui2 = UIServer(SHABD("z", secret="x" * 32, require_auth=False),
                       bind="127.0.0.1", port=PORT_BASE + 30,
                       superusers=["root"])
        with self.assertRaises(UIError) as ctx:
            ui2.nova_tenants()
        self.assertEqual(ctx.exception.status, 400)

    def test_create_and_list_tenant(self):
        self.ui.nova_create_tenant(self.sess, name="TCS")
        ts = self.ui.nova_tenants()
        self.assertEqual(len(ts), 1)
        self.assertEqual(ts[0]["name"], "TCS")

    def test_create_and_list_pipeline(self):
        t = self.ui.nova_create_tenant(self.sess, name="TCS")
        self.ui.nova_create_pipeline(
            self.sess, tenant_id=t["id"], name="Doc",
            config={"chunk_size": 500})
        pls = self.ui.nova_pipelines(t["id"])
        self.assertEqual(len(pls), 1)
        self.assertEqual(pls[0]["name"], "Doc")

    def test_ingest_sends_multipart_file(self):
        t = self.ui.nova_create_tenant(self.sess, name="TCS")
        pl = self.ui.nova_create_pipeline(
            self.sess, tenant_id=t["id"], name="Doc")
        r = self.ui.nova_ingest_text(
            self.sess, pid=pl["id"], filename="policy.txt",
            text="hello world")
        self.assertEqual(r["status"], "indexed")
        self.assertIn(b'filename="policy.txt"', _Nova.last_ingest_body)
        self.assertIn(b"hello world", _Nova.last_ingest_body)

    def test_ingest_appends_txt_extension(self):
        t = self.ui.nova_create_tenant(self.sess, name="TCS")
        pl = self.ui.nova_create_pipeline(
            self.sess, tenant_id=t["id"], name="Doc")
        self.ui.nova_ingest_text(
            self.sess, pid=pl["id"], filename="noext", text="x")
        self.assertIn(b'filename="noext.txt"', _Nova.last_ingest_body)

    def test_ingest_file_forwards_bytes_and_ctype(self):
        t = self.ui.nova_create_tenant(self.sess, name="TCS")
        pl = self.ui.nova_create_pipeline(
            self.sess, tenant_id=t["id"], name="Doc")
        blob = b"%PDF-1.4 fake pdf bytes \x00\x01\x02"
        r = self.ui.nova_ingest_file(
            self.sess, pid=pl["id"], filename="report.pdf",
            content=blob, content_type="application/pdf")
        self.assertEqual(r["status"], "indexed")
        body = _Nova.last_ingest_body
        self.assertIn(b'filename="report.pdf"', body)
        self.assertIn(b"Content-Type: application/pdf", body)
        self.assertIn(blob, body)

    def test_ingest_file_rejects_empty(self):
        t = self.ui.nova_create_tenant(self.sess, name="TCS")
        pl = self.ui.nova_create_pipeline(
            self.sess, tenant_id=t["id"], name="Doc")
        with self.assertRaises(UIError) as ctx:
            self.ui.nova_ingest_file(
                self.sess, pid=pl["id"], filename="x.pdf",
                content=b"")
        self.assertEqual(ctx.exception.status, 400)

    def test_query_returns_results(self):
        t = self.ui.nova_create_tenant(self.sess, name="TCS")
        pl = self.ui.nova_create_pipeline(
            self.sess, tenant_id=t["id"], name="Doc")
        r = self.ui.nova_query(pl["id"], "casual leaves")
        self.assertEqual(len(r["results"]), 1)
        self.assertIn("casual", r["results"][0]["text"])

    def test_expose_registers_spell_and_proxies(self):
        t = self.ui.nova_create_tenant(self.sess, name="TCS")
        pl = self.ui.nova_create_pipeline(
            self.sess, tenant_id=t["id"], name="Doc Pipeline")
        e = self.ui.nova_expose_pipeline(
            self.sess, pid=pl["id"], name="Doc Pipeline")
        self.assertIn(e["spell"], self.app._spells)
        out = self.app.invoke(e["spell"], {"question": "leaves?"})
        self.assertIn("casual", out["answer"])

    def test_exposed_in_manifest(self):
        t = self.ui.nova_create_tenant(self.sess, name="TCS")
        pl = self.ui.nova_create_pipeline(
            self.sess, tenant_id=t["id"], name="Doc")
        e = self.ui.nova_expose_pipeline(
            self.sess, pid=pl["id"], name="Doc")
        names = {s["name"] for s in self.app.manifest()["spells"]}
        self.assertIn(e["spell"], names)

    def test_delete_pipeline_removes_tool(self):
        t = self.ui.nova_create_tenant(self.sess, name="TCS")
        pl = self.ui.nova_create_pipeline(
            self.sess, tenant_id=t["id"], name="Doc")
        e = self.ui.nova_expose_pipeline(
            self.sess, pid=pl["id"], name="Doc")
        self.ui.nova_delete_pipeline(self.sess, pl["id"])
        self.assertNotIn(e["spell"], self.app._spells)

    def test_config_not_leaked_in_get(self):
        self.ui.nova_set_config(
            self.sess, base_url=self.base, api_key="topsecret")
        self.assertNotIn(
            "topsecret", json.dumps(self.ui.nova_get_config()))


def main() -> int:
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromTestCase(NovaTests)
    return 0 if unittest.TextTestRunner(
        verbosity=2, stream=sys.stdout).run(suite).wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
