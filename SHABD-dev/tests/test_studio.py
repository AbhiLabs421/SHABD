"""Tests for the visual Chatbot Studio (shabd_studio) and the chatbot
backend it drives in UIServer."""
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
from shabd_studio import StudioServer  # noqa: E402
from shabd_ui import UIError, UIServer  # noqa: E402

PORT_BASE = 29000


def _ui(app, port):
    os.environ["SHABD_UI_BOOTSTRAP_USER"] = "root"
    os.environ["SHABD_UI_BOOTSTRAP_PASSWORD"] = "rootpw"
    return UIServer(app, bind="127.0.0.1", port=port,
                     superusers=["root"])


def _seed(ui, sess):
    ui.create_spell(
        sess, name="faq",
        source="def faq(topic: str) -> str:\n    return 'about ' + topic\n",
        description="faq")
    ui.save_agent(sess, name="helper", system="help", tools=["faq"])


def _start(srv):
    threading.Thread(target=srv.serve, daemon=True).start()
    for _ in range(80):
        try:
            with urllib.request.urlopen(
                    f"http://{srv.bind}:{srv.port}/healthz",
                    timeout=1) as r:
                if r.status == 200:
                    return
        except Exception:
            time.sleep(0.05)
    raise RuntimeError("server did not start")


def _http(method, url, *, headers=None, body=None):
    req = urllib.request.Request(
        url, data=body, method=method, headers=dict(headers or {}))
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status, dict(r.headers), r.read().decode(
                "utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read().decode(
            "utf-8", "replace")


class ChatbotBackendTests(unittest.TestCase):
    def setUp(self):
        self.app = SHABD("bot", secret="x" * 32, require_auth=False)
        self.ui = _ui(self.app, PORT_BASE + 1)
        self.sess = self.ui._login("root", "rootpw")
        _seed(self.ui, self.sess)

    def tearDown(self):
        os.environ.pop("SHABD_UI_BOOTSTRAP_PASSWORD", None)

    def test_save_and_get(self):
        self.ui.save_chatbot(
            self.sess, name="b1", system="hi",
            greeting="yo", tools=["faq"], agents=["helper"])
        b = self.ui.get_chatbot("b1")
        self.assertEqual(b["name"], "b1")
        self.assertEqual(b["tools"], ["faq"])
        self.assertEqual(b["agents"], ["helper"])

    def test_bad_name(self):
        with self.assertRaises(UIError) as ctx:
            self.ui.save_chatbot(self.sess, name="bad name!")
        self.assertEqual(ctx.exception.status, 400)

    def test_toolset_merges_agent_tools(self):
        # bot has no direct tools, but its agent 'helper' carries 'faq'
        self.ui.save_chatbot(
            self.sess, name="b2", agents=["helper"])
        ts = self.ui._chatbot_toolset(self.ui._chatbots["b2"])
        self.assertIn("faq", ts)

    def test_run_chatbot_audits_turn(self):
        self.ui.save_chatbot(
            self.sess, name="b3", tools=["faq"])
        before = len(self.app.grimoire.pages(limit=10_000))
        r = self.ui.run_chatbot(
            self.sess, name="b3", message="hello")
        self.assertTrue(r["ok"])
        pages = self.app.grimoire.pages(limit=10_000)
        self.assertEqual(len(pages), before + 1)
        self.assertEqual(pages[-1]["spell"], "__chat:b3")

    def test_run_unknown_bot(self):
        with self.assertRaises(UIError) as ctx:
            self.ui.run_chatbot(
                self.sess, name="ghost", message="hi")
        self.assertEqual(ctx.exception.status, 404)

    def test_run_empty_message(self):
        self.ui.save_chatbot(self.sess, name="b4", tools=["faq"])
        with self.assertRaises(UIError) as ctx:
            self.ui.run_chatbot(self.sess, name="b4", message="")
        self.assertEqual(ctx.exception.status, 400)

    def test_list_and_delete(self):
        self.ui.save_chatbot(self.sess, name="b5", tools=["faq"])
        self.assertTrue(
            any(b["name"] == "b5" for b in self.ui.list_chatbots()))
        self.ui.delete_chatbot(self.sess, "b5")
        self.assertIsNone(self.ui.get_chatbot("b5"))


class StudioServerTests(unittest.TestCase):
    PORT_UI = PORT_BASE + 10
    PORT_ST = PORT_BASE + 11

    @classmethod
    def setUpClass(cls):
        cls.app = SHABD("studio", secret="x" * 32, require_auth=False)
        cls.ui = _ui(cls.app, cls.PORT_UI)
        cls.sess = cls.ui._login("root", "rootpw")
        _seed(cls.ui, cls.sess)
        cls.ui.save_chatbot(
            cls.sess, name="sup", system="support",
            greeting="Hello!", tools=["faq"], agents=["helper"])
        cls.studio = StudioServer(
            cls.ui, bind="127.0.0.1", port=cls.PORT_ST)
        _start(cls.studio)
        cls.base = f"http://127.0.0.1:{cls.PORT_ST}"
        cls.cookie = f"shabd_sid={cls.sess.sid}"

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("SHABD_UI_BOOTSTRAP_PASSWORD", None)

    def test_healthz(self):
        s, _, _ = _http("GET", f"{self.base}/healthz")
        self.assertEqual(s, 200)

    def test_builder_requires_session(self):
        s, _, body = _http("GET", f"{self.base}/")
        self.assertEqual(s, 200)
        self.assertIn("sign in", body.lower())

    def test_builder_loads_with_session(self):
        s, _, body = _http(
            "GET", f"{self.base}/",
            headers={"Cookie": self.cookie})
        self.assertEqual(s, 200)
        self.assertIn("SHABD Studio", body)

    def test_palette(self):
        s, _, raw = _http(
            "GET", f"{self.base}/api/palette",
            headers={"Cookie": self.cookie})
        self.assertEqual(s, 200)
        p = json.loads(raw)
        self.assertIn("faq", p["tools"])
        self.assertIn("helper", p["agents"])
        self.assertTrue(any(b["name"] == "sup" for b in p["bots"]))

    def test_save_via_studio(self):
        body = json.dumps({
            "name": "newbot", "system": "s", "greeting": "hi",
            "tools": ["faq"], "agents": [],
            "graph": {"nodes": []},
        }).encode()
        s, _, raw = _http(
            "POST", f"{self.base}/api/chatbots/save",
            headers={"Cookie": self.cookie,
                      "X-CSRF": self.sess.csrf,
                      "Content-Type": "application/json"},
            body=body)
        self.assertEqual(s, 200)
        self.assertTrue(json.loads(raw)["ok"])

    def test_public_chat_endpoint(self):
        s, _, raw = _http(
            "POST", f"{self.base}/chat/sup",
            headers={"Content-Type": "application/json"},
            body=b'{"message":"hi"}')
        self.assertEqual(s, 200)
        self.assertTrue(json.loads(raw)["ok"])

    def test_chat_unknown_bot(self):
        s, _, _ = _http(
            "POST", f"{self.base}/chat/ghost",
            headers={"Content-Type": "application/json"},
            body=b'{"message":"hi"}')
        self.assertEqual(s, 404)

    def test_chat_bad_token(self):
        s, _, _ = _http(
            "POST", f"{self.base}/chat/sup",
            headers={"Content-Type": "application/json",
                      "Authorization": "Bearer garbage"},
            body=b'{"message":"hi"}')
        self.assertEqual(s, 401)

    def test_hosted_page(self):
        s, _, body = _http("GET", f"{self.base}/c/sup")
        self.assertEqual(s, 200)
        self.assertIn("sup", body)
        self.assertIn("Hello!", body)

    def test_embed_script(self):
        s, headers, body = _http(
            "GET", f"{self.base}/embed/sup.js")
        self.assertEqual(s, 200)
        self.assertIn("javascript", headers.get("Content-Type", ""))
        self.assertIn("shabd-bub", body)

    def test_embed_unknown_bot(self):
        s, _, _ = _http("GET", f"{self.base}/embed/ghost.js")
        self.assertEqual(s, 404)


def main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for cls in (ChatbotBackendTests, StudioServerTests):
        suite.addTests(loader.loadTestsFromTestCase(cls))
    return 0 if unittest.TextTestRunner(
        verbosity=2, stream=sys.stdout).run(suite).wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
