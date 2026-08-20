"""Tests for the public /ask orchestrator endpoint — send a question,
the orchestrator routes it to the right agent and answers."""
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

PORT_BASE = 25000


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
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


class AskOrchestratorTests(unittest.TestCase):
    PORT = PORT_BASE + 1

    @classmethod
    def setUpClass(cls):
        os.environ["SHABD_UI_BOOTSTRAP_USER"] = "root"
        os.environ["SHABD_UI_BOOTSTRAP_PASSWORD"] = "rootpw"
        cls.app = _new_app("ask-ui")

        @cls.app.spell
        def add_two_numbers(a: int, b: int) -> int:
            return a + b

        @cls.app.spell
        def lookup_leave(emp: str) -> dict:
            return {"casual": 12}

        cls.ui = UIServer(cls.app, bind="127.0.0.1", port=cls.PORT,
                           superusers=["root"])
        _start(cls.ui)
        cls.sess = cls.ui._login("root", "rootpw")
        cls.ui.save_agent(
            cls.sess, name="math",
            system="You add numbers.", tools=["add_two_numbers"])
        cls.ui.save_agent(
            cls.sess, name="hrbot",
            system="HR helper.", tools=["lookup_leave"])
        cls.ui.save_intent(
            cls.sess, name="arithmetic",
            keywords=["add", "sum", "plus", "calculate"],
            description="Arithmetic operations", route_to="math")
        cls.ui.save_intent(
            cls.sess, name="hr",
            keywords=["leave", "holiday", "chuti"],
            description="HR — leave and attendance", route_to="hrbot")
        cls.base = f"http://127.0.0.1:{cls.PORT}"

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("SHABD_UI_BOOTSTRAP_PASSWORD", None)

    # ---- method level ----

    def test_ask_routes_to_math(self):
        res = self.ui.ask_orchestrator(question="please add these")
        self.assertTrue(res["ok"])
        self.assertEqual(res["intent"], "arithmetic")
        self.assertEqual(res["agent"], "math")

    def test_ask_routes_to_hr(self):
        res = self.ui.ask_orchestrator(question="I need leave tomorrow")
        self.assertTrue(res["ok"])
        self.assertEqual(res["intent"], "hr")
        self.assertEqual(res["agent"], "hrbot")

    def test_ask_no_match_fallback(self):
        res = self.ui.ask_orchestrator(question="zzz qqq xxx")
        # fallback intent has no agent → ok False with a message
        self.assertFalse(res["ok"])

    def test_ask_empty_question(self):
        with self.assertRaises(UIError) as ctx:
            self.ui.ask_orchestrator(question="")
        self.assertEqual(ctx.exception.status, 400)

    # ---- HTTP level ----

    def test_ask_http_routes(self):
        s, raw = _http(
            "POST", f"{self.base}/ask",
            headers={"Content-Type": "application/json"},
            body=b'{"question":"add two numbers"}')
        self.assertEqual(s, 200)
        d = json.loads(raw)
        self.assertTrue(d["ok"])
        self.assertEqual(d["agent"], "math")
        self.assertIn("answer", d)

    def test_ask_http_accepts_query_key(self):
        s, raw = _http(
            "POST", f"{self.base}/ask",
            headers={"Content-Type": "application/json"},
            body=b'{"query":"chuti chahiye"}')
        self.assertEqual(s, 200)
        self.assertEqual(json.loads(raw)["agent"], "hrbot")

    def test_ask_http_bad_token(self):
        s, _ = _http(
            "POST", f"{self.base}/ask",
            headers={"Content-Type": "application/json",
                      "Authorization": "Bearer garbage"},
            body=b'{"question":"add"}')
        self.assertEqual(s, 401)

    def test_ask_http_valid_token(self):
        tok = self.app.issue_token("caller", [], ttl=120)
        s, raw = _http(
            "POST", f"{self.base}/ask",
            headers={"Content-Type": "application/json",
                      "Authorization": f"Bearer {tok}"},
            body=b'{"question":"sum it"}')
        self.assertEqual(s, 200)
        self.assertTrue(json.loads(raw)["ok"])


def main() -> int:
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromTestCase(AskOrchestratorTests)
    return 0 if unittest.TextTestRunner(
        verbosity=2, stream=sys.stdout).run(suite).wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
