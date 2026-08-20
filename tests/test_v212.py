"""Tests for v2.12:

  * Native SHABD endpoints on the UI server (/manifest, POST /spells/<n>,
    /grimoire/verify, /grimoire/head) — token-authenticated, the
    integration / sharing surface.
  * UI-managed orchestrator intents: register → classify → route to a
    saved agent → run.
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

PORT_BASE = 23000


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


def _http(method, url, *, headers=None, body=None):
    req = urllib.request.Request(
        url, data=body, method=method, headers=dict(headers or {}))
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# Native endpoints
# ---------------------------------------------------------------------------


class NativeEndpointTests(unittest.TestCase):
    PORT = PORT_BASE + 1

    @classmethod
    def setUpClass(cls):
        os.environ["SHABD_UI_BOOTSTRAP_USER"] = "root"
        os.environ["SHABD_UI_BOOTSTRAP_PASSWORD"] = "rootpw"
        # require_auth=False: unscoped spells are open, scoped spells
        # still enforce their scope via the token's scope list.
        cls.app = _new_app("native", require_auth=False)

        @cls.app.spell
        def add(a: int, b: int) -> int:
            return a + b

        @cls.app.spell(scopes=["payments"])
        def pay(amount: int) -> dict:
            return {"ok": True, "amount": amount}

        cls.ui = UIServer(cls.app, bind="127.0.0.1", port=cls.PORT,
                           superusers=["root"])
        _start(cls.ui)
        cls.base = f"http://127.0.0.1:{cls.PORT}"

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("SHABD_UI_BOOTSTRAP_PASSWORD", None)

    def test_manifest_public(self):
        s, raw = _http("GET", f"{self.base}/manifest")
        self.assertEqual(s, 200)
        d = json.loads(raw)
        names = {sp["name"] for sp in d["spells"]}
        self.assertIn("add", names)
        self.assertIn("pay", names)

    def test_invoke_no_scope_needed(self):
        s, raw = _http(
            "POST", f"{self.base}/spells/add",
            headers={"Content-Type": "application/json"},
            body=b'{"a":7,"b":35}')
        self.assertEqual(s, 200)
        self.assertEqual(json.loads(raw)["result"], 42)

    def test_invoke_scoped_without_token_rejected(self):
        s, _ = _http(
            "POST", f"{self.base}/spells/pay",
            headers={"Content-Type": "application/json"},
            body=b'{"amount":100}')
        self.assertIn(s, (401, 403))

    def test_invoke_scoped_with_token(self):
        tok = self.app.issue_token("client", ["payments"], ttl=120)
        s, raw = _http(
            "POST", f"{self.base}/spells/pay",
            headers={"Content-Type": "application/json",
                      "Authorization": f"Bearer {tok}"},
            body=b'{"amount":100}')
        self.assertEqual(s, 200, raw)
        self.assertTrue(json.loads(raw)["result"]["ok"])

    def test_invoke_unknown_spell_404(self):
        s, raw = _http(
            "POST", f"{self.base}/spells/ghost",
            headers={"Content-Type": "application/json"},
            body=b"{}")
        self.assertEqual(s, 404)
        self.assertIn("spell_not_found", raw)

    def test_grimoire_verify_endpoint(self):
        s, raw = _http("GET", f"{self.base}/grimoire/verify")
        self.assertEqual(s, 200)
        self.assertIn("ok", json.loads(raw))

    def test_grimoire_head_endpoint(self):
        s, raw = _http("GET", f"{self.base}/grimoire/head")
        self.assertEqual(s, 200)
        self.assertIn("head", json.loads(raw))


# ---------------------------------------------------------------------------
# Orchestrator intents from the UI
# ---------------------------------------------------------------------------


class OrchestratorIntentTests(unittest.TestCase):
    def setUp(self):
        os.environ["SHABD_UI_BOOTSTRAP_USER"] = "root"
        os.environ["SHABD_UI_BOOTSTRAP_PASSWORD"] = "rootpw"
        self.app = _new_app()

        @self.app.spell
        def add_two_numbers(a: int, b: int) -> int:
            return a + b

        self.ui = UIServer(self.app, bind="127.0.0.1",
                            port=PORT_BASE + 10,
                            superusers=["root"])
        self.sess = self.ui._login("root", "rootpw")
        self.ui.save_agent(
            self.sess, name="arithmetic",
            system="You add numbers.", tools=["add_two_numbers"])

    def tearDown(self):
        os.environ.pop("SHABD_UI_BOOTSTRAP_PASSWORD", None)

    def test_no_intents_message(self):
        res = self.ui.classify_query("anything")
        self.assertIsNone(res["intent"])
        self.assertEqual(res["via"], "no_intents")

    def test_save_intent_requires_known_agent(self):
        with self.assertRaises(UIError) as ctx:
            self.ui.save_intent(
                self.sess, name="x", keywords=["a"],
                route_to="ghost-agent")
        self.assertEqual(ctx.exception.status, 404)

    def test_classify_routes_to_agent(self):
        self.ui.save_intent(
            self.sess, name="math",
            keywords=["add", "sum", "plus", "calculate"],
            description="Arithmetic operations",
            route_to="arithmetic")
        res = self.ui.classify_query("please add these numbers")
        self.assertEqual(res["intent"], "math")
        self.assertEqual(res["route_to"], "arithmetic")
        self.assertGreater(res["confidence"], 0)

    def test_classify_falls_back(self):
        self.ui.save_intent(
            self.sess, name="math", keywords=["add"],
            route_to="arithmetic")
        res = self.ui.classify_query("zzzz qqqq xxxx")
        self.assertEqual(res["intent"], "fallback")

    def test_route_and_run_executes_agent(self):
        self.ui.save_intent(
            self.sess, name="math",
            keywords=["add", "sum", "plus"],
            description="Arithmetic", route_to="arithmetic")
        res = self.ui.route_and_run(
            self.sess, query="add some numbers")
        self.assertEqual(res["intent"], "math")
        self.assertTrue(res["ran"])
        self.assertIn("result", res)

    def test_route_without_agent_does_not_run(self):
        self.ui.save_intent(
            self.sess, name="hr",
            keywords=["leave", "holiday"],
            description="HR", route_to="")
        res = self.ui.route_and_run(
            self.sess, query="I need leave")
        self.assertEqual(res["intent"], "hr")
        self.assertFalse(res["ran"])

    def test_delete_intent(self):
        self.ui.save_intent(
            self.sess, name="math", keywords=["add"],
            route_to="arithmetic")
        self.ui.delete_intent(self.sess, "math")
        self.assertNotIn("math", self.ui._intents)

    def test_intents_survive_restart(self):
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        audit = os.path.join(tmp.name, "a.jsonl")
        try:
            app1 = SHABD("p", secret="x" * 32, require_auth=False,
                          grimoire_log_path=audit)

            @app1.spell
            def add_two_numbers(a: int, b: int) -> int:
                return a + b

            ui1 = UIServer(app1, bind="127.0.0.1",
                            port=PORT_BASE + 11, superusers=["root"])
            sess1 = ui1._login("root", "rootpw")
            ui1.save_agent(
                sess1, name="arithmetic", system="add",
                tools=["add_two_numbers"])
            ui1.save_intent(
                sess1, name="math", keywords=["add"],
                route_to="arithmetic")
            # Restart
            app2 = SHABD("p", secret="x" * 32, require_auth=False,
                          grimoire_log_path=audit)
            ui2 = UIServer(app2, bind="127.0.0.1",
                            port=PORT_BASE + 12, superusers=["root"])
            self.assertIn("math", ui2._intents)
            self.assertEqual(
                ui2._intents["math"]["route_to"], "arithmetic")
        finally:
            tmp.cleanup()


def main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for cls in (NativeEndpointTests, OrchestratorIntentTests):
        suite.addTests(loader.loadTestsFromTestCase(cls))
    return 0 if unittest.TextTestRunner(
        verbosity=2, stream=sys.stdout).run(suite).wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
