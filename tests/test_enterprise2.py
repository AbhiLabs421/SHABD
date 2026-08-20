"""
Tests for shabd_enterprise (HSM, RBAC, SQLite chain, OTLP, cluster,
mTLS config) and shabd_packs (sanctions, regtech, pretrade, ccil).

Run:
    python tests/test_enterprise2.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shabd import SHABD, ConjureError, ForbiddenError  # noqa: E402
from shabd_enterprise import (  # noqa: E402
    ClusterPeer,
    EnvKeyProvider,
    FileKeyProvider,
    HSMKeyProvider,
    RBACPolicyEngine,
    SeparationOfDutiesPolicy,
    SQLiteGrimoirePersistence,
    install_enterprise,
)
from shabd_packs import ccil, pretrade, regtech, sanctions  # noqa: E402


def _app() -> SHABD:
    return SHABD("ent2", secret="x" * 32, require_auth=False)


def _auth_app(name: str = "ent2-auth") -> tuple:
    """Return (app, token_factory). The factory issues a fresh token on
    every call so the built-in replay protection (`jti` cache) does not
    reject repeat invocations from the same test."""
    app = SHABD(name, secret="x" * 32, require_auth=True)

    def mint() -> str:
        return app.issue_token("test-user", scopes=["*"])

    return app, mint


# ---------------------------------------------------------------------------
# Key providers
# ---------------------------------------------------------------------------
class KeyProviderTests(unittest.TestCase):
    def test_env_provider_round_trip(self):
        os.environ["SHABD_TEST_KEY"] = "deadbeef" * 8
        kp = EnvKeyProvider(active_env="SHABD_TEST_KEY",
                            fallback_env="SHABD_TEST_KEY_OLD")
        self.assertEqual(kp.get_signing_key(), bytes.fromhex("deadbeef" * 8))
        self.assertEqual(len(kp.get_verifying_keys()), 1)
        del os.environ["SHABD_TEST_KEY"]

    def test_env_with_fallback(self):
        os.environ["SHABD_TEST_KEY"] = "aa" * 32
        os.environ["SHABD_TEST_KEY_OLD"] = "bb" * 32
        kp = EnvKeyProvider("SHABD_TEST_KEY", "SHABD_TEST_KEY_OLD")
        self.assertEqual(len(kp.get_verifying_keys()), 2)
        del os.environ["SHABD_TEST_KEY"]
        del os.environ["SHABD_TEST_KEY_OLD"]

    def test_file_provider(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "current"), "w") as f:
                f.write("11" * 32)
            kp = FileKeyProvider(d)
            self.assertEqual(kp.get_signing_key(), bytes.fromhex("11" * 32))

    def test_hsm_provider_falls_back(self):
        os.environ["SHABD_TEST_HSM_FALLBACK"] = "22" * 32
        kp = HSMKeyProvider(slot_id=0, label="x", pin="x",
                            fallback_env="SHABD_TEST_HSM_FALLBACK")
        # pkcs11 isn't installed; we expect the fallback path to win.
        self.assertEqual(kp.get_signing_key(), bytes.fromhex("22" * 32))
        del os.environ["SHABD_TEST_HSM_FALLBACK"]


# ---------------------------------------------------------------------------
# RBAC + Separation of Duties
# ---------------------------------------------------------------------------
class RBACTests(unittest.TestCase):
    def test_role_allow_works(self):
        app = SHABD("rbac", secret="x" * 32, require_auth=True)

        @app.spell
        def trade(symbol: str) -> dict:
            return {"ok": True}

        rbac = RBACPolicyEngine()
        rbac.add_rule("trader", allow=["trade"])
        rbac.install_on(app)

        token = app.issue_token("alice", scopes=["trader"])
        self.assertEqual(
            app.invoke("trade", {"symbol": "X"}, token=token)["ok"], True
        )

    def test_no_role_denies(self):
        app = SHABD("rbac2", secret="x" * 32, require_auth=True)

        @app.spell
        def trade(symbol: str) -> dict:
            return {"ok": True}

        rbac = RBACPolicyEngine()
        rbac.add_rule("trader", allow=["trade"])
        rbac.install_on(app)
        token = app.issue_token("eve", scopes=["guest"])
        with self.assertRaises(ForbiddenError):
            app.invoke("trade", {"symbol": "X"}, token=token)

    def test_prefix_allow(self):
        app = SHABD("rbac3", secret="x" * 32, require_auth=True)

        @app.spell
        def finance_calc_gst(amount: float) -> dict:
            return {"gst": amount * 0.18}

        rbac = RBACPolicyEngine()
        rbac.add_rule("finance", allow_prefixes=["finance_*"])
        rbac.install_on(app)
        token = app.issue_token("bob", scopes=["finance"])
        self.assertAlmostEqual(
            app.invoke("finance_calc_gst", {"amount": 100.0},
                       token=token)["gst"], 18.0,
        )

    def test_separation_of_duties(self):
        app = SHABD("sod", secret="x" * 32, require_auth=True)

        @app.spell
        def wire_transfer(amount: float, approver_token: str = "") -> dict:
            return {"ok": True, "amount": amount}

        SeparationOfDutiesPolicy(app, sensitive_spells=["wire_transfer"])

        def alice() -> str:
            return app.issue_token("alice", scopes=["*"])

        def bob() -> str:
            return app.issue_token("bob", scopes=["*"])

        # Without approver — denied
        with self.assertRaises(ForbiddenError):
            app.invoke("wire_transfer", {"amount": 100}, token=alice())
        # Same-subject approver — denied
        with self.assertRaises(ForbiddenError):
            app.invoke("wire_transfer",
                       {"amount": 100, "approver_token": alice()},
                       token=alice())
        # Different-subject approver — allowed
        self.assertEqual(
            app.invoke("wire_transfer",
                       {"amount": 100, "approver_token": bob()},
                       token=alice())["ok"], True
        )


# ---------------------------------------------------------------------------
# SQLite-backed Grimoire
# ---------------------------------------------------------------------------
class SQLitePersistenceTests(unittest.TestCase):
    def test_survives_restart_and_loads_chain(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "audit.db")

            app1 = _app()

            @app1.spell
            def echo(x: str) -> str:
                return x

            SQLiteGrimoirePersistence(db).install_on(app1)
            app1.invoke("echo", {"x": "a"})
            app1.invoke("echo", {"x": "b"})
            self.assertTrue(app1.grimoire.verify()["ok"])

            app2 = _app()

            @app2.spell  # noqa: F811
            def echo(x: str) -> str:  # noqa: F811
                return x

            SQLiteGrimoirePersistence(db).install_on(app2)
            self.assertEqual(len(app2.grimoire._pages), 2)
            self.assertTrue(app2.grimoire.verify()["ok"])


# ---------------------------------------------------------------------------
# Cluster peer wiring
# ---------------------------------------------------------------------------
class ClusterTests(unittest.TestCase):
    def test_cluster_push_swallows_unreachable_peer(self):
        app = _app()

        @app.spell
        def hello() -> dict:
            return {"ok": True}

        ClusterPeer(["http://127.0.0.1:1"],
                    hmac_secret=b"x" * 32).install_on(app)
        # Even if the peer is unreachable, the call must succeed.
        self.assertEqual(app.invoke("hello", {})["ok"], True)


# ---------------------------------------------------------------------------
# Sanctions pack
# ---------------------------------------------------------------------------
class SanctionsPackTests(unittest.TestCase):
    def test_screening_returns_hits_or_clear(self):
        app, tok = _auth_app("sanc")
        sanctions.install(app)
        clear = app.invoke("screen_party", {"name": "Plain Citizen"}, token=tok())
        self.assertTrue(clear["clear"])
        hit = app.invoke("screen_party", {"name": "Vladimir Putin"}, token=tok())
        self.assertFalse(hit["clear"])
        self.assertIn("OFAC", hit["matched_lists"])

    def test_list_status(self):
        app, tok = _auth_app("sanc2")
        sanctions.install(app)
        s = app.invoke("list_status", {}, token=tok())
        self.assertIn("lists", s)
        self.assertIn("loaded_at", s)


# ---------------------------------------------------------------------------
# RegTech pack
# ---------------------------------------------------------------------------
class RegTechPackTests(unittest.TestCase):
    def test_str_requires_summary(self):
        app, tok = _auth_app("rg1")
        regtech.install(app)
        with self.assertRaises(ConjureError):
            app.invoke("generate_str", {
                "case_id": "C-1", "summary": "",
                "parties": ["x"], "amount_inr": 100.0,
            }, token=tok())

    def test_digital_lending_packet_redacts_aadhaar(self):
        app, tok = _auth_app("rg2")
        regtech.install(app)
        packet = app.invoke("generate_digital_lending_audit", {
            "loan_id": "L-1",
            "applicant_aadhaar": "123456789012",
            "model_version": "v2.3",
            "decision": "APPROVE",
            "top_factors": ["income", "credit_history", "DTI"],
            "requested_inr": "100000.00 INR",
        }, token=tok())
        masked = packet["applicant_aadhaar_masked"]
        self.assertTrue(masked.startswith("12"))
        self.assertTrue(masked.endswith("12"))
        self.assertNotIn("123456789012", masked)


# ---------------------------------------------------------------------------
# Pre-trade pack
# ---------------------------------------------------------------------------
class PreTradePackTests(unittest.TestCase):
    def test_position_limit_breach(self):
        app, tok = _auth_app("pt1")
        book = pretrade.install(app)
        book.set_position_limit("alpha", "RELIANCE", 10)
        app.invoke("check_pre_trade", {
            "strategy": "alpha", "symbol": "RELIANCE", "side": "buy",
            "qty": 7, "limit_price_inr": 2500.0,
        }, token=tok())
        with self.assertRaises(ConjureError):
            app.invoke("check_pre_trade", {
                "strategy": "alpha", "symbol": "RELIANCE", "side": "buy",
                "qty": 5, "limit_price_inr": 2500.0,
            }, token=tok())

    def test_notional_limit_breach(self):
        app, tok = _auth_app("pt2")
        book = pretrade.install(app)
        book.set_notional_limit("beta", 10_000.0)
        with self.assertRaises(ConjureError):
            app.invoke("check_pre_trade", {
                "strategy": "beta", "symbol": "X", "side": "buy",
                "qty": 1, "limit_price_inr": 100_000.0,
            }, token=tok())


# ---------------------------------------------------------------------------
# CCIL pack
# ---------------------------------------------------------------------------
class CCILPackTests(unittest.TestCase):
    def test_book_repo_requires_isin(self):
        app, tok = _auth_app("cc1")
        ccil.install(app)
        with self.assertRaises(ConjureError):
            app.invoke("book_repo", {
                "counterparty_mid": "MBR-2", "isin": "not-an-isin",
                "qty": 100, "rate_pct": 7.5, "tenor_days": 1,
            }, token=tok())

    def test_book_repo_happy_path(self):
        app, tok = _auth_app("cc2")
        ccil.install(app)
        r = app.invoke("book_repo", {
            "counterparty_mid": "MBR-2", "isin": "IN0020230015",
            "qty": 100, "rate_pct": 7.5, "tenor_days": 1,
        }, token=tok())
        self.assertEqual(r["status"], "accepted")
        self.assertTrue(r["ccil_ref"].startswith("REPO-"))


# ---------------------------------------------------------------------------
# install_enterprise wiring
# ---------------------------------------------------------------------------
class InstallEnterpriseTests(unittest.TestCase):
    def test_compose_sqlite_plus_rbac(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "a.db")
            app = SHABD("ent", secret="x" * 32, require_auth=True)

            @app.spell
            def trade(symbol: str) -> dict:
                return {"ok": True}

            rbac = RBACPolicyEngine()
            rbac.add_rule("trader", allow=["trade"])

            install_enterprise(
                app,
                sqlite_store=SQLiteGrimoirePersistence(db),
                rbac=rbac,
            )
            tok = app.issue_token("alice", scopes=["trader"])
            self.assertEqual(
                app.invoke("trade", {"symbol": "X"}, token=tok)["ok"], True
            )
            self.assertTrue(app.grimoire.verify()["ok"])


def main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for cls in (KeyProviderTests, RBACTests, SQLitePersistenceTests,
                ClusterTests, SanctionsPackTests, RegTechPackTests,
                PreTradePackTests, CCILPackTests, InstallEnterpriseTests):
        suite.addTests(loader.loadTestsFromTestCase(cls))
    runner = unittest.TextTestRunner(verbosity=2, stream=sys.stdout)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
