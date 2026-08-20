"""Tests for the optional FastAPI adapter (shabd_fastapi).

Skipped entirely if FastAPI isn't installed — the stdlib UI works
without it, so its absence must never fail the suite. When present, we
prove behaviour/accuracy is identical to the stdlib server: same
spells, same scope enforcement, same audit chain.
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shabd import SHABD  # noqa: E402

try:
    from fastapi.testclient import TestClient

    from shabd_fastapi import build_fastapi, have_fastapi
    _SKIP = not have_fastapi()
except Exception:
    _SKIP = True


def _app():
    app = SHABD("fa-test", secret="x" * 32, require_auth=False)

    @app.spell
    def add(a: int, b: int) -> int:
        return a + b

    @app.spell(scopes=["payments"])
    def pay(amount: int) -> dict:
        return {"ok": True, "amount": amount}

    return app


def _ui(app):
    os.environ["SHABD_UI_BOOTSTRAP_USER"] = "root"
    os.environ["SHABD_UI_BOOTSTRAP_PASSWORD"] = "rootpw"
    from shabd_ui import UIServer
    ui = UIServer(app, bind="127.0.0.1", port=27999,
                   superusers=["root"])
    sess = ui._login("root", "rootpw")
    ui.save_agent(sess, name="math", system="add", tools=["add"])
    ui.save_intent(sess, name="arith", keywords=["add", "sum"],
                   route_to="math")
    return ui


@unittest.skipIf(_SKIP, "fastapi not installed")
class FastApiAdapterTests(unittest.TestCase):
    def setUp(self):
        self.app = _app()
        self.client = TestClient(build_fastapi(self.app))

    def tearDown(self):
        os.environ.pop("SHABD_UI_BOOTSTRAP_PASSWORD", None)

    def test_healthz(self):
        r = self.client.get("/healthz")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])

    def test_manifest_lists_spells(self):
        r = self.client.get("/manifest")
        names = {s["name"] for s in r.json()["spells"]}
        self.assertEqual(names, {"add", "pay"})

    def test_invoke_open_spell(self):
        r = self.client.post("/spells/add", json={"a": 7, "b": 35})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["result"], 42)

    def test_scoped_spell_without_token_403(self):
        r = self.client.post("/spells/pay", json={"amount": 100})
        self.assertEqual(r.status_code, 403)

    def test_scoped_spell_with_token(self):
        tok = self.app.issue_token("bot", ["payments"], ttl=120)
        r = self.client.post(
            "/spells/pay", json={"amount": 100},
            headers={"Authorization": f"Bearer {tok}"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["result"]["ok"])

    def test_unknown_spell_404(self):
        r = self.client.post("/spells/ghost", json={})
        self.assertEqual(r.status_code, 404)

    def test_audit_chain_intact(self):
        self.client.post("/spells/add", json={"a": 1, "b": 2})
        r = self.client.get("/grimoire/verify")
        self.assertTrue(r.json()["ok"])

    def test_swagger_docs_served(self):
        self.assertEqual(self.client.get("/docs").status_code, 200)
        self.assertEqual(
            self.client.get("/openapi.json").status_code, 200)

    def test_result_matches_stdlib_invoke(self):
        # The whole point: FastAPI result == direct app.invoke result.
        direct = self.app.invoke("add", {"a": 4, "b": 5})
        via_api = self.client.post(
            "/spells/add", json={"a": 4, "b": 5}).json()["result"]
        self.assertEqual(direct, via_api)


@unittest.skipIf(_SKIP, "fastapi not installed")
class FastApiWithUiTests(unittest.TestCase):
    def setUp(self):
        self.app = _app()
        self.ui = _ui(self.app)
        self.client = TestClient(build_fastapi(self.app, self.ui))

    def tearDown(self):
        os.environ.pop("SHABD_UI_BOOTSTRAP_PASSWORD", None)

    def test_list_agents(self):
        r = self.client.get("/agents")
        self.assertIn("math", r.json()["agents"])

    def test_query_agent(self):
        r = self.client.post(
            "/query/math", json={"question": "add 2 and 3"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])

    def test_ask_routes(self):
        r = self.client.post(
            "/ask", json={"question": "add some numbers"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["agent"], "math")

    def test_query_bad_token_401(self):
        r = self.client.post(
            "/query/math", json={"question": "hi"},
            headers={"Authorization": "Bearer garbage"})
        self.assertEqual(r.status_code, 401)

    def test_ui_built_spell_appears_live(self):
        # Build a spell through the UI AFTER the FastAPI app exists.
        sess = self.ui._login("root", "rootpw")
        self.ui.create_spell(
            sess, name="triple",
            source="def triple(n: int) -> int:\n    return n * 3\n",
            description="x3")
        # Dynamic catch-all route picks it up with no restart.
        r = self.client.post("/spells/triple", json={"n": 5})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["result"], 15)


def main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for cls in (FastApiAdapterTests, FastApiWithUiTests):
        suite.addTests(loader.loadTestsFromTestCase(cls))
    return 0 if unittest.TextTestRunner(
        verbosity=2, stream=sys.stdout).run(suite).wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
