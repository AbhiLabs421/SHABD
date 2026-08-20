"""Tests for shabd_ui v2.9 — Spell Builder, Token issuance, Scope editor
and the Client-Console proxy.

Coverage levels:
  Easy     : pure helpers (_SAFE_BUILTINS, _compile_spell_source).
  Medium   : single UIServer method calls (create_spell, issue_token,
             update_scopes, RBAC enforcement).
  Hard     : HTTP round-trip through the live server with CSRF + cookies.
  Complex  : two SHABD servers — UI proxies a call from one to the other
             over the client console (the MCP-client pattern), then
             verifies both audit chains and that the scoped token
             actually authorises the call end-to-end.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shabd import SHABD  # noqa: E402
from shabd_ui import (  # noqa: E402
    _SAFE_BUILTINS,
    UIError,
    UIServer,
    _compile_spell_source,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _new_app(name: str = "test") -> SHABD:
    return SHABD(name, secret="x" * 32, require_auth=False)


def _new_ui(app: SHABD, *, superusers=("root",), admins=(),
             port_base: int = 19000, suffix: str = "") -> UIServer:
    port = port_base + (hash(suffix) % 800)
    return UIServer(app, bind="127.0.0.1", port=port,
                    superusers=list(superusers), admins=list(admins))


def _start(ui: UIServer) -> threading.Thread:
    t = threading.Thread(target=ui.serve, daemon=True)
    t.start()
    for _ in range(80):
        try:
            with urllib.request.urlopen(
                    f"http://{ui.bind}:{ui.port}/healthz",
                    timeout=1) as r:
                if r.status == 200:
                    return t
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
# EASY — pure-function helpers
# ---------------------------------------------------------------------------

class SafeBuiltinsTests(unittest.TestCase):
    def test_no_os_or_subprocess_exposed(self):
        for banned in ("os", "subprocess", "open", "eval",
                        "exec", "compile", "globals", "locals"):
            self.assertNotIn(
                banned, _SAFE_BUILTINS,
                f"{banned!r} must not be in the safe namespace")

    def test_basic_types_present(self):
        for needed in ("int", "str", "list", "dict", "len", "range",
                        "isinstance", "True", "False", "None"):
            self.assertIn(needed, _SAFE_BUILTINS)


class CompileSpellSourceTests(unittest.TestCase):
    def test_simple_function_compiles(self):
        src = "def add(a: int, b: int) -> int:\n    return a + b\n"
        fn = _compile_spell_source("add", src)
        self.assertEqual(fn(2, 3), 5)

    def test_syntax_error_is_400(self):
        with self.assertRaises(UIError) as ctx:
            _compile_spell_source("bad", "def bad(:\n  return 1")
        self.assertEqual(ctx.exception.status, 400)
        self.assertIn("syntax error", ctx.exception.message)

    def test_missing_function_is_400(self):
        with self.assertRaises(UIError) as ctx:
            _compile_spell_source("missing", "x = 1\n")
        self.assertEqual(ctx.exception.status, 400)

    def test_invalid_name_is_rejected(self):
        with self.assertRaises(UIError) as ctx:
            _compile_spell_source("123bad", "def x(): pass")
        self.assertEqual(ctx.exception.status, 400)

    def test_too_large_source_is_413(self):
        src = "x = '" + ("a" * (33 * 1024)) + "'\n"
        with self.assertRaises(UIError) as ctx:
            _compile_spell_source("x", src)
        self.assertEqual(ctx.exception.status, 413)

    def test_runtime_error_at_definition_is_400(self):
        # KeyError raised while module body runs
        src = "{}['nope']\n"
        with self.assertRaises(UIError) as ctx:
            _compile_spell_source("y", src)
        self.assertEqual(ctx.exception.status, 400)


# ---------------------------------------------------------------------------
# MEDIUM — UIServer method calls (no HTTP layer)
# ---------------------------------------------------------------------------

class CreateSpellMethodTests(unittest.TestCase):
    def setUp(self):
        os.environ["SHABD_UI_BOOTSTRAP_USER"] = "root"
        os.environ["SHABD_UI_BOOTSTRAP_PASSWORD"] = "rootpw"
        self.app = _new_app()
        self.ui = _new_ui(self.app, suffix=self._testMethodName)
        self.sess = self.ui._login("root", "rootpw")

    def test_create_then_invoke(self):
        src = ("def hello(name: str) -> str:\n"
               "    return 'hi ' + name\n")
        meta = self.ui.create_spell(
            self.sess, name="hello", source=src,
            description="say hi", scopes=[], tags=["greet"])
        self.assertEqual(meta["name"], "hello")
        self.assertIn("hello", self.app._spells)
        out = self.app.invoke("hello", {"name": "amit"})
        self.assertEqual(out, "hi amit")

    def test_duplicate_spell_rejected(self):
        src = "def dup() -> int:\n    return 1\n"
        self.ui.create_spell(self.sess, name="dup", source=src)
        with self.assertRaises(UIError) as ctx:
            self.ui.create_spell(self.sess, name="dup", source=src)
        self.assertEqual(ctx.exception.status, 409)

    def test_delete_only_works_on_dynamic(self):
        # First a code-registered spell (cannot be deleted)
        @self.app.spell
        def static_spell() -> int:
            return 7

        with self.assertRaises(UIError) as ctx:
            self.ui.delete_spell(self.sess, "static_spell")
        self.assertEqual(ctx.exception.status, 403)

        # Now a dynamic one — must work
        self.ui.create_spell(
            self.sess, name="dyn",
            source="def dyn() -> int:\n    return 9\n")
        self.assertIn("dyn", self.app._spells)
        self.ui.delete_spell(self.sess, "dyn")
        self.assertNotIn("dyn", self.app._spells)

    def test_create_audited_in_grimoire(self):
        before = len(self.app.grimoire.pages(limit=10_000))
        self.ui.create_spell(
            self.sess, name="aud",
            source="def aud() -> int:\n    return 1\n")
        after = self.app.grimoire.pages(limit=10_000)
        self.assertEqual(len(after), before + 1)
        page = after[-1]
        self.assertEqual(page["spell"], "__ui_admin:create_spell")
        self.assertEqual(page["subject"], "root")
        self.assertTrue(page["ok"])

    def test_chain_remains_verified_after_admin_actions(self):
        for i in range(3):
            self.ui.create_spell(
                self.sess, name=f"s{i}",
                source=f"def s{i}() -> int:\n    return {i}\n")
            self.ui.update_scopes(self.sess, f"s{i}", ["x"])
        self.assertTrue(self.app.grimoire.verify()["ok"])


class TokenIssueMethodTests(unittest.TestCase):
    def setUp(self):
        os.environ["SHABD_UI_BOOTSTRAP_USER"] = "root"
        os.environ["SHABD_UI_BOOTSTRAP_PASSWORD"] = "rootpw"
        self.app = _new_app()
        self.ui = _new_ui(self.app, suffix=self._testMethodName)
        self.sess = self.ui._login("root", "rootpw")

    def test_issue_token_returns_verifiable_token(self):
        out = self.ui.issue_token(
            self.sess, subject="agent-1",
            scopes=["trader", "kyc"], ttl=120)
        self.assertIn("token", out)
        payload = self.app.tokens.verify(out["token"])
        self.assertEqual(payload["sub"], "agent-1")
        self.assertEqual(sorted(payload["scopes"]),
                          ["kyc", "trader"])

    def test_issue_token_rejects_zero_ttl(self):
        with self.assertRaises(UIError) as ctx:
            self.ui.issue_token(
                self.sess, subject="a", scopes=[], ttl=0)
        self.assertEqual(ctx.exception.status, 400)

    def test_issue_token_rejects_excessive_ttl(self):
        with self.assertRaises(UIError) as ctx:
            self.ui.issue_token(
                self.sess, subject="a", scopes=[],
                ttl=30 * 86400)
        self.assertEqual(ctx.exception.status, 400)

    def test_issue_token_requires_subject(self):
        with self.assertRaises(UIError) as ctx:
            self.ui.issue_token(
                self.sess, subject="", scopes=[], ttl=600)
        self.assertEqual(ctx.exception.status, 400)


class UpdateScopesMethodTests(unittest.TestCase):
    def setUp(self):
        os.environ["SHABD_UI_BOOTSTRAP_USER"] = "root"
        os.environ["SHABD_UI_BOOTSTRAP_PASSWORD"] = "rootpw"
        self.app = _new_app()

        @self.app.spell(scopes=["original"])
        def pay(amount: int) -> dict:
            return {"ok": True, "amount": amount}

        self.ui = _new_ui(self.app, suffix=self._testMethodName)
        self.sess = self.ui._login("root", "rootpw")

    def test_replace_scopes_takes_effect(self):
        res = self.ui.update_scopes(
            self.sess, "pay", ["payments", "withdrawal"])
        self.assertEqual(sorted(res["scopes"]),
                          ["payments", "withdrawal"])
        spell = self.app._spells["pay"]
        self.assertEqual(sorted(spell.scopes),
                          ["payments", "withdrawal"])

    def test_clear_scopes(self):
        self.ui.update_scopes(self.sess, "pay", [])
        self.assertEqual(self.app._spells["pay"].scopes, [])

    def test_unknown_spell_is_404(self):
        with self.assertRaises(UIError) as ctx:
            self.ui.update_scopes(self.sess, "nope", ["x"])
        self.assertEqual(ctx.exception.status, 404)


# ---------------------------------------------------------------------------
# HARD — live HTTP server, CSRF, cookies, RBAC enforcement
# ---------------------------------------------------------------------------

class LiveBuilderTokensScopesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["SHABD_UI_BOOTSTRAP_USER"] = "root"
        os.environ["SHABD_UI_BOOTSTRAP_PASSWORD"] = "rootpw"
        cls.app = SHABD("live-builder", secret="x" * 32,
                         require_auth=False)

        @cls.app.spell(scopes=["payments"])
        def transfer(src: str, dst: str, amount: int) -> dict:
            return {"ok": True, "src": src, "dst": dst,
                    "amount": amount}

        cls.ui = UIServer(cls.app, bind="127.0.0.1", port=19450,
                           superusers=["root"])
        _start(cls.ui)
        cls.base = f"http://{cls.ui.bind}:{cls.ui.port}"
        cls.root = cls.ui._login("root", "rootpw")

    def test_create_spell_via_http(self):
        src = ("def square(n: int) -> int:\n"
               "    return n * n\n")
        body = json.dumps({
            "name": "square", "source": src,
            "description": "square a number",
            "scopes": [], "tags": ["math"],
        }).encode()
        status, _, resp = _http(
            "POST", f"{self.base}/api/spells/create",
            headers={"Content-Type": "application/json",
                      "X-CSRF": self.root.csrf},
            body=body,
            cookies={"shabd_sid": self.root.sid},
        )
        self.assertEqual(status, 200)
        payload = json.loads(resp)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["name"], "square")
        self.assertEqual(self.app.invoke("square", {"n": 7}), 49)

    def test_create_spell_without_csrf_is_403(self):
        status, _, _ = _http(
            "POST", f"{self.base}/api/spells/create",
            headers={"Content-Type": "application/json"},
            body=b'{"name":"x","source":"def x(): return 1"}',
            cookies={"shabd_sid": self.root.sid},
        )
        self.assertEqual(status, 403)

    def test_non_super_cannot_create_spell(self):
        # A user-only session
        user = self.ui._login("root", "rootpw")
        user.roles = ["user"]  # demote in-memory
        status, _, body = _http(
            "POST", f"{self.base}/api/spells/create",
            headers={"Content-Type": "application/json",
                      "X-CSRF": user.csrf},
            body=b'{"name":"sneak","source":"def sneak(): return 1"}',
            cookies={"shabd_sid": user.sid},
        )
        self.assertEqual(status, 403)
        self.assertIn("superuser", body)

    def test_issue_token_via_http(self):
        body = json.dumps({
            "subject": "ci-bot", "scopes": ["payments"], "ttl": 600,
        }).encode()
        status, _, resp = _http(
            "POST", f"{self.base}/api/tokens/issue",
            headers={"Content-Type": "application/json",
                      "X-CSRF": self.root.csrf},
            body=body,
            cookies={"shabd_sid": self.root.sid},
        )
        self.assertEqual(status, 200)
        payload = json.loads(resp)
        self.assertTrue(payload["ok"])
        self.assertIn("token", payload)
        verified = self.app.tokens.verify(payload["token"])
        self.assertEqual(verified["sub"], "ci-bot")
        self.assertIn("payments", verified["scopes"])

    def test_list_scopes(self):
        status, _, resp = _http(
            "GET", f"{self.base}/api/scopes",
            cookies={"shabd_sid": self.root.sid},
        )
        self.assertEqual(status, 200)
        payload = json.loads(resp)
        names = {s["name"] for s in payload["spells"]}
        self.assertIn("transfer", names)

    def test_update_scopes_via_http(self):
        body = json.dumps({"scopes": ["new_scope"]}).encode()
        status, _, resp = _http(
            "POST", f"{self.base}/api/scopes/transfer",
            headers={"Content-Type": "application/json",
                      "X-CSRF": self.root.csrf},
            body=body,
            cookies={"shabd_sid": self.root.sid},
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            self.app._spells["transfer"].scopes, ["new_scope"])

    def test_builder_page_html_loads_for_super(self):
        status, _, body = _http(
            "GET", f"{self.base}/builder",
            cookies={"shabd_sid": self.root.sid},
        )
        self.assertEqual(status, 200)
        self.assertIn("Spell Builder", body)

    def test_builder_page_blocked_for_user(self):
        user = self.ui._login("root", "rootpw")
        user.roles = ["user"]
        status, _, _ = _http(
            "GET", f"{self.base}/builder",
            cookies={"shabd_sid": user.sid},
        )
        self.assertEqual(status, 403)

    def test_security_headers_present(self):
        """Enterprise hardening: every response must carry the security
        headers (CSP/HSTS/frame-deny/nosniff/referrer/permissions)."""
        status, headers, _ = _http(
            "GET", f"{self.base}/",
            cookies={"shabd_sid": self.root.sid},
        )
        self.assertEqual(status, 200)
        self.assertIn("frame-ancestors", headers.get("Content-Security-Policy", ""))
        self.assertIn("max-age", headers.get("Strict-Transport-Security", ""))
        self.assertEqual(headers.get("X-Frame-Options"), "DENY")
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
        self.assertIn("camera=()", headers.get("Permissions-Policy", ""))

    def test_security_headers_can_be_disabled(self):
        self.ui.security_headers = False
        try:
            _, headers, _ = _http(
                "GET", f"{self.base}/",
                cookies={"shabd_sid": self.root.sid},
            )
            self.assertIsNone(headers.get("Content-Security-Policy"))
            # the always-on basics remain
            self.assertEqual(headers.get("X-Frame-Options"), "DENY")
        finally:
            self.ui.security_headers = True

    def test_spells_page_renders_union_number_inputs(self):
        """Regression: a Union[int, float] / `int | float` param has no
        top-level JSON-Schema `type` (it is `anyOf`). The /spells test
        form must still (a) render it as a numeric input and (b) allow
        decimals via step="any" — otherwise the browser coerces the value
        to a string (→ "matches none of allowed types") or blocks the
        submit on a step mismatch, so a valid call like add(2, 3) or
        add(2.5, 1.25) fails from the UI even though the spell is fine."""
        status, _, body = _http(
            "GET", f"{self.base}/spells",
            cookies={"shabd_sid": self.root.sid},
        )
        self.assertEqual(status, 200)
        # The form-builder JS must understand union (anyOf) numeric params
        # and emit step="any" so floats can be submitted.
        self.assertIn("anyOf", body)
        self.assertIn('step="any"', body)
        # And the coercion must key off the numeric input type, not a
        # bare `p.type` check that misses unions.
        self.assertIn("_numeric", body)

    def test_spells_page_renders_json_textarea_for_object_params(self):
        """Regression: an object/array param (e.g. an MCP tool whose sole
        arg `request` is an object) cannot fit a one-line text input and
        must be sent as real JSON, not a string — else the server rejects
        it with 'request must be object'. The /spells form must render a
        JSON textarea for such params and JSON.parse the value on submit."""
        status, _, body = _http(
            "GET", f"{self.base}/spells",
            cookies={"shabd_sid": self.root.sid},
        )
        self.assertEqual(status, 200)
        self.assertIn("_complex", body)      # object/array detector
        self.assertIn('data-json="1"', body)  # textarea marked for JSON parse
        self.assertIn("JSON.parse", body)      # coercion path exists


# ---------------------------------------------------------------------------
# COMPLEX — two-server flow with the Client console proxy
# ---------------------------------------------------------------------------

class ClientConsoleProxyTests(unittest.TestCase):
    """End-to-end proxy test.

    Topology:
      * `remote_app` runs a full SHABD HTTP server on port 18999.
      * `ui_app`    runs the UI server on 19880.

    A logged-in user on the UI calls `/api/client/call` to ping, fetch
    the manifest, and invoke a remote spell — exactly how the browser's
    Client Console page works. We assert the proxy carries the token,
    reaches the remote, and surfaces the result.
    """

    REMOTE_PORT = 18999
    UI_PORT = 19880

    @classmethod
    def setUpClass(cls):
        os.environ["SHABD_UI_BOOTSTRAP_USER"] = "root"
        os.environ["SHABD_UI_BOOTSTRAP_PASSWORD"] = "rootpw"

        # ---- Remote SHABD server (the "other" entity)
        cls.remote = SHABD("remote-shabd",
                            secret="r" * 32,
                            require_auth=True)

        @cls.remote.spell(scopes=["math"])
        def multiply(a: int, b: int) -> int:
            return a * b

        @cls.remote.spell
        def whoami(ctx) -> dict:
            return {"sub": ctx.subject, "scopes": ctx.scopes}

        # Start it via shabd's own HTTP server on a background thread.
        cls._remote_thread = threading.Thread(
            target=cls.remote.serve,
            kwargs={"host": "127.0.0.1", "port": cls.REMOTE_PORT},
            daemon=True,
        )
        cls._remote_thread.start()
        # Wait until /healthz responds
        for _ in range(80):
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{cls.REMOTE_PORT}/healthz",
                        timeout=1) as r:
                    if r.status == 200:
                        break
            except Exception:
                time.sleep(0.05)
        else:
            raise RuntimeError("remote SHABD did not start")

        # Issue a real token against the remote
        cls.remote_token = cls.remote.issue_token(
            "ui-proxy-test", ["math"], ttl=600)

        # ---- UI server (the local console)
        cls.ui_app = _new_app("ui-shabd")
        cls.ui = UIServer(
            cls.ui_app, bind="127.0.0.1", port=cls.UI_PORT,
            superusers=["root"])
        _start(cls.ui)
        cls.sess = cls.ui._login("root", "rootpw")
        cls.base = f"http://127.0.0.1:{cls.UI_PORT}"

    def _proxy(self, body: dict) -> tuple[int, dict]:
        status, _, raw = _http(
            "POST", f"{self.base}/api/client/call",
            headers={"Content-Type": "application/json",
                      "X-CSRF": self.sess.csrf},
            body=json.dumps(body).encode(),
            cookies={"shabd_sid": self.sess.sid},
        )
        return status, json.loads(raw)

    def test_ping_via_proxy(self):
        status, body = self._proxy({
            "base_url": f"http://127.0.0.1:{self.REMOTE_PORT}",
            "action": "ping",
        })
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        # SHABD's /healthz returns {"status": "ok", ...}
        self.assertEqual(body["health"].get("status"), "ok")

    def test_manifest_via_proxy(self):
        status, body = self._proxy({
            "base_url": f"http://127.0.0.1:{self.REMOTE_PORT}",
            "action": "manifest",
        })
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        names = {s["name"] for s in body["manifest"]["spells"]}
        self.assertIn("multiply", names)
        self.assertIn("whoami", names)

    def test_invoke_remote_spell_with_scope(self):
        status, body = self._proxy({
            "base_url": f"http://127.0.0.1:{self.REMOTE_PORT}",
            "token": self.remote_token,
            "action": "invoke",
            "spell": "multiply",
            "args": {"a": 6, "b": 7},
        })
        self.assertEqual(status, 200)
        self.assertTrue(body.get("ok"), body)
        self.assertEqual(body["result"], 42)

    def test_invoke_remote_without_token_is_unauthorised(self):
        status, body = self._proxy({
            "base_url": f"http://127.0.0.1:{self.REMOTE_PORT}",
            "action": "invoke",
            "spell": "multiply",
            "args": {"a": 2, "b": 3},
        })
        self.assertEqual(status, 200)
        self.assertFalse(body.get("ok"))
        # 401 / 403 from remote surfaced through proxy
        self.assertIn(body.get("status"), (401, 403))

    def test_bad_scheme_is_400(self):
        status, body = self._proxy({
            "base_url": "ftp://example.com",
            "action": "ping",
        })
        self.assertEqual(status, 400)
        self.assertIn("http", body["error"])

    def test_unknown_action_is_400(self):
        status, body = self._proxy({
            "base_url": f"http://127.0.0.1:{self.REMOTE_PORT}",
            "action": "frobnicate",
        })
        self.assertEqual(status, 400)

    def test_grimoire_via_proxy(self):
        status, body = self._proxy({
            "base_url": f"http://127.0.0.1:{self.REMOTE_PORT}",
            "token": self.remote_token,
            "action": "grimoire",
        })
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertIn("verify", body)


# ---------------------------------------------------------------------------
# COMPLEX² — full no-code lifecycle: build → issue token → call via client
# ---------------------------------------------------------------------------

class NoCodeFullLifecycleTests(unittest.TestCase):
    """The full "look ma, no @app.spell in the source!" flow:

       1. superuser logs in
       2. Builder page creates `valuate(price, qty)` from JavaScript
       3. admin issues a token with scope `pricing`
       4. scope is added to the new spell via the UI
       5. an external client (SHABDClient) invokes it with that token —
          the call lands on `app` and is recorded in the Grimoire
       6. the proxy can also reach it through the Client console
    """

    PORT_UI = 19601
    PORT_HTTP = 18760

    @classmethod
    def setUpClass(cls):
        os.environ["SHABD_UI_BOOTSTRAP_USER"] = "root"
        os.environ["SHABD_UI_BOOTSTRAP_PASSWORD"] = "rootpw"
        cls.app = SHABD("lifecycle", secret="x" * 32,
                         require_auth=True)
        # Spin up the SHABD HTTP server (for the SHABDClient leg)
        cls._http_thread = threading.Thread(
            target=cls.app.serve,
            kwargs={"host": "127.0.0.1", "port": cls.PORT_HTTP},
            daemon=True,
        )
        cls._http_thread.start()
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
            raise RuntimeError("app HTTP server did not start")

        # Spin up the UI
        cls.ui = UIServer(cls.app, bind="127.0.0.1",
                           port=cls.PORT_UI, superusers=["root"])
        _start(cls.ui)
        cls.base = f"http://127.0.0.1:{cls.PORT_UI}"
        cls.sess = cls.ui._login("root", "rootpw")

    def test_full_no_code_journey(self):
        # 1) Create the spell from the browser
        src = ("def valuate(price: float, qty: int) -> dict:\n"
               "    total = price * qty\n"
               "    return {'total': total, 'qty': qty}\n")
        status, _, raw = _http(
            "POST", f"{self.base}/api/spells/create",
            headers={"Content-Type": "application/json",
                      "X-CSRF": self.sess.csrf},
            body=json.dumps({
                "name": "valuate", "source": src,
                "description": "Compute order value",
                "scopes": [], "tags": ["pricing"],
            }).encode(),
            cookies={"shabd_sid": self.sess.sid},
        )
        self.assertEqual(status, 200, raw)
        self.assertIn("valuate", self.app._spells)

        # 2) Update scopes via the UI
        status, _, _ = _http(
            "POST", f"{self.base}/api/scopes/valuate",
            headers={"Content-Type": "application/json",
                      "X-CSRF": self.sess.csrf},
            body=json.dumps({"scopes": ["pricing"]}).encode(),
            cookies={"shabd_sid": self.sess.sid},
        )
        self.assertEqual(status, 200)

        # 3) Issue TWO scoped tokens. SHABD enforces JTI-replay
        # protection (each token is one-shot), so each downstream
        # call needs its own freshly-minted token.
        def _mint() -> str:
            _s, _h, _raw = _http(
                "POST", f"{self.base}/api/tokens/issue",
                headers={"Content-Type": "application/json",
                          "X-CSRF": self.sess.csrf},
                body=json.dumps({
                    "subject": "trader-llm",
                    "scopes": ["pricing"], "ttl": 300,
                }).encode(),
                cookies={"shabd_sid": self.sess.sid},
            )
            self.assertEqual(_s, 200, _raw)
            return json.loads(_raw)["token"]

        token_direct = _mint()
        token_proxy = _mint()

        # 4) Call it via the SHABDClient as if from outside
        from shabd_client import SHABDClient
        client = SHABDClient(
            f"http://127.0.0.1:{self.PORT_HTTP}",
            token=token_direct, timeout=4.0)
        res = client.cast(
            "valuate", {"price": 12.5, "qty": 4})
        self.assertEqual(res["total"], 50.0)
        self.assertEqual(res["qty"], 4)

        # 5) Confirm Grimoire has both admin pages and the invoke
        pages = self.app.grimoire.pages(limit=10_000)
        kinds = [p["spell"] for p in pages]
        self.assertIn("__ui_admin:create_spell", kinds)
        self.assertIn("__ui_admin:update_scopes", kinds)
        self.assertIn("__ui_admin:issue_token", kinds)
        self.assertIn("valuate", kinds)
        self.assertTrue(self.app.grimoire.verify()["ok"])

        # 6) Same call should also work via the UI Client Console proxy
        status, _, raw = _http(
            "POST", f"{self.base}/api/client/call",
            headers={"Content-Type": "application/json",
                      "X-CSRF": self.sess.csrf},
            body=json.dumps({
                "base_url": f"http://127.0.0.1:{self.PORT_HTTP}",
                "token": token_proxy,
                "action": "invoke",
                "spell": "valuate",
                "args": {"price": 100, "qty": 3},
            }).encode(),
            cookies={"shabd_sid": self.sess.sid},
        )
        self.assertEqual(status, 200)
        body = json.loads(raw)
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["result"]["total"], 300)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for cls in (SafeBuiltinsTests, CompileSpellSourceTests,
                CreateSpellMethodTests, TokenIssueMethodTests,
                UpdateScopesMethodTests,
                LiveBuilderTokensScopesTests,
                ClientConsoleProxyTests,
                NoCodeFullLifecycleTests):
        suite.addTests(loader.loadTestsFromTestCase(cls))
    return 0 if unittest.TextTestRunner(
        verbosity=2, stream=sys.stdout).run(suite).wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
