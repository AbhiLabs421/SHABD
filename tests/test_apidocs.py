"""Tests for the API docs: /openapi.json (machine-readable) and the
/api-docs page (human-readable)."""
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
from shabd_ui import UIServer  # noqa: E402

PORT_BASE = 26000


def _new_app(name="t") -> SHABD:
    return SHABD(name, secret="x" * 32, require_auth=False)


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


def _http(method, url, *, headers=None, body=None):
    req = urllib.request.Request(
        url, data=body, method=method, headers=dict(headers or {}))
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, dict(r.headers), r.read().decode(
                "utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read().decode(
            "utf-8", "replace")


class OpenApiSpecTests(unittest.TestCase):
    def setUp(self):
        os.environ["SHABD_UI_BOOTSTRAP_USER"] = "root"
        os.environ["SHABD_UI_BOOTSTRAP_PASSWORD"] = "rootpw"
        self.app = _new_app()

        @self.app.spell
        def add(a: int, b: int) -> int:
            return a + b

        @self.app.spell(scopes=["payments"])
        def pay(amount: int) -> dict:
            return {"ok": True}

        self.ui = UIServer(self.app, bind="127.0.0.1",
                            port=PORT_BASE + 1, superusers=["root"])
        self.sess = self.ui._login("root", "rootpw")
        self.ui.save_agent(
            self.sess, name="helper", system="help", tools=["add"])
        self.ui.save_intent(
            self.sess, name="math", keywords=["add"],
            route_to="helper")

    def tearDown(self):
        os.environ.pop("SHABD_UI_BOOTSTRAP_PASSWORD", None)

    def test_spec_is_valid_openapi(self):
        spec = self.ui.openapi_spec(base_url="http://x")
        self.assertEqual(spec["openapi"], "3.0.3")
        self.assertIn("paths", spec)
        self.assertEqual(spec["servers"][0]["url"], "http://x")

    def test_spec_has_core_endpoints(self):
        spec = self.ui.openapi_spec()
        for p in ("/healthz", "/manifest", "/grimoire/verify"):
            self.assertIn(p, spec["paths"])

    def test_spec_has_each_spell(self):
        spec = self.ui.openapi_spec()
        self.assertIn("/spells/add", spec["paths"])
        self.assertIn("/spells/pay", spec["paths"])

    def test_scoped_spell_requires_security(self):
        spec = self.ui.openapi_spec()
        pay = spec["paths"]["/spells/pay"]["post"]
        self.assertTrue(pay["security"])  # non-empty → bearer required
        add = spec["paths"]["/spells/add"]["post"]
        self.assertEqual(add["security"], [])  # open

    def test_spec_has_agent_query(self):
        spec = self.ui.openapi_spec()
        self.assertIn("/query/helper", spec["paths"])

    def test_spec_has_ask_when_intents_exist(self):
        spec = self.ui.openapi_spec()
        self.assertIn("/ask", spec["paths"])


class LiveDocsTests(unittest.TestCase):
    PORT = PORT_BASE + 10

    @classmethod
    def setUpClass(cls):
        os.environ["SHABD_UI_BOOTSTRAP_USER"] = "root"
        os.environ["SHABD_UI_BOOTSTRAP_PASSWORD"] = "rootpw"
        cls.app = _new_app("docs")

        @cls.app.spell
        def echo(msg: str) -> str:
            return msg

        cls.ui = UIServer(cls.app, bind="127.0.0.1", port=cls.PORT,
                           superusers=["root"])
        _start(cls.ui)
        cls.sess = cls.ui._login("root", "rootpw")
        cls.base = f"http://127.0.0.1:{cls.PORT}"

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("SHABD_UI_BOOTSTRAP_PASSWORD", None)

    def test_openapi_json_public(self):
        s, headers, raw = _http("GET", f"{self.base}/openapi.json")
        self.assertEqual(s, 200)
        spec = json.loads(raw)
        self.assertEqual(spec["openapi"], "3.0.3")
        # servers URL should reflect the request host
        self.assertIn("127.0.0.1", spec["servers"][0]["url"])

    def test_openapi_lists_echo_spell(self):
        s, _, raw = _http("GET", f"{self.base}/openapi.json")
        spec = json.loads(raw)
        self.assertIn("/spells/echo", spec["paths"])

    def test_api_docs_page_requires_session(self):
        # No cookie → redirected to login (urllib follows → 200 login)
        s, _, _ = _http("GET", f"{self.base}/api-docs")
        self.assertEqual(s, 200)

    def test_api_docs_page_loads_with_session(self):
        s, _, body = _http(
            "GET", f"{self.base}/api-docs",
            headers={"Cookie": f"shabd_sid={self.sess.sid}"})
        self.assertEqual(s, 200)
        self.assertIn("API Docs", body)


def main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for cls in (OpenApiSpecTests, LiveDocsTests):
        suite.addTests(loader.loadTestsFromTestCase(cls))
    return 0 if unittest.TextTestRunner(
        verbosity=2, stream=sys.stdout).run(suite).wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
