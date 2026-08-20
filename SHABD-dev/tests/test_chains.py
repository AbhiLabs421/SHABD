"""Tests for the UI Spell Chain builder — deterministic spell pipelines
created from the browser. Also covers the boot-recreation fix that
makes UI-built spells and chains survive a restart."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shabd import SHABD  # noqa: E402
from shabd_ui import UIError, UIServer  # noqa: E402

PORT_BASE = 27000


def _ui(app, port):
    os.environ["SHABD_UI_BOOTSTRAP_USER"] = "root"
    os.environ["SHABD_UI_BOOTSTRAP_PASSWORD"] = "rootpw"
    return UIServer(app, bind="127.0.0.1", port=port,
                     superusers=["root"])


class ChainMethodTests(unittest.TestCase):
    def setUp(self):
        self.app = SHABD("chain", secret="x" * 32, require_auth=False)
        self.ui = _ui(self.app, PORT_BASE + 1)
        self.sess = self.ui._login("root", "rootpw")
        self.ui.create_spell(
            self.sess, name="double",
            source="def double(n: int) -> dict:\n    return {'n': n*2}\n",
            description="double")
        self.ui.create_spell(
            self.sess, name="addten",
            source="def addten(n: int) -> int:\n    return n + 10\n",
            description="add 10")

    def tearDown(self):
        os.environ.pop("SHABD_UI_BOOTSTRAP_PASSWORD", None)

    def test_create_chain_registers_a_spell(self):
        self.ui.create_chain(
            self.sess, name="dbl_add",
            steps=["double", "addten"])
        self.assertIn("dbl_add", self.app._spells)

    def test_chain_pipes_output_to_next(self):
        self.ui.create_chain(
            self.sess, name="dbl_add",
            steps=["double", "addten"])
        # double(5) -> {n:10} -> addten(n=10) -> 20
        self.assertEqual(
            self.app.invoke("dbl_add", {"n": 5}), 20)

    def test_chain_needs_two_steps(self):
        with self.assertRaises(UIError) as ctx:
            self.ui.create_chain(
                self.sess, name="solo", steps=["double"])
        self.assertEqual(ctx.exception.status, 400)

    def test_chain_rejects_unknown_step(self):
        with self.assertRaises(UIError) as ctx:
            self.ui.create_chain(
                self.sess, name="bad",
                steps=["double", "ghost"])
        self.assertEqual(ctx.exception.status, 404)

    def test_chain_name_collision(self):
        self.ui.create_chain(
            self.sess, name="dbl_add",
            steps=["double", "addten"])
        with self.assertRaises(UIError) as ctx:
            self.ui.create_chain(
                self.sess, name="dbl_add",
                steps=["double", "addten"])
        self.assertEqual(ctx.exception.status, 409)

    def test_list_chains(self):
        self.ui.create_chain(
            self.sess, name="dbl_add",
            steps=["double", "addten"])
        chains = self.ui.list_chains()
        self.assertEqual(len(chains), 1)
        self.assertEqual(chains[0]["name"], "dbl_add")
        self.assertTrue(chains[0]["live"])

    def test_delete_chain(self):
        self.ui.create_chain(
            self.sess, name="dbl_add",
            steps=["double", "addten"])
        self.ui.delete_chain(self.sess, "dbl_add")
        self.assertNotIn("dbl_add", self.app._spells)
        self.assertEqual(self.ui.list_chains(), [])

    def test_three_step_chain(self):
        self.ui.create_spell(
            self.sess, name="square",
            source="def square(n: int) -> dict:\n    return {'n': n*n}\n",
            description="square")
        # double(2)->{n:4} -> square(n=4)->{n:16} -> addten(n=16)->26
        self.ui.create_chain(
            self.sess, name="d_sq_add",
            steps=["double", "square", "addten"])
        self.assertEqual(
            self.app.invoke("d_sq_add", {"n": 2}), 26)


class ChainPersistenceTests(unittest.TestCase):
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
        ui = UIServer(app, bind="127.0.0.1", port=port,
                       superusers=["root"])
        return app, ui

    def test_dynamic_spells_survive_restart(self):
        app1, ui1 = self._build(PORT_BASE + 10)
        sess = ui1._login("root", "rootpw")
        ui1.create_spell(
            sess, name="triple",
            source="def triple(n: int) -> int:\n    return n*3\n",
            description="x3")
        # Restart
        app2, _ = self._build(PORT_BASE + 11)
        self.assertIn("triple", app2._spells)
        self.assertEqual(app2.invoke("triple", {"n": 4}), 12)

    def test_chain_survives_restart(self):
        app1, ui1 = self._build(PORT_BASE + 12)
        sess = ui1._login("root", "rootpw")
        ui1.create_spell(
            sess, name="double",
            source="def double(n: int) -> dict:\n    return {'n': n*2}\n",
            description="double")
        ui1.create_spell(
            sess, name="addten",
            source="def addten(n: int) -> int:\n    return n+10\n",
            description="add 10")
        ui1.create_chain(
            sess, name="dbl_add", steps=["double", "addten"])
        self.assertEqual(app1.invoke("dbl_add", {"n": 5}), 20)
        # Restart — chain + its step spells must come back live
        app2, _ = self._build(PORT_BASE + 13)
        self.assertIn("dbl_add", app2._spells)
        self.assertEqual(app2.invoke("dbl_add", {"n": 9}), 28)


def main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for cls in (ChainMethodTests, ChainPersistenceTests):
        suite.addTests(loader.loadTestsFromTestCase(cls))
    return 0 if unittest.TextTestRunner(
        verbosity=2, stream=sys.stdout).run(suite).wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
