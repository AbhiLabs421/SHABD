"""Tests for shabd_users — hash-chained built-in user store + the
register/login UI integration.

Tiers:
  Easy     — pure helpers (scrypt hash, verify, UserError).
  Medium   — UserStore method calls (register, login, role updates,
             delete, idempotent replay, identity events in Grimoire).
  Hard     — live HTTP through shabd_ui: /register, /login, /api/users,
             /api/users/*/roles, RBAC enforcement, throttling.
  Complex  — full lifecycle: bootstrap → invite users → admin promotes
             → audit chain shows every identity event in order →
             chain still verifies after every action.
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
from shabd_ui import UIError, UIServer  # noqa: E402
from shabd_users import (  # noqa: E402
    User,
    UserError,
    UserStore,
    _make_hash,
    _verify_hash,
)

# Make tests fast — drop scrypt cost.
_FAST = {"scrypt_n": 2 ** 10, "scrypt_r": 8, "scrypt_p": 1}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _new_app(name: str = "test") -> SHABD:
    return SHABD(name, secret="x" * 32, require_auth=False)


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
    raise RuntimeError("UI did not come up")


def _http(method: str, url: str, *,
          headers: dict = None, body: bytes = None,
          cookies: dict = None) -> tuple[int, dict, str]:
    h = dict(headers or {})
    if cookies:
        h["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
    req = urllib.request.Request(url, data=body, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, dict(r.headers), r.read().decode(
                "utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read().decode(
            "utf-8", "replace")


def _extract_sid(headers: dict) -> str | None:
    raw = headers.get("Set-Cookie", "")
    if "shabd_sid=" in raw:
        head = raw.split(";")[0]
        return head.split("=", 1)[1].strip()
    return None


# ---------------------------------------------------------------------------
# EASY — pure helpers
# ---------------------------------------------------------------------------

class HashHelpersTests(unittest.TestCase):
    def test_hash_round_trip(self):
        h = _make_hash("hello-world", n=2 ** 10, r=8, p=1)
        self.assertTrue(_verify_hash("hello-world", h))
        self.assertFalse(_verify_hash("wrong", h))

    def test_hash_format_is_self_describing(self):
        h = _make_hash("pw", n=2 ** 10, r=8, p=1)
        parts = h.split("$")
        self.assertEqual(parts[0], "scrypt")
        self.assertEqual(parts[1], str(2 ** 10))

    def test_verify_garbage_safely_returns_false(self):
        self.assertFalse(_verify_hash("anything", "not-a-hash"))
        self.assertFalse(_verify_hash("anything", "scrypt$xx"))
        self.assertFalse(_verify_hash("anything", ""))


class UserDataclassTests(unittest.TestCase):
    def test_admin_helpers(self):
        u = User(username="x", pwd_hash="", roles=["user"])
        self.assertFalse(u.is_admin())
        self.assertFalse(u.is_superuser())
        u2 = User(username="x", pwd_hash="", roles=["admin"])
        self.assertTrue(u2.is_admin())
        self.assertFalse(u2.is_superuser())
        u3 = User(username="x", pwd_hash="", roles=["superuser"])
        self.assertTrue(u3.is_admin())
        self.assertTrue(u3.is_superuser())


# ---------------------------------------------------------------------------
# MEDIUM — UserStore method-level
# ---------------------------------------------------------------------------

class UserStoreBasicsTests(unittest.TestCase):
    def setUp(self):
        self.app = _new_app()
        self.store = UserStore(self.app, **_FAST)

    def test_first_run_detection(self):
        self.assertTrue(self.store.is_first_run())
        self.store.register("alice", "abcd1234")
        self.assertFalse(self.store.is_first_run())

    def test_first_user_auto_superuser(self):
        u = self.store.register("first", "abcd1234")
        self.assertIn("superuser", u.roles)
        self.assertIn("admin", u.roles)
        self.assertIn("user", u.roles)

    def test_subsequent_users_default_to_user(self):
        self.store.register("first", "abcd1234")
        u2 = self.store.register("second", "abcd1234")
        self.assertEqual(u2.roles, ["user"])

    def test_login_success(self):
        self.store.register("amit", "supersecret")
        u = self.store.login("amit", "supersecret")
        self.assertEqual(u.username, "amit")
        self.assertGreater(u.last_login_at, 0)

    def test_login_bad_password_constant_time(self):
        self.store.register("amit", "supersecret")
        t1 = time.time()
        with self.assertRaises(UserError):
            self.store.login("amit", "wrong")
        t2 = time.time()
        with self.assertRaises(UserError):
            self.store.login("nobody", "wrong")
        t3 = time.time()
        # Both should take roughly the same time (within 5×) —
        # protects against username enumeration.
        d1 = t2 - t1
        d2 = t3 - t2
        self.assertLess(abs(d1 - d2), max(d1, d2) * 4 + 0.1)

    def test_username_validation(self):
        for bad in ("", "  ", "a!b", "x" * 65, "ab cd"):
            with self.assertRaises(UserError) as ctx:
                self.store.register(bad, "abcd1234")
            self.assertEqual(ctx.exception.status, 400)

    def test_password_validation(self):
        with self.assertRaises(UserError) as ctx:
            self.store.register("x", "short")
        self.assertEqual(ctx.exception.status, 400)

    def test_duplicate_username(self):
        self.store.register("amit", "abcd1234")
        with self.assertRaises(UserError) as ctx:
            self.store.register("amit", "abcd5678")
        self.assertEqual(ctx.exception.status, 409)

    def test_set_password(self):
        self.store.register("amit", "abcd1234")
        self.store.set_password("amit", "newpass99", actor="amit")
        with self.assertRaises(UserError):
            self.store.login("amit", "abcd1234")
        self.store.login("amit", "newpass99")

    def test_set_roles(self):
        self.store.register("alice", "abcd1234")
        self.store.register("bob", "abcd1234")
        self.store.set_roles("bob", ["admin", "user"], actor="alice")
        self.assertEqual(
            sorted(self.store.get("bob").roles), ["admin", "user"])

    def test_delete_user(self):
        self.store.register("a", "abcd1234")
        self.store.register("b", "abcd1234")
        self.store.delete("b", actor="a")
        self.assertIsNone(self.store.get("b"))
        with self.assertRaises(UserError):
            self.store.login("b", "abcd1234")

    def test_set_password_unknown_user(self):
        with self.assertRaises(UserError) as ctx:
            self.store.set_password("ghost", "abcd1234", actor="x")
        self.assertEqual(ctx.exception.status, 404)


class UserStoreAuditChainTests(unittest.TestCase):
    """The killer feature: every identity event is a Grimoire page."""

    def setUp(self):
        self.app = _new_app()
        self.store = UserStore(self.app, **_FAST)

    def _events(self) -> list[str]:
        return [p["spell"]
                for p in self.app.grimoire.pages(limit=10_000)
                if p["spell"].startswith("__user_event:")]

    def test_register_is_audited(self):
        self.store.register("alice", "abcd1234")
        evts = self._events()
        self.assertIn("__user_event:register", evts)

    def test_login_success_is_audited(self):
        self.store.register("alice", "abcd1234")
        self.store.login("alice", "abcd1234")
        self.assertIn("__user_event:login_ok", self._events())

    def test_login_failure_is_audited(self):
        self.store.register("alice", "abcd1234")
        with self.assertRaises(UserError):
            self.store.login("alice", "wrong")
        self.assertIn("__user_event:login_fail", self._events())

    def test_role_change_is_audited(self):
        self.store.register("alice", "abcd1234")
        self.store.register("bob", "abcd1234")
        self.store.set_roles("bob", ["admin"], actor="alice")
        self.assertIn("__user_event:set_roles", self._events())

    def test_delete_is_audited(self):
        self.store.register("alice", "abcd1234")
        self.store.register("bob", "abcd1234")
        self.store.delete("bob", actor="alice")
        self.assertIn("__user_event:delete", self._events())

    def test_chain_intact_after_many_events(self):
        self.store.register("a1", "abcd1234")
        for i in range(5):
            self.store.register(f"u{i}", "abcd1234")
            self.store.set_roles(f"u{i}", ["admin"], actor="a1")
        for i in range(5):
            try:
                self.store.login(f"u{i}", "wrong")
            except UserError:
                pass
        v = self.store.verify_chain()
        self.assertTrue(v["ok"], v)


# ---------------------------------------------------------------------------
# HARD — live HTTP integration
# ---------------------------------------------------------------------------

class LiveRegisterLoginTests(unittest.TestCase):
    PORT = 20100

    @classmethod
    def setUpClass(cls):
        # Make sure no SHABD_UI_BOOTSTRAP_PASSWORD is set — we want
        # the built-in UserStore path.
        os.environ.pop("SHABD_UI_BOOTSTRAP_PASSWORD", None)
        cls.app = _new_app("live-users")
        # Use the fast store, then attach via `users=`.
        store = UserStore(cls.app, **_FAST)
        cls.ui = UIServer(
            cls.app, bind="127.0.0.1", port=cls.PORT, users=store)
        _start(cls.ui)
        cls.base = f"http://127.0.0.1:{cls.PORT}"

    def test_register_page_loads(self):
        s, _, body = _http("GET", f"{self.base}/register")
        self.assertEqual(s, 200)
        self.assertIn("Create your account", body)

    def test_first_registration_becomes_superuser_via_http(self):
        body = b"username=root&password=abcd1234&password2=abcd1234"
        s, headers, _ = _http(
            "POST", f"{self.base}/register",
            headers={"Content-Type":
                      "application/x-www-form-urlencoded"},
            body=body,
        )
        # urllib follows 303 → 200 from /
        self.assertEqual(s, 200)
        u = self.ui.users.get("root")
        self.assertIsNotNone(u)
        self.assertIn("superuser", u.roles)

    def test_login_after_register(self):
        # Ensure root exists from prior test (test order matters here
        # only because we share state; if it doesn't exist, register it)
        if self.ui.users.get("amit") is None:
            self.ui.users.register("amit", "abcd1234")
        body = b"username=amit&password=abcd1234"
        s, headers, _ = _http(
            "POST", f"{self.base}/login",
            headers={"Content-Type":
                      "application/x-www-form-urlencoded"},
            body=body,
        )
        self.assertEqual(s, 200)
        sids = [s2.sid for s2 in self.ui.sessions.all()
                if s2.username == "amit"]
        self.assertTrue(sids)

    def test_login_bad_password_returns_to_login(self):
        body = b"username=ghost&password=wrongwrong"
        s, _, html = _http(
            "POST", f"{self.base}/login",
            headers={"Content-Type":
                      "application/x-www-form-urlencoded"},
            body=body,
        )
        self.assertEqual(s, 200)
        self.assertIn("bad credentials", html)

    def test_self_register_can_be_disabled(self):
        # Spin up a separate UI with allow_self_register=False
        app2 = _new_app("no-register")
        store2 = UserStore(app2, **_FAST)
        # Pre-register one user so it's not "first run"
        store2.register("root", "abcd1234")
        ui2 = UIServer(
            app2, bind="127.0.0.1", port=20111,
            users=store2, allow_self_register=False)
        _start(ui2)
        s, _, _ = _http(
            "GET", "http://127.0.0.1:20111/register")
        # Should redirect to /login (urllib follows → ends at login page)
        self.assertEqual(s, 200)
        # POST register also blocked
        sess = ui2._login("root", "abcd1234")
        with self.assertRaises(UIError) as ctx:
            ui2._register("newbie", "abcd1234")
        self.assertEqual(ctx.exception.status, 403)
        _ = sess  # unused


class LiveAdminUserMgmtTests(unittest.TestCase):
    PORT = 20200

    @classmethod
    def setUpClass(cls):
        os.environ.pop("SHABD_UI_BOOTSTRAP_PASSWORD", None)
        cls.app = _new_app("admin-mgmt")
        cls.store = UserStore(cls.app, **_FAST)
        cls.ui = UIServer(
            cls.app, bind="127.0.0.1", port=cls.PORT,
            users=cls.store)
        _start(cls.ui)
        cls.base = f"http://127.0.0.1:{cls.PORT}"
        cls.root = cls.ui._register("root", "rootpw1234")

    def test_admin_create_user_via_http(self):
        s, _, raw = _http(
            "POST", f"{self.base}/api/users/create",
            headers={"Content-Type": "application/json",
                      "X-CSRF": self.root.csrf},
            body=json.dumps({
                "username": "amit",
                "password": "abcd1234",
                "roles": ["admin", "user"],
            }).encode(),
            cookies={"shabd_sid": self.root.sid},
        )
        self.assertEqual(s, 200)
        out = json.loads(raw)
        self.assertTrue(out["ok"])
        self.assertEqual(out["username"], "amit")
        self.assertEqual(sorted(out["roles"]), ["admin", "user"])

    def test_admin_set_roles_via_http(self):
        self.store.register("rohan", "abcd1234")
        s, _, raw = _http(
            "POST", f"{self.base}/api/users/rohan/roles",
            headers={"Content-Type": "application/json",
                      "X-CSRF": self.root.csrf},
            body=json.dumps({"roles": ["admin"]}).encode(),
            cookies={"shabd_sid": self.root.sid},
        )
        self.assertEqual(s, 200)
        self.assertEqual(self.store.get("rohan").roles, ["admin"])

    def test_admin_reset_password(self):
        self.store.register("priya", "oldpasswd")
        s, _, raw = _http(
            "POST", f"{self.base}/api/users/priya/password",
            headers={"Content-Type": "application/json",
                      "X-CSRF": self.root.csrf},
            body=json.dumps({"password": "newpasswd"}).encode(),
            cookies={"shabd_sid": self.root.sid},
        )
        self.assertEqual(s, 200)
        with self.assertRaises(UserError):
            self.store.login("priya", "oldpasswd")
        self.store.login("priya", "newpasswd")

    def test_super_delete_user(self):
        self.store.register("temp", "abcd1234")
        s, _, _ = _http(
            "POST", f"{self.base}/api/users/temp/delete",
            headers={"X-CSRF": self.root.csrf},
            cookies={"shabd_sid": self.root.sid},
        )
        self.assertEqual(s, 200)
        self.assertIsNone(self.store.get("temp"))

    def test_admin_create_blocks_non_admin(self):
        self.store.register("noobie", "abcd1234")
        sess = self.ui._login("noobie", "abcd1234")
        s, _, _ = _http(
            "POST", f"{self.base}/api/users/create",
            headers={"Content-Type": "application/json",
                      "X-CSRF": sess.csrf},
            body=json.dumps({
                "username": "x", "password": "abcd1234"}).encode(),
            cookies={"shabd_sid": sess.sid},
        )
        self.assertEqual(s, 403)

    def test_users_api_lists_store_entries(self):
        s, _, raw = _http(
            "GET", f"{self.base}/api/users",
            cookies={"shabd_sid": self.root.sid},
        )
        self.assertEqual(s, 200)
        payload = json.loads(raw)
        self.assertTrue(payload["store_enabled"])
        names = {u["username"] for u in payload["store"]}
        self.assertIn("root", names)


# ---------------------------------------------------------------------------
# COMPLEX — full lifecycle
# ---------------------------------------------------------------------------

class FullIdentityLifecycleTests(unittest.TestCase):
    """End-to-end story: bootstrap → invite team → promote → reset →
    delete; assert every action lives in the Grimoire chain in order
    and that the chain still verifies after the storm."""

    PORT = 20300

    @classmethod
    def setUpClass(cls):
        os.environ.pop("SHABD_UI_BOOTSTRAP_PASSWORD", None)
        cls.app = _new_app("lifecycle")
        cls.store = UserStore(cls.app, **_FAST)
        cls.ui = UIServer(
            cls.app, bind="127.0.0.1", port=cls.PORT,
            users=cls.store)
        _start(cls.ui)

    def test_full_journey(self):
        # 1) First-run register → superuser (or login if already there
        # from a prior test-method in this class)
        if self.store.get("boss") is None:
            boss = self.ui._register("boss", "abcd1234")
        else:
            boss = self.ui._login("boss", "abcd1234")
        self.assertIn("superuser", boss.roles)

        # 2) boss creates a user
        self.ui.admin_create_user(
            boss, username="emp1", password="emppass1",
            roles=["user"])
        self.assertEqual(self.store.get("emp1").roles, ["user"])

        # 3) boss promotes emp1 to admin
        self.ui.admin_set_roles(
            boss, username="emp1", roles=["admin", "user"])
        self.assertIn("admin", self.store.get("emp1").roles)

        # 4) emp1 logs in
        emp1_sess = self.ui._login("emp1", "emppass1")
        self.assertTrue(emp1_sess.is_admin())

        # 5) boss resets emp1 password
        self.ui.admin_reset_password(
            boss, username="emp1", new_password="brand-new-1")
        with self.assertRaises(UIError):
            self.ui._login("emp1", "emppass1")
        self.ui._login("emp1", "brand-new-1")

        # 6) boss deletes emp1
        self.ui.admin_delete_user(boss, username="emp1")
        self.assertIsNone(self.store.get("emp1"))

        # 7) chain still intact and identity events appear in order
        v = self.app.grimoire.verify()
        self.assertTrue(v["ok"], v)
        identity_events = [
            p["spell"]
            for p in self.app.grimoire.pages(limit=10_000)
            if p["spell"].startswith("__user_event:")
            or p["spell"].startswith("__ui_admin:")
        ]
        # boss registered + create_user + register(emp1) +
        # set_roles + login_ok + reset + set_password + delete
        for needed in (
                "__user_event:register",
                "__ui_admin:create_user",
                "__ui_admin:set_roles",
                "__user_event:login_ok",
                "__user_event:set_password",
                "__user_event:delete",
                "__ui_admin:delete_user"):
            self.assertIn(needed, identity_events,
                          f"missing event {needed}")

    def test_cannot_delete_self(self):
        # Self-contained: register if first run, else login.
        if self.store.get("boss") is None:
            boss = self.ui._register("boss", "abcd1234")
        else:
            boss = self.ui._login("boss", "abcd1234")
        with self.assertRaises(UIError) as ctx:
            self.ui.admin_delete_user(boss, username="boss")
        self.assertEqual(ctx.exception.status, 400)


def main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for cls in (HashHelpersTests, UserDataclassTests,
                UserStoreBasicsTests, UserStoreAuditChainTests,
                LiveRegisterLoginTests, LiveAdminUserMgmtTests,
                FullIdentityLifecycleTests):
        suite.addTests(loader.loadTestsFromTestCase(cls))
    return 0 if unittest.TextTestRunner(
        verbosity=2, stream=sys.stdout).run(suite).wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
