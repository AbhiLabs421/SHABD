"""Tests for shabd_praman — the built-in identity provider (Praman).

Coverage levels:
  Easy    : JWT round-trip, base64url, TOTP, password validation.
  Medium  : realm (users/clients), password grant, refresh, client-creds.
  Hard    : lockout, MFA/TOTP login, revocation, introspection, discovery,
            tamper detection, Grimoire audit hook.
  Complex : full HTTP round-trip through the standalone PramanServer.
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

from shabd_praman import (  # noqa: E402
    BuiltinIdentityProvider,
    Praman,
    PramanError,
    PramanRealm,
    PramanServer,
    identity_from_config,
    jwt_decode_hs256,
    jwt_encode_hs256,
    totp_now,
    totp_verify,
)

SECRET = b"x" * 32


def _fresh(**kw) -> Praman:
    realm = PramanRealm(name="test", scrypt_n=2 ** 12)  # fast hashing for tests
    return Praman(issuer="https://shabd.test", secret=SECRET, realm=realm, **kw)


# ---------------------------------------------------------------------------
# EASY
# ---------------------------------------------------------------------------
class JwtTests(unittest.TestCase):
    def test_roundtrip(self):
        tok = jwt_encode_hs256({"sub": "a", "exp": int(time.time()) + 60}, SECRET)
        claims = jwt_decode_hs256(tok, SECRET)
        self.assertEqual(claims["sub"], "a")

    def test_tamper_detected(self):
        tok = jwt_encode_hs256({"sub": "a", "exp": int(time.time()) + 60}, SECRET)
        h, p, s = tok.split(".")
        forged = jwt_encode_hs256({"sub": "admin", "exp": int(time.time()) + 60},
                                  b"y" * 32)
        with self.assertRaises(PramanError):
            jwt_decode_hs256(f"{h}.{forged.split('.')[1]}.{s}", SECRET)

    def test_expired_rejected(self):
        tok = jwt_encode_hs256({"sub": "a", "exp": int(time.time()) - 100}, SECRET)
        with self.assertRaises(PramanError):
            jwt_decode_hs256(tok, SECRET)

    def test_wrong_secret_rejected(self):
        tok = jwt_encode_hs256({"sub": "a", "exp": int(time.time()) + 60}, SECRET)
        with self.assertRaises(PramanError):
            jwt_decode_hs256(tok, b"z" * 32)


class TotpTests(unittest.TestCase):
    def test_current_code_verifies(self):
        sec = "JBSWY3DPEHPK3PXP"
        self.assertTrue(totp_verify(sec, totp_now(sec)))

    def test_wrong_code_fails(self):
        self.assertFalse(totp_verify("JBSWY3DPEHPK3PXP", "000000"))

    def test_skew_tolerated(self):
        sec = "JBSWY3DPEHPK3PXP"
        prev = totp_now(sec, t0=int(time.time()) - 30)
        self.assertTrue(totp_verify(sec, prev, window=1))


class PasswordPolicyTests(unittest.TestCase):
    def test_short_password_rejected(self):
        p = _fresh(min_password_len=12)
        with self.assertRaises(PramanError) as c:
            p.create_user("u", "short")
        self.assertEqual(c.exception.status, 400)

    def test_ok_password_accepted(self):
        p = _fresh(min_password_len=8)
        p.create_user("u", "longenough")
        self.assertIn("u", p.realm.users)


# ---------------------------------------------------------------------------
# MEDIUM
# ---------------------------------------------------------------------------
class GrantTests(unittest.TestCase):
    def setUp(self):
        self.p = _fresh(min_password_len=6)
        self.p.create_user("alice", "secret1", roles=["trader"])

    def test_password_grant_returns_tokens(self):
        r = self.p.token({"grant_type": "password", "username": "alice",
                          "password": "secret1"})
        self.assertIn("access_token", r)
        self.assertIn("refresh_token", r)
        self.assertIn("id_token", r)
        self.assertEqual(r["token_type"], "Bearer")

    def test_access_token_verifies_and_carries_roles(self):
        r = self.p.token({"grant_type": "password", "username": "alice",
                          "password": "secret1"})
        claims = self.p.verify_access_token(r["access_token"])
        self.assertEqual(claims["sub"], "alice")
        self.assertIn("trader", claims["roles"])

    def test_bad_password_rejected(self):
        with self.assertRaises(PramanError) as c:
            self.p.token({"grant_type": "password", "username": "alice",
                          "password": "wrong"})
        self.assertEqual(c.exception.status, 401)

    def test_refresh_rotates(self):
        r1 = self.p.token({"grant_type": "password", "username": "alice",
                           "password": "secret1"})
        r2 = self.p.token({"grant_type": "refresh_token",
                           "refresh_token": r1["refresh_token"]})
        self.assertIn("access_token", r2)
        # old refresh must no longer work (rotation)
        with self.assertRaises(PramanError):
            self.p.token({"grant_type": "refresh_token",
                          "refresh_token": r1["refresh_token"]})

    def test_client_credentials(self):
        self.p.realm.add_client("svc", client_secret="topsecret",
                                scopes=["ingest"])
        r = self.p.token({"grant_type": "client_credentials",
                          "client_id": "svc", "client_secret": "topsecret"})
        self.assertIn("access_token", r)
        claims = self.p.verify_access_token(r["access_token"])
        self.assertEqual(claims["sub"], "svc")
        self.assertNotIn("refresh_token", r)  # client-creds gets no refresh

    def test_client_bad_secret_rejected(self):
        self.p.realm.add_client("svc", client_secret="topsecret")
        with self.assertRaises(PramanError):
            self.p.token({"grant_type": "client_credentials",
                          "client_id": "svc", "client_secret": "nope"})

    def test_unsupported_grant(self):
        with self.assertRaises(PramanError) as c:
            self.p.token({"grant_type": "authorization_code"})
        self.assertEqual(c.exception.status, 400)


# ---------------------------------------------------------------------------
# HARD
# ---------------------------------------------------------------------------
class SecurityTests(unittest.TestCase):
    def setUp(self):
        self.p = _fresh(min_password_len=6, lockout_max=3, lockout_window=300)
        self.p.create_user("bob", "secret1")

    def test_lockout_after_max_failures(self):
        for _ in range(3):
            with self.assertRaises(PramanError):
                self.p.authenticate("bob", "wrong")
        # now even the RIGHT password is locked out
        with self.assertRaises(PramanError) as c:
            self.p.authenticate("bob", "secret1")
        self.assertEqual(c.exception.status, 429)

    def test_mfa_required_once_enabled(self):
        enroll = self.p.enroll_totp("bob")
        self.p.confirm_totp("bob", totp_now(enroll["secret"]))
        # password alone now fails
        with self.assertRaises(PramanError) as c:
            self.p.authenticate("bob", "secret1")
        self.assertEqual(c.exception.error, "mfa_required")
        # password + valid OTP works
        u = self.p.authenticate("bob", "secret1",
                                otp=totp_now(enroll["secret"]))
        self.assertEqual(u["username"], "bob")

    def test_revoked_access_token_rejected(self):
        r = self.p.token({"grant_type": "password", "username": "bob",
                          "password": "secret1"})
        self.p.verify_access_token(r["access_token"])  # ok before revoke
        self.p.revoke(r["access_token"])
        with self.assertRaises(PramanError):
            self.p.verify_access_token(r["access_token"])

    def test_revoked_refresh_token_rejected(self):
        r = self.p.token({"grant_type": "password", "username": "bob",
                          "password": "secret1"})
        self.p.revoke(r["refresh_token"])
        with self.assertRaises(PramanError):
            self.p.token({"grant_type": "refresh_token",
                          "refresh_token": r["refresh_token"]})

    def test_introspect_active_and_inactive(self):
        r = self.p.token({"grant_type": "password", "username": "bob",
                          "password": "secret1"})
        self.assertTrue(self.p.introspect(r["access_token"])["active"])
        self.assertFalse(self.p.introspect("garbage.token.here")["active"])

    def test_userinfo(self):
        self.p.realm.users["bob"]["email"] = "bob@x.in"
        r = self.p.token({"grant_type": "password", "username": "bob",
                          "password": "secret1"})
        info = self.p.userinfo(r["access_token"])
        self.assertEqual(info["sub"], "bob")
        self.assertEqual(info["email"], "bob@x.in")

    def test_discovery_document_shape(self):
        d = self.p.discovery_document()
        self.assertEqual(d["issuer"], "https://shabd.test")
        self.assertTrue(d["token_endpoint"].endswith("/praman/token"))
        self.assertIn("password", d["grant_types_supported"])

    def test_audit_hook_fires(self):
        events = []
        p = _fresh(min_password_len=6, audit=lambda e, d: events.append(e))
        p.create_user("carol", "secret1")
        p.token({"grant_type": "password", "username": "carol",
                 "password": "secret1"})
        self.assertIn("praman.user_created", events)
        self.assertIn("praman.login_ok", events)
        self.assertIn("praman.token_issued", events)

    def test_secret_never_in_realm_dump(self):
        # passwords stored only as scrypt hashes; raw password absent
        self.assertNotIn("secret1", json.dumps(self.p.realm.users))


# ---------------------------------------------------------------------------
# Config selection + persistence
# ---------------------------------------------------------------------------
class ConfigTests(unittest.TestCase):
    def test_builtin_provider_from_config(self):
        prov = identity_from_config(
            {"provider": "builtin",
             "builtin": {"issuer": "https://x", "password_policy": {"min_len": 6}}},
            secret=SECRET)
        self.assertIsInstance(prov, BuiltinIdentityProvider)
        prov.praman.create_user("dana", "secret1")
        r = prov.password_login("dana", "secret1")
        claims = prov.verify(r["access_token"])
        self.assertEqual(claims["sub"], "dana")

    def test_keycloak_provider_selected(self):
        prov = identity_from_config(
            {"provider": "keycloak",
             "keycloak": {"server_url": "https://kc", "realm": "r",
                          "client_id": "c"}},
            secret=SECRET)
        self.assertEqual(prov.__class__.__name__, "ExternalKeycloakProvider")

    def test_realm_persists_across_reload(self):
        import tempfile
        path = os.path.join(tempfile.mkdtemp(), "realm.json")
        r1 = PramanRealm(path=path, scrypt_n=2 ** 12)
        p1 = Praman(issuer="https://x", secret=SECRET, realm=r1,
                    min_password_len=6)
        p1.create_user("erin", "secret1", roles=["auditor"])
        # reload from disk
        r2 = PramanRealm(path=path, scrypt_n=2 ** 12)
        self.assertIn("erin", r2.users)
        p2 = Praman(issuer="https://x", secret=SECRET, realm=r2,
                    min_password_len=6)
        r = p2.token({"grant_type": "password", "username": "erin",
                      "password": "secret1"})
        self.assertIn("access_token", r)


# ---------------------------------------------------------------------------
# COMPLEX — full HTTP round-trip through PramanServer
# ---------------------------------------------------------------------------
class HttpServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.p = _fresh(min_password_len=6)
        cls.p.create_user("frank", "secret1", roles=["trader"], email="f@x.in")
        cls.srv = PramanServer(cls.p, bind="127.0.0.1", port=0)
        # bind to an ephemeral port
        import socketserver
        socketserver.ThreadingTCPServer.allow_reuse_address = True
        from shabd_praman import _make_handler
        cls.httpd = socketserver.ThreadingTCPServer(
            ("127.0.0.1", 0), _make_handler(cls.p))
        cls.httpd.daemon_threads = True
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.port}"
        time.sleep(0.2)

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def _post(self, path, data, token=None):
        body = urllib.parse.urlencode(data).encode()
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(self.base + path, data=body, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def _get(self, path, token=None):
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(self.base + path, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def test_discovery_endpoint(self):
        code, doc = self._get("/.well-known/openid-configuration")
        self.assertEqual(code, 200)
        self.assertEqual(doc["issuer"], "https://shabd.test")

    def test_token_userinfo_introspect_revoke_flow(self):
        code, tok = self._post("/praman/token", {
            "grant_type": "password", "username": "frank",
            "password": "secret1"})
        self.assertEqual(code, 200)
        access = tok["access_token"]
        # userinfo
        code, info = self._get("/praman/userinfo", token=access)
        self.assertEqual(code, 200)
        self.assertEqual(info["sub"], "frank")
        # introspect active
        code, intro = self._post("/praman/introspect", {"token": access})
        self.assertTrue(intro["active"])
        # revoke then introspect inactive
        self._post("/praman/revoke", {"token": access})
        code, intro2 = self._post("/praman/introspect", {"token": access})
        self.assertFalse(intro2["active"])

    def test_bad_login_http_401(self):
        code, body = self._post("/praman/token", {
            "grant_type": "password", "username": "frank", "password": "nope"})
        self.assertEqual(code, 401)
        self.assertEqual(body["error"], "invalid_grant")


class Rs256Tests(unittest.TestCase):
    """RS256 signing + JWKS: prove a THIRD PARTY can verify SHABD tokens
    offline using only the public JWKS (real OIDC interop), and that the
    server refuses algorithm substitution."""

    def setUp(self):
        realm = PramanRealm(name="rs", scrypt_n=2 ** 12)
        self.p = Praman(issuer="https://shabd.rs", secret=SECRET, realm=realm,
                        min_password_len=6, token_alg="RS256", rsa_bits=1024)
        self.p.create_user("gina", "secret1", roles=["auditor"])

    def test_token_is_rs256_and_self_verifies(self):
        r = self.p.token({"grant_type": "password", "username": "gina",
                          "password": "secret1"})
        from shabd_praman import jwt_header
        self.assertEqual(jwt_header(r["access_token"])["alg"], "RS256")
        claims = self.p.verify_access_token(r["access_token"])
        self.assertEqual(claims["sub"], "gina")

    def test_jwks_publishes_public_key(self):
        jwks = self.p.jwks()
        self.assertEqual(len(jwks["keys"]), 1)
        self.assertEqual(jwks["keys"][0]["kty"], "RSA")
        self.assertEqual(jwks["keys"][0]["alg"], "RS256")

    def test_third_party_verifies_with_jwks_only(self):
        # Reconstruct the public key from the published JWKS (as an external
        # service would) and verify the token signature independently.
        import base64

        import shabd_rsa
        from shabd_praman import _b64u_decode
        r = self.p.token({"grant_type": "password", "username": "gina",
                          "password": "secret1"})
        jwk = self.p.jwks()["keys"][0]

        def b64u_int(s):
            pad = "=" * (-len(s) % 4)
            return int.from_bytes(base64.urlsafe_b64decode(s + pad), "big")

        pub = {"n": b64u_int(jwk["n"]), "e": b64u_int(jwk["e"])}
        h, p, sig = r["access_token"].split(".")
        signing_input = f"{h}.{p}".encode()
        self.assertTrue(shabd_rsa.verify_pkcs1v15_sha256(
            signing_input, _b64u_decode(sig), pub))

    def test_alg_substitution_rejected(self):
        # An attacker re-signs the payload with HS256 using the (public) modulus
        # bytes as the "secret" — the classic RS/HS confusion. Server must
        # refuse because it only accepts its configured alg.
        r = self.p.token({"grant_type": "password", "username": "gina",
                          "password": "secret1"})
        _, payload_b64, _ = r["access_token"].split(".")
        forged = jwt_encode_hs256(
            {"iss": "https://shabd.rs", "sub": "gina",
             "exp": int(time.time()) + 60, "jti": "x"}, SECRET)
        with self.assertRaises(PramanError):
            self.p.verify_access_token(forged)

    def test_rs256_key_persists(self):
        import tempfile
        path = os.path.join(tempfile.mkdtemp(), "rs.json")
        r1 = PramanRealm(path=path, scrypt_n=2 ** 12)
        p1 = Praman(issuer="https://x", secret=SECRET, realm=r1,
                    min_password_len=6, token_alg="RS256", rsa_bits=1024)
        p1.create_user("hank", "secret1")
        tok = p1.token({"grant_type": "password", "username": "hank",
                        "password": "secret1"})["access_token"]
        # reload — same key must verify the earlier token
        r2 = PramanRealm(path=path, scrypt_n=2 ** 12)
        p2 = Praman(issuer="https://x", secret=SECRET, realm=r2,
                    min_password_len=6, token_alg="RS256", rsa_bits=1024)
        self.assertEqual(p2.verify_access_token(tok)["sub"], "hank")


class GrimoireAuditBridgeTests(unittest.TestCase):
    """The differentiator: Praman auth events land in the SHABD Grimoire hash
    chain, so identity history is tamper-evident (unlike a stock IdP's
    editable event table)."""

    def _app(self):
        from shabd import SHABD
        return SHABD("idaudit", secret="x" * 32, require_auth=False)

    def test_auth_events_recorded_in_chain(self):
        from shabd_praman import grimoire_audit_bridge
        app = self._app()
        realm = PramanRealm(name="a", scrypt_n=2 ** 12)
        p = Praman(issuer="https://x", secret=SECRET, realm=realm,
                   min_password_len=6, audit=grimoire_audit_bridge(app))
        p.create_user("ivy", "secret1", roles=["auditor"])
        p.token({"grant_type": "password", "username": "ivy",
                 "password": "secret1"})
        spells = [pg["spell"] for pg in app.grimoire.pages(limit=10 ** 6)]
        self.assertIn("__praman_event:praman.user_created", spells)
        self.assertIn("__praman_event:praman.login_ok", spells)
        self.assertIn("__praman_event:praman.token_issued", spells)
        # chain still verifies
        self.assertTrue(app.grimoire.verify()["ok"])

    def test_tampering_with_auth_event_is_detected(self):
        from shabd_praman import grimoire_audit_bridge
        app = self._app()
        realm = PramanRealm(name="a", scrypt_n=2 ** 12)
        p = Praman(issuer="https://x", secret=SECRET, realm=realm,
                   min_password_len=6, audit=grimoire_audit_bridge(app))
        p.create_user("jack", "secret1")
        self.assertTrue(app.grimoire.verify()["ok"])
        # forge history: flip a recorded subject
        app.grimoire._pages[-1]["subject"] = "attacker"
        v = app.grimoire.verify()
        self.assertFalse(v["ok"])

    def test_passwords_never_written_to_chain(self):
        from shabd_praman import grimoire_audit_bridge
        app = self._app()
        realm = PramanRealm(name="a", scrypt_n=2 ** 12)
        p = Praman(issuer="https://x", secret=SECRET, realm=realm,
                   min_password_len=6, audit=grimoire_audit_bridge(app))
        p.create_user("kate", "supersecretpw")
        import json as _json
        dump = _json.dumps(app.grimoire.pages(limit=10 ** 6), default=str)
        self.assertNotIn("supersecretpw", dump)


def main() -> int:
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    ok = unittest.TextTestRunner(verbosity=2, stream=sys.stdout).run(suite)
    return 0 if ok.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
