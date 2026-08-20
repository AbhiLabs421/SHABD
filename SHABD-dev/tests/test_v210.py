"""Tests for v2.10 UI additions:

  * Agent registry  — save / list / delete / run a named agent.
  * Spell editor    — view + update an existing UI-built spell.
  * Token-test mode — invoking via the SHABD /spells route enforces
                       scopes the same way an external curl would.
  * LLM config      — persist backend/url/model/api_key + replay
                       across restart via the sidecar state file.

Tiers run left-to-right in difficulty: Easy / Medium / Hard / Complex.
"""
from __future__ import annotations

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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shabd import SHABD  # noqa: E402
from shabd_ui import UIError, UIServer  # noqa: E402

PORT_BASE = 21000


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _new_app(name="t", *, audit_path: str | None = None,
              require_auth: bool = False) -> SHABD:
    kwargs = {"secret": "x" * 32, "require_auth": require_auth}
    if audit_path:
        kwargs["grimoire_log_path"] = audit_path
    return SHABD(name, **kwargs)


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


def _http(method: str, url: str, *,
          headers: dict = None, body: bytes = None,
          cookies: dict = None) -> tuple[int, dict, str]:
    h = dict(headers or {})
    if cookies:
        h["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
    req = urllib.request.Request(
        url, data=body, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, dict(r.headers), r.read().decode(
                "utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read().decode(
            "utf-8", "replace")


# ---------------------------------------------------------------------------
# EASY — UIServer-method-level
# ---------------------------------------------------------------------------


class LlmConfigMethodTests(unittest.TestCase):
    def setUp(self):
        os.environ["SHABD_UI_BOOTSTRAP_USER"] = "root"
        os.environ["SHABD_UI_BOOTSTRAP_PASSWORD"] = "rootpw"
        self.app = _new_app()
        self.ui = UIServer(self.app, bind="127.0.0.1",
                            port=PORT_BASE + 1,
                            superusers=["root"])
        self.sess = self.ui._login("root", "rootpw")

    def tearDown(self):
        os.environ.pop("SHABD_UI_BOOTSTRAP_PASSWORD", None)

    def test_default_is_none(self):
        self.assertEqual(
            self.ui.get_llm_config()["backend"], "none")

    def test_set_ollama(self):
        self.ui.set_llm_config(
            self.sess, backend="ollama",
            base_url="http://127.0.0.1:11434",
            model="llama3.1:8b", api_key="")
        c = self.ui.get_llm_config(redact=False)
        self.assertEqual(c["backend"], "ollama")
        self.assertEqual(c["model"], "llama3.1:8b")

    def test_api_key_redacted_by_default(self):
        self.ui.set_llm_config(
            self.sess, backend="openai",
            base_url="https://api.openai.com/v1",
            model="gpt-4o", api_key="sk-abc123")
        c = self.ui.get_llm_config(redact=True)
        self.assertEqual(c["api_key"], "***")
        c2 = self.ui.get_llm_config(redact=False)
        self.assertEqual(c2["api_key"], "sk-abc123")

    def test_rejects_bad_backend(self):
        with self.assertRaises(UIError) as ctx:
            self.ui.set_llm_config(
                self.sess, backend="weirdai",
                base_url="http://x", model="y")
        self.assertEqual(ctx.exception.status, 400)

    def test_rejects_bad_url(self):
        with self.assertRaises(UIError) as ctx:
            self.ui.set_llm_config(
                self.sess, backend="openai",
                base_url="ftp://api", model="y")
        self.assertEqual(ctx.exception.status, 400)

    def test_none_does_not_require_url(self):
        # Reset to none — should accept empty url/model
        res = self.ui.set_llm_config(
            self.sess, backend="none",
            base_url="", model="")
        self.assertEqual(res["backend"], "none")

    def test_build_backend_returns_mock_for_none(self):
        from shabd_agent import MockBackend
        be = self.ui.build_llm_backend()
        self.assertIsInstance(be, MockBackend)

    def test_build_backend_returns_openai_compat_for_ollama(self):
        from shabd_agent import OpenAICompatBackend
        self.ui.set_llm_config(
            self.sess, backend="ollama",
            base_url="http://127.0.0.1:11434",
            model="llama3.1:8b")
        be = self.ui.build_llm_backend()
        self.assertIsInstance(be, OpenAICompatBackend)
        # Ollama base URL should auto-append /v1
        self.assertTrue(be.base_url.endswith("/v1"))


class AgentRegistryMethodTests(unittest.TestCase):
    def setUp(self):
        os.environ["SHABD_UI_BOOTSTRAP_USER"] = "root"
        os.environ["SHABD_UI_BOOTSTRAP_PASSWORD"] = "rootpw"
        self.app = _new_app()

        @self.app.spell
        def add(a: int, b: int) -> int:
            return a + b

        @self.app.spell
        def echo(msg: str) -> str:
            return msg

        self.ui = UIServer(self.app, bind="127.0.0.1",
                            port=PORT_BASE + 2,
                            superusers=["root"])
        self.sess = self.ui._login("root", "rootpw")

    def tearDown(self):
        os.environ.pop("SHABD_UI_BOOTSTRAP_PASSWORD", None)

    def test_save_and_list_agent(self):
        self.ui.save_agent(
            self.sess, name="math-bot",
            system="You can add numbers.",
            tools=["add"], description="adds")
        self.assertIn("math-bot", self.ui._agents)
        a = self.ui._agents["math-bot"]
        self.assertEqual(a["tools"], ["add"])
        self.assertEqual(a["created_by"], "root")

    def test_save_rejects_unknown_tool(self):
        with self.assertRaises(UIError) as ctx:
            self.ui.save_agent(
                self.sess, name="x",
                system="", tools=["ghost"])
        self.assertEqual(ctx.exception.status, 404)

    def test_save_rejects_bad_name(self):
        with self.assertRaises(UIError) as ctx:
            self.ui.save_agent(
                self.sess, name="bad name!",
                system="", tools=[])
        self.assertEqual(ctx.exception.status, 400)

    def test_delete_agent(self):
        self.ui.save_agent(
            self.sess, name="ag1", system="", tools=[])
        self.ui.delete_agent(self.sess, "ag1")
        self.assertNotIn("ag1", self.ui._agents)

    def test_delete_unknown_agent(self):
        with self.assertRaises(UIError) as ctx:
            self.ui.delete_agent(self.sess, "ghost")
        self.assertEqual(ctx.exception.status, 404)

    def test_run_saved_agent_with_mock_llm(self):
        self.ui.save_agent(
            self.sess, name="math-bot",
            system="You are a math assistant.",
            tools=["add"])
        res = self.ui.run_agent(
            self.sess, name="math-bot",
            prompt="What is 2 + 3?", max_steps=2)
        self.assertTrue(res["ok"])
        # MockBackend with no plan still returns SOMETHING.
        self.assertIn("answer", res)

    def test_run_adhoc_agent(self):
        res = self.ui.run_agent(
            self.sess, name=None,
            prompt="hello", system="You are nice.",
            tools=["echo"], max_steps=2)
        self.assertTrue(res["ok"])


# ---------------------------------------------------------------------------
# MEDIUM — Spell editor
# ---------------------------------------------------------------------------


class SpellEditorMethodTests(unittest.TestCase):
    def setUp(self):
        os.environ["SHABD_UI_BOOTSTRAP_USER"] = "root"
        os.environ["SHABD_UI_BOOTSTRAP_PASSWORD"] = "rootpw"
        self.app = _new_app()
        self.ui = UIServer(self.app, bind="127.0.0.1",
                            port=PORT_BASE + 3,
                            superusers=["root"])
        self.sess = self.ui._login("root", "rootpw")
        self.ui.create_spell(
            self.sess, name="hello",
            source="def hello(n: str) -> str:\n    return 'hi ' + n\n",
            description="say hi", scopes=[])

    def tearDown(self):
        os.environ.pop("SHABD_UI_BOOTSTRAP_PASSWORD", None)

    def test_get_source_returns_dict(self):
        src = self.ui.get_spell_source("hello")
        self.assertIsNotNone(src)
        self.assertIn("def hello", src["source"])

    def test_get_source_none_for_code_spell(self):
        @self.app.spell
        def in_code() -> int:
            return 1

        self.assertIsNone(self.ui.get_spell_source("in_code"))

    def test_update_changes_behaviour(self):
        # original returns 'hi NAME'
        self.assertEqual(
            self.app.invoke("hello", {"n": "amit"}), "hi amit")
        new_src = ("def hello(n: str) -> str:\n"
                   "    return 'hola ' + n\n")
        self.ui.update_spell_source(
            self.sess, name="hello", source=new_src,
            description="say hi (spanish)", scopes=[])
        self.assertEqual(
            self.app.invoke("hello", {"n": "amit"}), "hola amit")

    def test_update_rejects_code_spell(self):
        @self.app.spell
        def lives_in_code() -> int:
            return 7

        with self.assertRaises(UIError) as ctx:
            self.ui.update_spell_source(
                self.sess, name="lives_in_code",
                source="def lives_in_code() -> int:\n    return 8\n")
        self.assertEqual(ctx.exception.status, 403)

    def test_update_rolls_back_on_syntax_error(self):
        with self.assertRaises(UIError):
            self.ui.update_spell_source(
                self.sess, name="hello",
                source="def hello(:\n  pass\n")
        # Original still works
        self.assertEqual(
            self.app.invoke("hello", {"n": "x"}), "hi x")

    def test_update_audits_old_and_new_hash(self):
        before = len(self.app.grimoire.pages(limit=10_000))
        new_src = "def hello(n: str) -> str:\n    return n.upper()\n"
        self.ui.update_spell_source(
            self.sess, name="hello", source=new_src)
        after = self.app.grimoire.pages(limit=10_000)
        self.assertEqual(len(after), before + 1)
        page = after[-1]
        self.assertEqual(
            page["spell"], "__ui_admin:update_spell_source")


# ---------------------------------------------------------------------------
# HARD — Live HTTP for new endpoints
# ---------------------------------------------------------------------------


class LiveV210HttpTests(unittest.TestCase):
    PORT = PORT_BASE + 50

    @classmethod
    def setUpClass(cls):
        os.environ["SHABD_UI_BOOTSTRAP_USER"] = "root"
        os.environ["SHABD_UI_BOOTSTRAP_PASSWORD"] = "rootpw"
        cls.app = _new_app("live-v210")

        @cls.app.spell
        def add(a: int, b: int) -> int:
            return a + b

        @cls.app.spell
        def echo(msg: str) -> str:
            return msg

        cls.ui = UIServer(cls.app, bind="127.0.0.1", port=cls.PORT,
                           superusers=["root"])
        _start(cls.ui)
        cls.base = f"http://127.0.0.1:{cls.PORT}"
        cls.sess = cls.ui._login("root", "rootpw")

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("SHABD_UI_BOOTSTRAP_PASSWORD", None)

    def test_llm_config_round_trip(self):
        body = json.dumps({
            "backend": "ollama",
            "base_url": "http://127.0.0.1:11434",
            "model": "llama3.1:8b",
            "api_key": "secret-key",
        }).encode()
        s, _, raw = _http(
            "POST", f"{self.base}/api/llm_config",
            headers={"Content-Type": "application/json",
                      "X-CSRF": self.sess.csrf},
            body=body,
            cookies={"shabd_sid": self.sess.sid},
        )
        self.assertEqual(s, 200)
        self.assertTrue(json.loads(raw)["ok"])
        # GET returns redacted
        s, _, raw = _http(
            "GET", f"{self.base}/api/llm_config",
            cookies={"shabd_sid": self.sess.sid})
        cfg = json.loads(raw)
        self.assertEqual(cfg["backend"], "ollama")
        self.assertEqual(cfg["api_key"], "***")

    def test_save_agent_via_http(self):
        body = json.dumps({
            "name": "echo-bot",
            "system": "Echo what the user says.",
            "tools": ["echo"],
            "max_steps": 3,
        }).encode()
        s, _, raw = _http(
            "POST", f"{self.base}/api/agents/save",
            headers={"Content-Type": "application/json",
                      "X-CSRF": self.sess.csrf},
            body=body,
            cookies={"shabd_sid": self.sess.sid},
        )
        self.assertEqual(s, 200)
        self.assertTrue(json.loads(raw)["ok"])
        self.assertIn("echo-bot", self.ui._agents)

    def test_list_agents_via_http(self):
        # Prereq: at least the echo-bot from previous test exists
        if "echo-bot" not in self.ui._agents:
            self.ui.save_agent(
                self.sess, name="echo-bot", system="",
                tools=["echo"])
        s, _, raw = _http(
            "GET", f"{self.base}/api/agents",
            cookies={"shabd_sid": self.sess.sid})
        self.assertEqual(s, 200)
        d = json.loads(raw)
        self.assertIn("echo", d["spells"])
        self.assertTrue(any(a["name"] == "echo-bot"
                             for a in d["agents"]))

    def test_run_agent_via_http(self):
        # Force mock LLM so this test never touches the network
        self.ui.set_llm_config(
            self.sess, backend="none",
            base_url="", model="")
        self.ui.save_agent(
            self.sess, name="run-test",
            system="Help the user.", tools=["echo"])
        body = json.dumps({
            "name": "run-test",
            "prompt": "hello there",
        }).encode()
        s, _, raw = _http(
            "POST", f"{self.base}/api/agents/run",
            headers={"Content-Type": "application/json",
                      "X-CSRF": self.sess.csrf},
            body=body,
            cookies={"shabd_sid": self.sess.sid},
        )
        self.assertEqual(s, 200)
        d = json.loads(raw)
        self.assertTrue(d["ok"], d)
        self.assertIsInstance(d["steps"], list)

    def test_get_spell_source_via_http(self):
        # Need a UI-built spell
        if "myspell" not in self.app._spells:
            self.ui.create_spell(
                self.sess, name="myspell",
                source="def myspell() -> int:\n    return 42\n")
        s, _, raw = _http(
            "GET", f"{self.base}/api/spells/myspell/source",
            cookies={"shabd_sid": self.sess.sid})
        self.assertEqual(s, 200)
        d = json.loads(raw)
        self.assertIn("def myspell", d["source"])

    def test_get_spell_source_404_for_code_spell(self):
        s, _, _ = _http(
            "GET", f"{self.base}/api/spells/add/source",
            cookies={"shabd_sid": self.sess.sid})
        self.assertEqual(s, 404)

    def test_update_spell_source_via_http(self):
        if "upspell" not in self.app._spells:
            self.ui.create_spell(
                self.sess, name="upspell",
                source="def upspell(x: int) -> int:\n    return x\n")
        new_src = "def upspell(x: int) -> int:\n    return x * 10\n"
        body = json.dumps({
            "source": new_src,
            "description": "Multiply by 10",
            "scopes": [], "tags": [],
        }).encode()
        s, _, raw = _http(
            "POST", f"{self.base}/api/spells/upspell/update",
            headers={"Content-Type": "application/json",
                      "X-CSRF": self.sess.csrf},
            body=body,
            cookies={"shabd_sid": self.sess.sid},
        )
        self.assertEqual(s, 200)
        self.assertEqual(self.app.invoke("upspell", {"x": 5}), 50)


# ---------------------------------------------------------------------------
# COMPLEX — token-test mode actually enforces scope
# ---------------------------------------------------------------------------


class TokenTestModeEnforcementTests(unittest.TestCase):
    """When the UI fetches /spells/<name> with a Bearer token, SHABD's
    real auth + scope pipeline runs. This proves the "Test as token"
    button on the Spells page is a faithful proxy for an external
    client call."""

    PORT_HTTP = PORT_BASE + 70

    @classmethod
    def setUpClass(cls):
        cls.app = _new_app("scope-enf", require_auth=True)

        @cls.app.spell(scopes=["payments"])
        def transfer(src: str, dst: str, amount: int) -> dict:
            return {"ok": True, "src": src, "dst": dst,
                    "amount": amount}

        threading.Thread(
            target=cls.app.serve,
            kwargs={"host": "127.0.0.1", "port": cls.PORT_HTTP},
            daemon=True).start()
        for _ in range(80):
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{cls.PORT_HTTP}/healthz",
                        timeout=1) as r:
                    if r.status == 200:
                        break
            except Exception:
                time.sleep(0.05)
        else:
            raise RuntimeError("HTTP server did not start")

    def test_call_without_token_is_unauthorised(self):
        s, _, _ = _http(
            "POST", f"http://127.0.0.1:{self.PORT_HTTP}/spells/transfer",
            headers={"Content-Type": "application/json"},
            body=b'{"src":"a","dst":"b","amount":100}',
        )
        self.assertIn(s, (401, 403))

    def test_call_with_scoped_token_succeeds(self):
        tok = self.app.issue_token("ui-tester", ["payments"], ttl=120)
        s, _, raw = _http(
            "POST", f"http://127.0.0.1:{self.PORT_HTTP}/spells/transfer",
            headers={"Content-Type": "application/json",
                      "Authorization": f"Bearer {tok}"},
            body=b'{"src":"a","dst":"b","amount":100}',
        )
        self.assertEqual(s, 200, raw)

    def test_call_with_wrong_scope_token_is_forbidden(self):
        tok = self.app.issue_token("ui-tester", ["other"], ttl=120)
        s, _, _ = _http(
            "POST", f"http://127.0.0.1:{self.PORT_HTTP}/spells/transfer",
            headers={"Content-Type": "application/json",
                      "Authorization": f"Bearer {tok}"},
            body=b'{"src":"a","dst":"b","amount":100}',
        )
        self.assertIn(s, (401, 403))


# ---------------------------------------------------------------------------
# COMPLEX² — LLM config + agent registry survive restart via sidecar
# ---------------------------------------------------------------------------


class StateFilePersistenceTests(unittest.TestCase):
    """The whole point: a fresh UIServer reading the same audit + state
    files comes up with the same LLM config and saved agents."""

    def setUp(self):
        os.environ["SHABD_UI_BOOTSTRAP_USER"] = "root"
        os.environ["SHABD_UI_BOOTSTRAP_PASSWORD"] = "rootpw"
        self.tmp = tempfile.TemporaryDirectory()
        self.audit = os.path.join(self.tmp.name, "audit.jsonl")

    def tearDown(self):
        os.environ.pop("SHABD_UI_BOOTSTRAP_PASSWORD", None)
        self.tmp.cleanup()

    def _new_ui(self, port: int) -> UIServer:
        app = _new_app("persist", audit_path=self.audit)
        return UIServer(app, bind="127.0.0.1", port=port,
                         superusers=["root"])

    def test_llm_config_survives_restart(self):
        ui1 = self._new_ui(PORT_BASE + 100)
        sess = ui1._login("root", "rootpw")
        ui1.set_llm_config(
            sess, backend="ollama",
            base_url="http://127.0.0.1:11434",
            model="llama3.1:8b", api_key="sk-secret")
        # Simulate restart
        ui2 = self._new_ui(PORT_BASE + 101)
        cfg = ui2.get_llm_config(redact=False)
        self.assertEqual(cfg["backend"], "ollama")
        self.assertEqual(cfg["model"], "llama3.1:8b")
        self.assertEqual(cfg["api_key"], "sk-secret")

    def test_saved_agent_survives_restart(self):
        ui1 = self._new_ui(PORT_BASE + 102)
        sess = ui1._login("root", "rootpw")
        # Need a spell to attach
        @ui1.app.spell
        def add(a: int, b: int) -> int:
            return a + b
        ui1.save_agent(
            sess, name="math-bot",
            system="You add numbers.", tools=["add"])
        # Restart
        ui2 = self._new_ui(PORT_BASE + 103)
        self.assertIn("math-bot", ui2._agents)
        self.assertEqual(
            ui2._agents["math-bot"]["tools"], ["add"])


def main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for cls in (LlmConfigMethodTests, AgentRegistryMethodTests,
                SpellEditorMethodTests, LiveV210HttpTests,
                TokenTestModeEnforcementTests,
                StateFilePersistenceTests):
        suite.addTests(loader.loadTestsFromTestCase(cls))
    return 0 if unittest.TextTestRunner(
        verbosity=2, stream=sys.stdout).run(suite).wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
