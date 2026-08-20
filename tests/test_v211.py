"""Tests for v2.11 UI additions:

  * AI suggestion endpoint     — falls back gracefully when no LLM is set.
  * Share / Import spell       — round-trip a UI-built spell as a string.
  * Project export / import    — zip of every spell + agent + state.
  * Spell versioning           — last N versions kept, rollback works.
  * Token revocation list      — revoked JTIs rejected by app.tokens.verify.
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shabd import SHABD  # noqa: E402
from shabd_ui import UIError, UIServer  # noqa: E402

PORT_BASE = 22000


def _new_app(name="t", *, audit_path: str | None = None,
              require_auth: bool = False) -> SHABD:
    kwargs = {"secret": "x" * 32, "require_auth": require_auth}
    if audit_path:
        kwargs["grimoire_log_path"] = audit_path
    return SHABD(name, **kwargs)


def _new_ui(app: SHABD, port: int = PORT_BASE) -> UIServer:
    return UIServer(app, bind="127.0.0.1", port=port,
                     superusers=["root"])


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
    raise RuntimeError(f"UI on :{ui.port} did not start")


# ---------------------------------------------------------------------------
# AI suggestion endpoint — fallback path
# ---------------------------------------------------------------------------


class AISuggestionFallbackTests(unittest.TestCase):
    def setUp(self):
        os.environ["SHABD_UI_BOOTSTRAP_USER"] = "root"
        os.environ["SHABD_UI_BOOTSTRAP_PASSWORD"] = "rootpw"
        self.app = _new_app()
        self.ui = _new_ui(self.app, port=PORT_BASE + 1)
        self.sess = self.ui._login("root", "rootpw")

    def tearDown(self):
        os.environ.pop("SHABD_UI_BOOTSTRAP_PASSWORD", None)

    def test_no_llm_returns_skeleton(self):
        res = self.ui.suggest_spell_source(
            self.sess,
            requirement="add two integers",
            name_hint="add")
        self.assertEqual(res["via"], "fallback")
        self.assertIn("def add(", res["source"])
        self.assertIn("add two integers", res["source"])

    def test_empty_requirement_rejected(self):
        with self.assertRaises(UIError) as ctx:
            self.ui.suggest_spell_source(
                self.sess, requirement="", name_hint="x")
        self.assertEqual(ctx.exception.status, 400)


# ---------------------------------------------------------------------------
# Share / Import
# ---------------------------------------------------------------------------


class ShareSpellTests(unittest.TestCase):
    def setUp(self):
        os.environ["SHABD_UI_BOOTSTRAP_USER"] = "root"
        os.environ["SHABD_UI_BOOTSTRAP_PASSWORD"] = "rootpw"
        self.app = _new_app()
        self.ui = _new_ui(self.app, port=PORT_BASE + 10)
        self.sess = self.ui._login("root", "rootpw")
        self.ui.create_spell(
            self.sess, name="hello",
            source="def hello(n: str) -> str:\n    return 'hi ' + n\n",
            description="say hi", scopes=[], tags=["demo"])

    def tearDown(self):
        os.environ.pop("SHABD_UI_BOOTSTRAP_PASSWORD", None)

    def test_share_returns_decodable_string(self):
        res = self.ui.share_spell(self.sess, "hello")
        self.assertTrue(res["share"].startswith("shabd-spell-v1:"))
        self.assertEqual(res["name"], "hello")

    def test_share_refuses_code_spell(self):
        @self.app.spell
        def code_built(x: int) -> int:
            return x

        with self.assertRaises(UIError) as ctx:
            self.ui.share_spell(self.sess, "code_built")
        self.assertEqual(ctx.exception.status, 403)

    def test_share_unknown_spell_404(self):
        with self.assertRaises(UIError) as ctx:
            self.ui.share_spell(self.sess, "ghost")
        self.assertEqual(ctx.exception.status, 404)

    def test_round_trip_via_second_server(self):
        # Server A shares
        share = self.ui.share_spell(self.sess, "hello")["share"]
        # Server B imports
        app_b = _new_app("recipient")
        ui_b = _new_ui(app_b, port=PORT_BASE + 11)
        sess_b = ui_b._login("root", "rootpw")
        ui_b.import_shared_spell(sess_b, share=share)
        # The spell now lives on B
        self.assertIn("hello", app_b._spells)
        self.assertEqual(
            app_b.invoke("hello", {"n": "amit"}), "hi amit")
        # Description was preserved through the share string
        self.assertEqual(
            app_b._spells["hello"].description, "say hi")

    def test_import_rejects_duplicate_without_overwrite(self):
        share = self.ui.share_spell(self.sess, "hello")["share"]
        # Try to import on the same server — spell already exists
        with self.assertRaises(UIError) as ctx:
            self.ui.import_shared_spell(self.sess, share=share)
        self.assertEqual(ctx.exception.status, 409)

    def test_import_overwrite_updates(self):
        # Edit source A then share, import with overwrite on the same
        # server — should replace.
        old_src = self.ui._dynamic_spells["hello"]["source"]
        new_src = "def hello(n: str) -> str:\n    return 'NAMASTE ' + n\n"
        self.ui.update_spell_source(
            self.sess, name="hello", source=new_src,
            description="namaste", scopes=[])
        share = self.ui.share_spell(self.sess, "hello")["share"]
        # Reset source A back to old (simulating "merge a colleague's
        # version that took the namaste spelling")
        self.ui.update_spell_source(
            self.sess, name="hello", source=old_src,
            description="say hi", scopes=[])
        # Now apply the colleague's version
        res = self.ui.import_shared_spell(
            self.sess, share=share, overwrite=True)
        self.assertEqual(res["mode"], "updated")
        self.assertEqual(
            self.app.invoke("hello", {"n": "amit"}), "NAMASTE amit")

    def test_invalid_share_string_rejected(self):
        for bad in ("", "garbage", "shabd-spell-v1:not-base64!!",
                     "shabd-spell-v1:" + "AAAA"):
            with self.assertRaises(UIError):
                self.ui.import_shared_spell(
                    self.sess, share=bad)


# ---------------------------------------------------------------------------
# Project export / import
# ---------------------------------------------------------------------------


class ProjectExportImportTests(unittest.TestCase):
    def setUp(self):
        os.environ["SHABD_UI_BOOTSTRAP_USER"] = "root"
        os.environ["SHABD_UI_BOOTSTRAP_PASSWORD"] = "rootpw"
        self.tmp = tempfile.TemporaryDirectory()
        self.audit_a = os.path.join(self.tmp.name, "a.jsonl")
        self.audit_b = os.path.join(self.tmp.name, "b.jsonl")
        self.app_a = _new_app("a", audit_path=self.audit_a)
        self.ui_a = _new_ui(self.app_a, port=PORT_BASE + 20)
        self.sess_a = self.ui_a._login("root", "rootpw")
        # Populate A with some content (no scopes for easy invoke later)
        self.ui_a.create_spell(
            self.sess_a, name="foo",
            source="def foo() -> int:\n    return 42\n")
        self.ui_a.create_spell(
            self.sess_a, name="bar",
            source="def bar(x: int) -> int:\n    return x*2\n")
        self.ui_a.save_agent(
            self.sess_a, name="adder",
            system="Add numbers.", tools=["foo", "bar"])
        self.ui_a.set_llm_config(
            self.sess_a, backend="ollama",
            base_url="http://localhost:11434",
            model="llama3.1:8b", api_key="my-secret")

    def tearDown(self):
        os.environ.pop("SHABD_UI_BOOTSTRAP_PASSWORD", None)
        self.tmp.cleanup()

    def test_export_returns_valid_zip(self):
        data = self.ui_a.export_project_zip(self.sess_a)
        zf = zipfile.ZipFile(io.BytesIO(data))
        names = set(zf.namelist())
        for needed in ("my_spells.py", "agents.json",
                        "llm_config.json", "audit.jsonl",
                        "state.json", "run.sh", "README.txt"):
            self.assertIn(needed, names, f"missing {needed}")

    def test_export_redacts_api_key(self):
        data = self.ui_a.export_project_zip(self.sess_a)
        zf = zipfile.ZipFile(io.BytesIO(data))
        cfg = json.loads(zf.read("llm_config.json"))
        self.assertNotEqual(cfg["api_key"], "my-secret")

    def test_my_spells_py_contains_each_spell(self):
        data = self.ui_a.export_project_zip(self.sess_a)
        zf = zipfile.ZipFile(io.BytesIO(data))
        src = zf.read("my_spells.py").decode()
        self.assertIn("def foo", src)
        self.assertIn("def bar", src)
        # And the decorator wires through app.spell()
        self.assertIn("@app.spell", src)

    def test_round_trip_into_second_server(self):
        data = self.ui_a.export_project_zip(self.sess_a)
        # Fresh empty server B
        app_b = _new_app("b", audit_path=self.audit_b)
        ui_b = _new_ui(app_b, port=PORT_BASE + 21)
        sess_b = ui_b._login("root", "rootpw")
        res = ui_b.import_project_zip(sess_b, data, overwrite=False)
        self.assertTrue(res["ok"])
        self.assertIn("foo", res["imported"])
        self.assertIn("bar", res["imported"])
        # Both spells should be invokable on B
        self.assertEqual(app_b.invoke("foo", {}), 42)
        self.assertEqual(app_b.invoke("bar", {"x": 5}), 10)
        # The agent rode along too
        self.assertIn("adder", ui_b._agents)


# ---------------------------------------------------------------------------
# Spell versioning
# ---------------------------------------------------------------------------


class SpellVersioningTests(unittest.TestCase):
    def setUp(self):
        os.environ["SHABD_UI_BOOTSTRAP_USER"] = "root"
        os.environ["SHABD_UI_BOOTSTRAP_PASSWORD"] = "rootpw"
        self.app = _new_app()
        self.ui = _new_ui(self.app, port=PORT_BASE + 30)
        self.sess = self.ui._login("root", "rootpw")
        self.ui.create_spell(
            self.sess, name="v",
            source="def v() -> int:\n    return 1\n")

    def tearDown(self):
        os.environ.pop("SHABD_UI_BOOTSTRAP_PASSWORD", None)

    def test_initial_has_one_version(self):
        vers = self.ui.list_spell_versions("v")
        self.assertEqual(len(vers), 1)
        self.assertTrue(vers[0]["current"])

    def test_update_grows_version_list(self):
        for i in range(2, 6):
            self.ui.update_spell_source(
                self.sess, name="v",
                source=f"def v() -> int:\n    return {i}\n")
        vers = self.ui.list_spell_versions("v")
        # 1 current + 4 prior
        self.assertEqual(len(vers), 5)
        self.assertTrue(vers[0]["current"])
        self.assertFalse(vers[-1]["current"])

    def test_keeps_at_most_ten_versions(self):
        for i in range(2, 20):
            self.ui.update_spell_source(
                self.sess, name="v",
                source=f"def v() -> int:\n    return {i}\n")
        vers = self.ui.list_spell_versions("v")
        # 1 current + at most 10 prior
        self.assertLessEqual(len(vers), 11)

    def test_rollback_restores_old_behaviour(self):
        # version 1 returns 1
        self.assertEqual(self.app.invoke("v", {}), 1)
        original_hash = self.ui._dynamic_spells["v"]["hash"]
        # Update to v=2
        self.ui.update_spell_source(
            self.sess, name="v",
            source="def v() -> int:\n    return 2\n")
        self.assertEqual(self.app.invoke("v", {}), 2)
        # Rollback to original
        self.ui.rollback_spell(
            self.sess, name="v", target_hash=original_hash)
        self.assertEqual(self.app.invoke("v", {}), 1)

    def test_rollback_unknown_hash_404(self):
        with self.assertRaises(UIError) as ctx:
            self.ui.rollback_spell(
                self.sess, name="v", target_hash="bogus" * 8)
        self.assertEqual(ctx.exception.status, 404)


# ---------------------------------------------------------------------------
# Token revocation
# ---------------------------------------------------------------------------


class TokenRevocationTests(unittest.TestCase):
    def setUp(self):
        os.environ["SHABD_UI_BOOTSTRAP_USER"] = "root"
        os.environ["SHABD_UI_BOOTSTRAP_PASSWORD"] = "rootpw"
        self.app = _new_app(require_auth=True)
        self.ui = _new_ui(self.app, port=PORT_BASE + 40)
        self.sess = self.ui._login("root", "rootpw")

    def tearDown(self):
        os.environ.pop("SHABD_UI_BOOTSTRAP_PASSWORD", None)

    def test_issue_recorded_in_list(self):
        res = self.ui.issue_token(
            self.sess, subject="bot", scopes=["x"], ttl=600)
        tokens = self.ui.list_issued_tokens()
        self.assertEqual(len(tokens), 1)
        self.assertEqual(tokens[0]["subject"], "bot")
        self.assertFalse(tokens[0]["revoked"])
        _ = res  # token itself

    def test_revoke_marks_as_revoked(self):
        self.ui.issue_token(
            self.sess, subject="bot", scopes=["x"], ttl=600)
        jti = list(self.ui._issued_tokens.keys())[0]
        self.ui.revoke_token(self.sess, jti)
        self.assertIn(jti, self.ui._revoked_jtis)
        listed = self.ui.list_issued_tokens()[0]
        self.assertTrue(listed["revoked"])

    def test_revoked_token_fails_verify(self):
        # NOTE: SHABD tokens have built-in replay protection — once
        # verified, the JTI is recorded and a second verify is
        # rejected as a replay. To prove the revocation path, we
        # issue → revoke → verify (no pre-verify).
        res = self.ui.issue_token(
            self.sess, subject="bot", scopes=["x"], ttl=600)
        tok = res["token"]
        jti = list(self.ui._issued_tokens.keys())[0]
        self.ui.revoke_token(self.sess, jti)
        from shabd import AuthError
        with self.assertRaises(AuthError) as ctx:
            self.app.tokens.verify(tok)
        self.assertIn("revoked", str(ctx.exception))

    def test_revoking_unknown_jti_404(self):
        with self.assertRaises(UIError) as ctx:
            self.ui.revoke_token(self.sess, "no-such-jti")
        self.assertEqual(ctx.exception.status, 404)

    def test_revoke_persists_across_restart(self):
        # tempfile-backed audit gives us a state.json sidecar
        tmp = tempfile.TemporaryDirectory()
        audit = os.path.join(tmp.name, "a.jsonl")
        try:
            app1 = _new_app("p", audit_path=audit, require_auth=True)
            ui1 = _new_ui(app1, port=PORT_BASE + 41)
            sess1 = ui1._login("root", "rootpw")
            res = ui1.issue_token(
                sess1, subject="bot", scopes=["x"], ttl=600)
            tok = res["token"]
            jti = list(ui1._issued_tokens.keys())[0]
            ui1.revoke_token(sess1, jti)
            # Restart
            app2 = _new_app("p", audit_path=audit, require_auth=True)
            ui2 = _new_ui(app2, port=PORT_BASE + 42)
            from shabd import AuthError
            with self.assertRaises(AuthError):
                app2.tokens.verify(tok)
            _ = ui2  # silence
        finally:
            tmp.cleanup()


def main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for cls in (AISuggestionFallbackTests, ShareSpellTests,
                ProjectExportImportTests, SpellVersioningTests,
                TokenRevocationTests):
        suite.addTests(loader.loadTestsFromTestCase(cls))
    return 0 if unittest.TextTestRunner(
        verbosity=2, stream=sys.stdout).run(suite).wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
