"""Tests for v2.13:

  * External SHABD tool source — server B imports server A's tools as
    local proxy spells; they become usable everywhere (manifest, agent).
  * /query/<agent> public endpoint — ask a question, get an answer.
  * Force-tool-use flag plumbs through to the backend.
  * Source disconnect removes the imported spells.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shabd import SHABD  # noqa: E402
from shabd_ui import UIError, UIServer  # noqa: E402

PORT_BASE = 24000


def _new_app(name="t", *, require_auth=False) -> SHABD:
    return SHABD(name, secret="x" * 32, require_auth=require_auth)


def _start(ui: UIServer) -> None:
    threading.Thread(target=ui.serve, daemon=True).start()
    for _ in range(80):
        try:
            with urllib.request.urlopen(
                    f"http://{ui.bind}:{ui.port}/healthz",
                    timeout=1) as r:
                if r.status == 200:
                    return
        except Exception:
            time.sleep(0.05)
    raise RuntimeError("UI did not start")


def _serve_app(app: SHABD, port: int):
    threading.Thread(
        target=app.serve,
        kwargs={"host": "127.0.0.1", "port": port},
        daemon=True).start()
    for _ in range(80):
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/healthz", timeout=1) as r:
                if r.status == 200:
                    return
        except Exception:
            time.sleep(0.05)
    raise RuntimeError("app server did not start")


def _http(method, url, *, headers=None, body=None):
    req = urllib.request.Request(
        url, data=body, method=method, headers=dict(headers or {}))
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# External SHABD tool source
# ---------------------------------------------------------------------------


class ExternalShabdSourceTests(unittest.TestCase):
    """Server A (plain SHABD HTTP) exposes tools. The UI server imports
    them as proxy spells and can invoke them."""

    PORT_A = PORT_BASE + 1
    PORT_UI = PORT_BASE + 2

    @classmethod
    def setUpClass(cls):
        os.environ["SHABD_UI_BOOTSTRAP_USER"] = "root"
        os.environ["SHABD_UI_BOOTSTRAP_PASSWORD"] = "rootpw"
        # Remote server A
        cls.app_a = _new_app("server-a", require_auth=False)

        @cls.app_a.spell
        def multiply(a: int, b: int) -> int:
            return a * b

        _serve_app(cls.app_a, cls.PORT_A)

        # UI server (its own app is empty)
        cls.ui = UIServer(_new_app("ui-app"), bind="127.0.0.1",
                           port=cls.PORT_UI, superusers=["root"])
        _start(cls.ui)
        cls.sess = cls.ui._login("root", "rootpw")

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("SHABD_UI_BOOTSTRAP_PASSWORD", None)

    def test_connect_imports_tools(self):
        res = self.ui.connect_tool_source(
            self.sess, name="alpha", kind="shabd",
            url=f"http://127.0.0.1:{self.PORT_A}")
        self.assertGreaterEqual(res["count"], 1)
        self.assertIn("alpha__multiply", self.ui.app._spells)

    def test_imported_tool_is_invokable(self):
        if "alpha__multiply" not in self.ui.app._spells:
            self.ui.connect_tool_source(
                self.sess, name="alpha", kind="shabd",
                url=f"http://127.0.0.1:{self.PORT_A}")
        out = self.ui.app.invoke(
            "alpha__multiply", {"a": 6, "b": 7})
        self.assertEqual(out, 42)

    def test_imported_tool_in_manifest(self):
        if "alpha__multiply" not in self.ui.app._spells:
            self.ui.connect_tool_source(
                self.sess, name="alpha", kind="shabd",
                url=f"http://127.0.0.1:{self.PORT_A}")
        s, raw = _http(
            "GET", f"http://127.0.0.1:{self.PORT_UI}/manifest")
        self.assertEqual(s, 200)
        names = {sp["name"] for sp in json.loads(raw)["spells"]}
        self.assertIn("alpha__multiply", names)

    def test_disconnect_removes_tools(self):
        self.ui.connect_tool_source(
            self.sess, name="beta", kind="shabd",
            url=f"http://127.0.0.1:{self.PORT_A}")
        self.assertIn("beta__multiply", self.ui.app._spells)
        self.ui.disconnect_tool_source(self.sess, "beta")
        self.assertNotIn("beta__multiply", self.ui.app._spells)

    def test_connect_rejects_bad_url(self):
        with self.assertRaises(UIError) as ctx:
            self.ui.connect_tool_source(
                self.sess, name="x", kind="shabd",
                url="ftp://nope")
        self.assertEqual(ctx.exception.status, 400)

    def test_connect_rejects_bad_kind(self):
        with self.assertRaises(UIError) as ctx:
            self.ui.connect_tool_source(
                self.sess, name="x", kind="weird",
                url=f"http://127.0.0.1:{self.PORT_A}")
        self.assertEqual(ctx.exception.status, 400)


# ---------------------------------------------------------------------------
# /query/<agent> endpoint + force-tools
# ---------------------------------------------------------------------------


class QueryEndpointTests(unittest.TestCase):
    PORT = PORT_BASE + 10

    @classmethod
    def setUpClass(cls):
        os.environ["SHABD_UI_BOOTSTRAP_USER"] = "root"
        os.environ["SHABD_UI_BOOTSTRAP_PASSWORD"] = "rootpw"
        cls.app = _new_app("query-ui")

        @cls.app.spell
        def add_two_numbers(a: int, b: int) -> int:
            return a + b

        cls.ui = UIServer(cls.app, bind="127.0.0.1", port=cls.PORT,
                           superusers=["root"])
        _start(cls.ui)
        cls.sess = cls.ui._login("root", "rootpw")
        cls.ui.save_agent(
            cls.sess, name="arithmetic",
            system="You add numbers.", tools=["add_two_numbers"])
        cls.base = f"http://127.0.0.1:{cls.PORT}"

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("SHABD_UI_BOOTSTRAP_PASSWORD", None)

    def test_ask_agent_method(self):
        res = self.ui.ask_agent(
            agent_name="arithmetic", question="add 2 and 3")
        self.assertTrue(res["ok"])
        self.assertIn("answer", res)

    def test_ask_unknown_agent_404(self):
        with self.assertRaises(UIError) as ctx:
            self.ui.ask_agent(
                agent_name="ghost", question="hi")
        self.assertEqual(ctx.exception.status, 404)

    def test_query_endpoint_http(self):
        s, raw = _http(
            "POST", f"{self.base}/query/arithmetic",
            headers={"Content-Type": "application/json"},
            body=b'{"question":"add some numbers"}')
        self.assertEqual(s, 200)
        d = json.loads(raw)
        self.assertTrue(d["ok"])
        self.assertEqual(d["agent"], "arithmetic")

    def test_query_endpoint_unknown_agent(self):
        s, raw = _http(
            "POST", f"{self.base}/query/nope",
            headers={"Content-Type": "application/json"},
            body=b'{"question":"hi"}')
        self.assertEqual(s, 404)

    def test_query_endpoint_empty_question(self):
        s, _ = _http(
            "POST", f"{self.base}/query/arithmetic",
            headers={"Content-Type": "application/json"},
            body=b'{"question":""}')
        self.assertEqual(s, 400)

    def test_query_endpoint_accepts_valid_token(self):
        tok = self.app.issue_token("caller-bot", [], ttl=120)
        s, raw = _http(
            "POST", f"{self.base}/query/arithmetic",
            headers={"Content-Type": "application/json",
                      "Authorization": f"Bearer {tok}"},
            body=b'{"question":"add"}')
        self.assertEqual(s, 200)
        self.assertTrue(json.loads(raw)["ok"])

    def test_query_endpoint_rejects_bad_token(self):
        s, _ = _http(
            "POST", f"{self.base}/query/arithmetic",
            headers={"Content-Type": "application/json",
                      "Authorization": "Bearer not-a-real-token"},
            body=b'{"question":"add"}')
        self.assertEqual(s, 401)


class ForceToolsTests(unittest.TestCase):
    def setUp(self):
        os.environ["SHABD_UI_BOOTSTRAP_USER"] = "root"
        os.environ["SHABD_UI_BOOTSTRAP_PASSWORD"] = "rootpw"
        self.app = _new_app()

        @self.app.spell
        def add(a: int, b: int) -> int:
            return a + b

        self.ui = UIServer(self.app, bind="127.0.0.1",
                            port=PORT_BASE + 20, superusers=["root"])
        self.sess = self.ui._login("root", "rootpw")

    def tearDown(self):
        os.environ.pop("SHABD_UI_BOOTSTRAP_PASSWORD", None)

    def test_force_tools_saved_on_agent(self):
        self.ui.save_agent(
            self.sess, name="strict", system="add",
            tools=["add"], force_tools=True)
        self.assertTrue(self.ui._agents["strict"]["force_tools"])

    def test_backend_gets_force_flag(self):
        self.ui.set_llm_config(
            self.sess, backend="ollama",
            base_url="http://127.0.0.1:11434", model="x")
        be = self.ui.build_llm_backend(force_tools=True)
        self.assertTrue(be.force_tools)
        be2 = self.ui.build_llm_backend(force_tools=False)
        self.assertFalse(be2.force_tools)

    def test_sources_survive_restart_metadata(self):
        # The source config (not its live tools) persists; reconnect
        # is best-effort. Here we just check the metadata round-trips.
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        audit = os.path.join(tmp.name, "a.jsonl")
        try:
            app1 = SHABD("p", secret="x" * 32, require_auth=False,
                          grimoire_log_path=audit)
            ui1 = UIServer(app1, bind="127.0.0.1",
                            port=PORT_BASE + 21, superusers=["root"])
            sess1 = ui1._login("root", "rootpw")
            # Register a source pointing nowhere — connect will error
            # but the metadata should still be saved.
            try:
                ui1.connect_tool_source(
                    sess1, name="dead", kind="shabd",
                    url="http://127.0.0.1:1")  # nothing listening
            except UIError:
                pass
            self.assertIn("dead", ui1._tool_sources)
            # Restart: metadata reloaded
            app2 = SHABD("p", secret="x" * 32, require_auth=False,
                          grimoire_log_path=audit)
            ui2 = UIServer(app2, bind="127.0.0.1",
                            port=PORT_BASE + 22, superusers=["root"])
            self.assertIn("dead", ui2._tool_sources)
        finally:
            tmp.cleanup()


def main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for cls in (ExternalShabdSourceTests, QueryEndpointTests,
                ForceToolsTests):
        suite.addTests(loader.loadTestsFromTestCase(cls))
    return 0 if unittest.TextTestRunner(
        verbosity=2, stream=sys.stdout).run(suite).wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
