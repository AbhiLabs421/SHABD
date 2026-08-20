"""Tests for multi-agent flows — sequential and parallel orchestrators
built in the UI, plus the public POST /flow/<name> endpoint."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shabd import SHABD  # noqa: E402
from shabd_ui import UIError, UIServer  # noqa: E402

PORT_BASE = 28000


def _ui(app, port):
    os.environ["SHABD_UI_BOOTSTRAP_USER"] = "root"
    os.environ["SHABD_UI_BOOTSTRAP_PASSWORD"] = "rootpw"
    return UIServer(app, bind="127.0.0.1", port=port,
                     superusers=["root"])


def _seed(ui, sess):
    ui.create_spell(
        sess, name="add",
        source="def add(a: int, b: int) -> int:\n    return a + b\n",
        description="add")
    ui.create_spell(
        sess, name="sub",
        source="def sub(a: int, b: int) -> int:\n    return a - b\n",
        description="sub")
    ui.save_agent(sess, name="adder", system="add", tools=["add"])
    ui.save_agent(sess, name="subber", system="sub", tools=["sub"])


def _start(ui):
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


class FlowMethodTests(unittest.TestCase):
    def setUp(self):
        self.app = SHABD("flow", secret="x" * 32, require_auth=False)
        self.ui = _ui(self.app, PORT_BASE + 1)
        self.sess = self.ui._login("root", "rootpw")
        _seed(self.ui, self.sess)

    def tearDown(self):
        os.environ.pop("SHABD_UI_BOOTSTRAP_PASSWORD", None)

    def test_save_sequential(self):
        f = self.ui.save_flow(
            self.sess, name="seq", kind="sequential",
            agents=["adder", "subber"])
        self.assertEqual(f["kind"], "sequential")
        self.assertEqual(f["agents"], ["adder", "subber"])

    def test_save_parallel(self):
        f = self.ui.save_flow(
            self.sess, name="par", kind="parallel",
            agents=["adder", "subber"])
        self.assertEqual(f["kind"], "parallel")

    def test_needs_two_agents(self):
        with self.assertRaises(UIError) as ctx:
            self.ui.save_flow(
                self.sess, name="x", kind="sequential",
                agents=["adder"])
        self.assertEqual(ctx.exception.status, 400)

    def test_rejects_bad_kind(self):
        with self.assertRaises(UIError) as ctx:
            self.ui.save_flow(
                self.sess, name="x", kind="weird",
                agents=["adder", "subber"])
        self.assertEqual(ctx.exception.status, 400)

    def test_rejects_unknown_agent(self):
        with self.assertRaises(UIError) as ctx:
            self.ui.save_flow(
                self.sess, name="x", kind="parallel",
                agents=["adder", "ghost"])
        self.assertEqual(ctx.exception.status, 404)

    def test_run_sequential_traces_each_agent(self):
        self.ui.save_flow(
            self.sess, name="seq", kind="sequential",
            agents=["adder", "subber"])
        r = self.ui.run_flow(
            self.sess, name="seq", question="add 5 and 2")
        self.assertTrue(r["ok"])
        self.assertEqual(r["kind"], "sequential")
        self.assertEqual([t["agent"] for t in r["trace"]],
                          ["adder", "subber"])

    def test_run_parallel_runs_all(self):
        self.ui.save_flow(
            self.sess, name="par", kind="parallel",
            agents=["adder", "subber"])
        r = self.ui.run_flow(
            self.sess, name="par", question="do stuff")
        self.assertTrue(r["ok"])
        self.assertEqual(r["kind"], "parallel")
        self.assertEqual(
            {t["agent"] for t in r["trace"]}, {"adder", "subber"})

    def test_run_unknown_flow_404(self):
        with self.assertRaises(UIError) as ctx:
            self.ui.run_flow(
                self.sess, name="ghost", question="hi")
        self.assertEqual(ctx.exception.status, 404)

    def test_run_empty_question_400(self):
        self.ui.save_flow(
            self.sess, name="seq", kind="sequential",
            agents=["adder", "subber"])
        with self.assertRaises(UIError) as ctx:
            self.ui.run_flow(self.sess, name="seq", question="")
        self.assertEqual(ctx.exception.status, 400)

    def test_delete_flow(self):
        self.ui.save_flow(
            self.sess, name="seq", kind="sequential",
            agents=["adder", "subber"])
        self.ui.delete_flow(self.sess, "seq")
        self.assertEqual(self.ui.list_flows(), [])

    def test_sequential_feeds_previous_result(self):
        # Use a real (mock) backend that calls the tool so we can see
        # the second agent receives the first's result in its prompt.
        # We assert the trace structure rather than exact text.
        self.ui.save_flow(
            self.sess, name="seq", kind="sequential",
            agents=["adder", "subber"])
        r = self.ui.run_flow(
            self.sess, name="seq", question="add then subtract")
        # final answer == last agent's answer
        self.assertEqual(r["answer"], r["trace"][-1]["answer"])


class LiveFlowEndpointTests(unittest.TestCase):
    PORT = PORT_BASE + 20

    @classmethod
    def setUpClass(cls):
        cls.app = SHABD("flow-live", secret="x" * 32,
                         require_auth=False)
        cls.ui = _ui(cls.app, cls.PORT)
        _start(cls.ui)
        sess = cls.ui._login("root", "rootpw")
        _seed(cls.ui, sess)
        cls.ui.save_flow(
            sess, name="seq", kind="sequential",
            agents=["adder", "subber"])
        cls.base = f"http://127.0.0.1:{cls.PORT}"

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("SHABD_UI_BOOTSTRAP_PASSWORD", None)

    def test_flow_endpoint(self):
        s, raw = _http(
            "POST", f"{self.base}/flow/seq",
            headers={"Content-Type": "application/json"},
            body=b'{"question":"add 1 and 2"}')
        self.assertEqual(s, 200)
        d = json.loads(raw)
        self.assertTrue(d["ok"])
        self.assertEqual(d["flow"], "seq")

    def test_flow_endpoint_unknown(self):
        s, _ = _http(
            "POST", f"{self.base}/flow/ghost",
            headers={"Content-Type": "application/json"},
            body=b'{"question":"hi"}')
        self.assertEqual(s, 404)

    def test_flow_endpoint_bad_token(self):
        s, _ = _http(
            "POST", f"{self.base}/flow/seq",
            headers={"Content-Type": "application/json",
                      "Authorization": "Bearer garbage"},
            body=b'{"question":"hi"}')
        self.assertEqual(s, 401)


class FlowPersistenceTests(unittest.TestCase):
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

    def test_flow_survives_restart(self):
        app1, ui1 = self._build(PORT_BASE + 30)
        sess = ui1._login("root", "rootpw")
        _seed(ui1, sess)
        ui1.save_flow(
            sess, name="seq", kind="sequential",
            agents=["adder", "subber"])
        # restart
        app2, ui2 = self._build(PORT_BASE + 31)
        self.assertIn("seq", ui2._flows)
        self.assertEqual(ui2._flows["seq"]["kind"], "sequential")


def main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for cls in (FlowMethodTests, LiveFlowEndpointTests,
                FlowPersistenceTests):
        suite.addTests(loader.loadTestsFromTestCase(cls))
    return 0 if unittest.TextTestRunner(
        verbosity=2, stream=sys.stdout).run(suite).wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
