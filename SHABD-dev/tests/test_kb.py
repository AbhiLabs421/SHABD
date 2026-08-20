"""Tests for the Knowledge Base (document RAG) feature in shabd_ui."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shabd import SHABD  # noqa: E402
from shabd_ui import UIError, UIServer  # noqa: E402

PORT_BASE = 30500

DOC = """Leave Policy

Every full-time employee is entitled to 12 casual leaves per year.
Sick leave is capped at 5 days annually.
Earned leave accrues at 1.75 days per month.

Attendance

Employees must mark attendance by 9:30 AM.
Work from home is allowed 2 days per week with approval."""


def _ui(app, port):
    os.environ["SHABD_UI_BOOTSTRAP_USER"] = "root"
    os.environ["SHABD_UI_BOOTSTRAP_PASSWORD"] = "rootpw"
    return UIServer(app, bind="127.0.0.1", port=port,
                     superusers=["root"])


class KbBackendTests(unittest.TestCase):
    def setUp(self):
        self.app = SHABD("kb", secret="x" * 32, require_auth=False)
        self.ui = _ui(self.app, PORT_BASE + 1)
        self.sess = self.ui._login("root", "rootpw")

    def tearDown(self):
        os.environ.pop("SHABD_UI_BOOTSTRAP_PASSWORD", None)

    def test_create_and_list(self):
        self.ui.create_kb(self.sess, name="hr", description="hr")
        kbs = self.ui.list_kbs()
        self.assertEqual(len(kbs), 1)
        self.assertEqual(kbs[0]["name"], "hr")
        self.assertFalse(kbs[0]["exposed"])

    def test_create_bad_name(self):
        with self.assertRaises(UIError) as ctx:
            self.ui.create_kb(self.sess, name="bad name!")
        self.assertEqual(ctx.exception.status, 400)

    def test_duplicate(self):
        self.ui.create_kb(self.sess, name="hr")
        with self.assertRaises(UIError) as ctx:
            self.ui.create_kb(self.sess, name="hr")
        self.assertEqual(ctx.exception.status, 409)

    def test_add_text_chunks(self):
        self.ui.create_kb(self.sess, name="hr")
        r = self.ui.add_kb_text(
            self.sess, name="hr", text=DOC, source="policy.txt")
        self.assertTrue(r["ok"])
        self.assertGreaterEqual(r["chunks_added"], 1)

    def test_add_to_missing_kb(self):
        with self.assertRaises(UIError) as ctx:
            self.ui.add_kb_text(self.sess, name="ghost", text="hi")
        self.assertEqual(ctx.exception.status, 404)

    def test_retrieval_finds_relevant(self):
        self.ui.create_kb(self.sess, name="hr")
        # add several distinct chunks so ranking is meaningful
        self.ui.add_kb_text(
            self.sess, name="hr",
            text="Casual leaves: 12 per year for full-time staff.",
            source="a")
        self.ui.add_kb_text(
            self.sess, name="hr",
            text="The office cafeteria serves lunch at noon daily.",
            source="b")
        self.ui.add_kb_text(
            self.sess, name="hr",
            text="Sick leave is capped at 5 days annually.",
            source="c")
        hits = self.ui.query_kb("hr", "how many casual leaves", top_k=1)
        self.assertTrue(hits)
        self.assertIn("casual", hits[0]["text"].lower())

    def test_expose_registers_spell(self):
        self.ui.create_kb(self.sess, name="hr")
        self.ui.add_kb_text(self.sess, name="hr", text=DOC)
        self.ui.expose_kb(self.sess, "hr")
        self.assertIn("kb_hr", self.app._spells)

    def test_exposed_spell_answers(self):
        self.ui.create_kb(self.sess, name="hr")
        self.ui.add_kb_text(self.sess, name="hr", text=DOC)
        self.ui.expose_kb(self.sess, "hr")
        out = self.app.invoke("kb_hr", {"question": "casual leaves"})
        self.assertIn("answer", out)
        self.assertIn("sources", out)

    def test_exposed_spell_in_manifest(self):
        self.ui.create_kb(self.sess, name="hr")
        self.ui.add_kb_text(self.sess, name="hr", text=DOC)
        self.ui.expose_kb(self.sess, "hr")
        names = {s["name"] for s in self.app.manifest()["spells"]}
        self.assertIn("kb_hr", names)

    def test_delete_removes_spell(self):
        self.ui.create_kb(self.sess, name="hr")
        self.ui.add_kb_text(self.sess, name="hr", text=DOC)
        self.ui.expose_kb(self.sess, "hr")
        self.ui.delete_kb(self.sess, "hr")
        self.assertNotIn("kb_hr", self.app._spells)
        self.assertEqual(self.ui.list_kbs(), [])

    def test_query_empty_kb(self):
        self.ui.create_kb(self.sess, name="empty")
        self.assertEqual(
            self.ui.query_kb("empty", "anything"), [])


class KbPersistenceTests(unittest.TestCase):
    def setUp(self):
        os.environ["SHABD_UI_BOOTSTRAP_USER"] = "root"
        os.environ["SHABD_UI_BOOTSTRAP_PASSWORD"] = "rootpw"
        self.tmp = tempfile.TemporaryDirectory()
        self.audit = os.path.join(self.tmp.name, "a.jsonl")

    def tearDown(self):
        os.environ.pop("SHABD_UI_BOOTSTRAP_PASSWORD", None)
        self.tmp.cleanup()

    def _build(self, port):
        app = SHABD("p", secret="x" * 32, require_auth=False,
                     grimoire_log_path=self.audit)
        return app, UIServer(app, bind="127.0.0.1", port=port,
                              superusers=["root"])

    def test_kb_and_spell_survive_restart(self):
        app1, ui1 = self._build(PORT_BASE + 10)
        sess = ui1._login("root", "rootpw")
        ui1.create_kb(sess, name="hr")
        ui1.add_kb_text(sess, name="hr", text=DOC)
        ui1.expose_kb(sess, "hr")
        self.assertIn("kb_hr", app1._spells)
        # restart
        app2, ui2 = self._build(PORT_BASE + 11)
        self.assertIn("hr", ui2._kbs)
        # the exposed spell must be live again after boot
        self.assertIn("kb_hr", app2._spells)
        out = app2.invoke("kb_hr", {"question": "casual leaves"})
        self.assertIn("answer", out)


def main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for cls in (KbBackendTests, KbPersistenceTests):
        suite.addTests(loader.loadTestsFromTestCase(cls))
    return 0 if unittest.TextTestRunner(
        verbosity=2, stream=sys.stdout).run(suite).wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
