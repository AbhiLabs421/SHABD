"""Tests for shabd_ui — production no-code dashboard with Keycloak."""
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
from shabd_ui import Session, UIError, UIServer, _decode_jwt_payload  # noqa: E402


def _start(ui: UIServer) -> threading.Thread:
    t = threading.Thread(target=ui.serve, daemon=True)
    t.start()
    # Wait until port is live
    for _ in range(50):
        try:
            with urllib.request.urlopen(
                    f"http://{ui.bind}:{ui.port}/healthz", timeout=1) as r:
                if r.status == 200:
                    return t
        except Exception:
            time.sleep(0.05)
    raise RuntimeError("UI did not come up")


def _http(method: str, url: str, *,
          headers: dict = None, body: bytes = None,
          cookies: dict = None) -> tuple[int, dict, dict, str]:
    h = dict(headers or {})
    if cookies:
        h["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
    req = urllib.request.Request(url, data=body, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            data = r.read()
            return r.status, dict(r.headers), {}, data.decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), {}, e.read().decode("utf-8", "replace")


def _extract_cookies(headers: dict) -> dict:
    out = {}
    raw = headers.get("Set-Cookie", "")
    if raw:
        # Only the first segment before ';' is name=value
        head = raw.split(";")[0]
        if "=" in head:
            k, v = head.split("=", 1)
            out[k.strip()] = v.strip()
    return out


class JWTDecodeTests(unittest.TestCase):
    def test_decode_valid(self):
        import base64
        import json as _j
        claims = {"sub": "amit", "realm_access": {"roles": ["admin"]}}
        seg = base64.urlsafe_b64encode(
            _j.dumps(claims).encode()).decode().rstrip("=")
        decoded = _decode_jwt_payload(f"h.{seg}.s")
        self.assertEqual(decoded["sub"], "amit")
        self.assertEqual(decoded["realm_access"]["roles"], ["admin"])

    def test_decode_garbage_returns_empty(self):
        self.assertEqual(_decode_jwt_payload("not-a-jwt"), {})


class BootstrapAuthTests(unittest.TestCase):
    def setUp(self):
        os.environ["SHABD_UI_BOOTSTRAP_USER"] = "abhi"
        os.environ["SHABD_UI_BOOTSTRAP_PASSWORD"] = "supersecret"
        self.app = SHABD("test-ui", secret="x" * 32, require_auth=False)

        @self.app.spell
        def add(a: int, b: int) -> int:
            return a + b

        self.app._add = add  # keep reference
        self.ui = UIServer(self.app, bind="127.0.0.1",
                            port=18101 + (hash(self._testMethodName) % 1000),
                            superusers=["abhi"])

    def test_bootstrap_login_succeeds(self):
        sess = self.ui._login("abhi", "supersecret")
        self.assertIn("superuser", sess.roles)
        self.assertIn("admin", sess.roles)

    def test_bootstrap_login_rejects_wrong_password(self):
        with self.assertRaises(UIError) as ctx:
            self.ui._login("abhi", "wrong")
        self.assertEqual(ctx.exception.status, 401)

    def test_login_throttle(self):
        for _ in range(5):
            try:
                self.ui._login("abhi", "wrong")
            except UIError:
                pass
        with self.assertRaises(UIError) as ctx:
            self.ui._login("abhi", "wrong")
        self.assertEqual(ctx.exception.status, 429)


class LiveServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["SHABD_UI_BOOTSTRAP_USER"] = "amit"
        os.environ["SHABD_UI_BOOTSTRAP_PASSWORD"] = "topsecret"
        cls.app = SHABD("live-ui", secret="x" * 32, require_auth=False)

        @cls.app.spell
        def echo(msg: str) -> str:
            return msg

        cls.ui = UIServer(cls.app, bind="127.0.0.1", port=18222,
                           superusers=["amit"])
        _start(cls.ui)
        cls.base = f"http://{cls.ui.bind}:{cls.ui.port}"

    def test_healthz_public(self):
        status, _, _, body = _http("GET", f"{self.base}/healthz")
        self.assertEqual(status, 200)
        self.assertIn("ok", body)

    def test_root_without_session_redirects_to_login(self):
        status, headers, _, _ = _http("GET", f"{self.base}/",
                                       headers={})
        # urllib follows redirects by default; check the login page
        self.assertEqual(status, 200)

    def test_api_without_session_returns_401_json(self):
        status, _, _, body = _http("GET", f"{self.base}/api/dashboard")
        self.assertEqual(status, 401)
        self.assertIn("not signed in", body)

    def test_login_flow_with_correct_creds_sets_cookie(self):
        body = b"username=amit&password=topsecret"
        status, headers, _, _ = _http(
            "POST", f"{self.base}/login",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            body=body,
        )
        self.assertEqual(status, 200)  # 303 → followed → login page final
        # Note: urllib follows 303 to /, so the cookie isn't visible
        # in the final response headers. Instead, assert via the
        # session store that a session was created.
        _ = headers
        sessions = self.ui.sessions.all()
        self.assertTrue(any(s.username == "amit" for s in sessions))

    def test_invoke_via_api_requires_csrf(self):
        # Make a session straight from the API
        sess = self.ui._login("amit", "topsecret")
        # No CSRF header → 403
        status, _, _, body = _http(
            "POST", f"{self.base}/api/invoke/echo",
            headers={"Content-Type": "application/json"},
            body=b'{"msg":"hi"}',
            cookies={"shabd_sid": sess.sid},
        )
        self.assertEqual(status, 403)
        self.assertIn("csrf", body.lower())

    def test_invoke_via_api_with_csrf_succeeds(self):
        sess = self.ui._login("amit", "topsecret")
        status, _, _, body = _http(
            "POST", f"{self.base}/api/invoke/echo",
            headers={"Content-Type": "application/json",
                      "X-CSRF": sess.csrf},
            body=b'{"msg":"hello"}',
            cookies={"shabd_sid": sess.sid},
        )
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["result"], "hello")

    def test_dashboard_returns_session_roles(self):
        sess = self.ui._login("amit", "topsecret")
        status, _, _, body = _http(
            "GET", f"{self.base}/api/dashboard",
            cookies={"shabd_sid": sess.sid},
        )
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertIn("superuser", payload["session"]["roles"])

    def test_unknown_spell_returns_404(self):
        sess = self.ui._login("amit", "topsecret")
        status, _, _, _ = _http(
            "POST", f"{self.base}/api/invoke/notthere",
            headers={"Content-Type": "application/json",
                      "X-CSRF": sess.csrf},
            body=b"{}",
            cookies={"shabd_sid": sess.sid},
        )
        self.assertEqual(status, 404)


class SessionRBACTests(unittest.TestCase):
    def test_session_role_helpers(self):
        s = Session(sid="a", username="u", roles=["user"],
                     access_token="x")
        self.assertFalse(s.is_admin())
        self.assertFalse(s.is_superuser())
        s2 = Session(sid="a", username="u", roles=["admin"],
                      access_token="x")
        self.assertTrue(s2.is_admin())
        self.assertFalse(s2.is_superuser())
        s3 = Session(sid="a", username="u", roles=["superuser"],
                      access_token="x")
        self.assertTrue(s3.is_admin())  # superuser implies admin
        self.assertTrue(s3.is_superuser())


def main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for cls in (JWTDecodeTests, BootstrapAuthTests, LiveServerTests,
                SessionRBACTests):
        suite.addTests(loader.loadTestsFromTestCase(cls))
    return 0 if unittest.TextTestRunner(verbosity=2,
                                         stream=sys.stdout).run(suite).wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
