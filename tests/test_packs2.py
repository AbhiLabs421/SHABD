"""
Tests for the second wave of revenue packs (v2.4):

  shabd_packs.aml             — velocity + structuring + BO checks
  shabd_packs.dpdpa           — consent grant / withdraw / verify / DSR
  shabd_packs.surveillance    — wash, spoofing, layering, insider
  shabd_packs.reconciliation  — break detection
  shabd_packs.algo_lifecycle  — algo audit chain
  shabd_packs.reconstruction  — trace / spell / subject reconstruction

Run:
    python tests/test_packs2.py
"""
from __future__ import annotations

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shabd import SHABD, ConjureError  # noqa: E402
from shabd_packs import (  # noqa: E402
    algo_lifecycle,
    aml,
    dpdpa,
    reconciliation,
    reconstruction,
    surveillance,
)


def _auth_app(name: str) -> tuple:
    """(app, token_factory) — fresh token per call avoids jti replay."""
    app = SHABD(name, secret="x" * 32, require_auth=True)

    def mint(scopes=("*",)) -> str:
        return app.issue_token("u", scopes=list(scopes))

    return app, mint


# ---------------------------------------------------------------------------
# AML pack
# ---------------------------------------------------------------------------
class AMLTests(unittest.TestCase):
    def test_velocity_alert_fires(self):
        app, tok = _auth_app("aml1")
        state = aml.install(app, state=aml.AMLState(velocity_window_s=60,
                                                    velocity_count=3))
        # First 2 are clear, the third should fire.
        a1 = app.invoke("record_transaction",
                        {"party": "P", "amount": "100 INR"}, token=tok())
        a2 = app.invoke("record_transaction",
                        {"party": "P", "amount": "100 INR"}, token=tok())
        a3 = app.invoke("record_transaction",
                        {"party": "P", "amount": "100 INR"}, token=tok())
        self.assertTrue(a1["clear"])
        self.assertTrue(a2["clear"])
        self.assertFalse(a3["clear"])
        self.assertEqual(a3["alerts"][0]["rule"], "velocity")
        self.assertGreaterEqual(state.velocity_count_in_window("P"), 3)

    def test_structuring_alert(self):
        app, tok = _auth_app("aml2")
        aml.install(app, state=aml.AMLState(
            velocity_count=999,                  # disable velocity
            structuring_threshold_inr=50_000.0,
            structuring_count=3,
        ))
        for _ in range(2):
            app.invoke("record_transaction",
                       {"party": "Q", "amount": "40000 INR"}, token=tok())
        r = app.invoke("record_transaction",
                       {"party": "Q", "amount": "40000 INR"}, token=tok())
        self.assertFalse(r["clear"])
        self.assertEqual(r["alerts"][0]["rule"], "structuring")

    def test_ubo_check(self):
        app, tok = _auth_app("aml3")
        aml.install(app)
        ok = app.invoke("beneficial_owner_check", {
            "legal_entity": "ACME",
            "declared_ubos": ["Alice", "Bob"],
            "declared_threshold_pct": 25.0,
        }, token=tok())
        self.assertTrue(ok["ok"])
        bad = app.invoke("beneficial_owner_check", {
            "legal_entity": "ACME", "declared_ubos": [],
            "declared_threshold_pct": 25.0,
        }, token=tok())
        self.assertFalse(bad["ok"])


# ---------------------------------------------------------------------------
# DPDPA pack
# ---------------------------------------------------------------------------
class DPDPATests(unittest.TestCase):
    def test_grant_verify_withdraw(self):
        app, tok = _auth_app("dpd1")
        dpdpa.install(app)
        rec = app.invoke("record_consent", {
            "subject_id": "S1", "purpose": "marketing.email",
            "ttl_days": 1.0, "channel": "web",
        }, token=tok())
        self.assertEqual(rec["subject_id"], "S1")
        v = app.invoke("verify_consent",
                       {"subject_id": "S1", "purpose": "marketing.email"},
                       token=tok())
        self.assertTrue(v["consented"])
        app.invoke("withdraw_consent",
                   {"subject_id": "S1", "purpose": "marketing.email"},
                   token=tok())
        v2 = app.invoke("verify_consent",
                        {"subject_id": "S1", "purpose": "marketing.email"},
                        token=tok())
        self.assertFalse(v2["consented"])

    def test_data_subject_request_validation(self):
        app, tok = _auth_app("dpd2")
        dpdpa.install(app)
        with self.assertRaises(ConjureError):
            app.invoke("data_subject_request", {
                "subject_id": "S2", "request_type": "wrong",
            }, token=tok())
        ok = app.invoke("data_subject_request", {
            "subject_id": "S2", "request_type": "access",
        }, token=tok())
        self.assertTrue(ok["request_id"].startswith("DSR-"))
        self.assertGreater(ok["sla_due_at"], ok["received_at"])


# ---------------------------------------------------------------------------
# Surveillance pack
# ---------------------------------------------------------------------------
class SurveillanceTests(unittest.TestCase):
    def test_wash_trade_detection(self):
        app, tok = _auth_app("surv1")
        surveillance.install(app)
        for side in ("buy", "sell"):
            app.invoke("record_order", {
                "symbol": "X", "party": "P1", "side": side,
                "qty": 10, "price": 100.0,
            }, token=tok())
        r = app.invoke("detect_wash_trade",
                       {"symbol": "X", "party": "P1"}, token=tok())
        self.assertTrue(r["wash_suspected"])

    def test_spoofing_detection(self):
        app, tok = _auth_app("surv2")
        surveillance.install(app)
        for _ in range(2):
            app.invoke("record_order", {
                "symbol": "X", "party": "P2", "side": "buy",
                "qty": 10, "price": 100.0, "status": "active",
            }, token=tok())
        for _ in range(8):
            app.invoke("record_order", {
                "symbol": "X", "party": "P2", "side": "buy",
                "qty": 10, "price": 100.0, "status": "cancelled",
            }, token=tok())
        r = app.invoke("detect_spoofing", {"party": "P2"}, token=tok())
        self.assertTrue(r["spoofing_suspected"])

    def test_insider_alert(self):
        app, tok = _auth_app("surv3")
        surveillance.install(app)
        now = time.time()
        app.invoke("declare_upsi_window", {
            "symbol": "ACME", "start_ts": now - 60,
            "end_ts": now + 60,
        }, token=tok())
        app.invoke("record_order", {
            "symbol": "ACME", "party": "InsiderInc",
            "side": "buy", "qty": 100, "price": 50.0,
        }, token=tok())
        r = app.invoke("detect_insider_alert",
                       {"symbol": "ACME", "party": "InsiderInc"},
                       token=tok())
        self.assertTrue(r["insider_suspected"])


# ---------------------------------------------------------------------------
# Reconciliation pack
# ---------------------------------------------------------------------------
class ReconTests(unittest.TestCase):
    def test_break_detection(self):
        app, tok = _auth_app("rec1")
        reconciliation.install(app)
        app.invoke("ingest_external_feed_alias", {}, token=tok()) \
            if False else None   # placeholder, not used
        # Real feeds:
        app.invoke("ingest_ledger_feed", {
            "date": "2026-06-04",
            "rows": [
                {"ref": "T1", "amount_inr": 1000.0},
                {"ref": "T2", "amount_inr": 2000.0},
                {"ref": "T3", "amount_inr": 3000.0},
            ],
        }, token=tok())
        app.invoke("ingest_internal_feed", {
            "date": "2026-06-04",
            "rows": [
                {"ref": "T1", "amount_inr": 1000.0},
                {"ref": "T2", "amount_inr": 2050.0},   # amount mismatch
                {"ref": "T4", "amount_inr": 4000.0},   # only internal
            ],
        }, token=tok())
        rep = app.invoke("reconcile_day",
                         {"date": "2026-06-04"}, token=tok())
        self.assertEqual(rep["matched"], 2)
        only_ext_refs = {r["ref"] for r in rep["only_external"]}
        only_int_refs = {r["ref"] for r in rep["only_internal"]}
        self.assertIn("T3", only_ext_refs)
        self.assertIn("T4", only_int_refs)
        self.assertEqual(len(rep["amount_mismatches"]), 1)
        self.assertGreater(rep["break_count"], 0)


# ---------------------------------------------------------------------------
# Algo lifecycle pack
# ---------------------------------------------------------------------------
class AlgoLifecycleTests(unittest.TestCase):
    def test_full_lifecycle(self):
        app, tok = _auth_app("algo1")
        algo_lifecycle.install(app)
        rec = app.invoke("register_algo", {
            "name": "momentum-v1", "version": "1.0.0",
            "owner": "quant-team",
        }, token=tok())
        algo_id = rec["algo_id"]
        app.invoke("submit_test_results",
                   {"algo_id": algo_id, "test_report": {"sharpe": 1.5}},
                   token=tok())
        app.invoke("request_exchange_approval", {
            "algo_id": algo_id, "exchange_member_id": "NSE-001",
        }, token=tok())
        app.invoke("record_approval_signature", {
            "algo_id": algo_id, "signer": "exchange-officer-1",
            "signature": "sig-abc-123",
        }, token=tok())
        app.invoke("deploy_algo", {"algo_id": algo_id, "env": "production"},
                   token=tok())
        history = app.invoke("algo_history",
                             {"algo_id": algo_id}, token=tok())
        events = [e["event"] for e in history["events"]]
        self.assertIn("registered", events)
        self.assertIn("tested", events)
        self.assertIn("approval-requested", events)
        self.assertIn("approved", events)
        self.assertIn("deployed", events)


# ---------------------------------------------------------------------------
# Reconstruction pack
# ---------------------------------------------------------------------------
class ReconstructionTests(unittest.TestCase):
    def test_reconstruct_by_spell_and_chain_proof(self):
        app, tok = _auth_app("rcn1")
        reconstruction.install(app)

        @app.spell
        def echo(x: str) -> str:
            return x

        app.invoke("echo", {"x": "a"}, token=tok())
        app.invoke("echo", {"x": "b"}, token=tok())
        by_spell = app.invoke("reconstruct_by_spell",
                              {"spell": "echo"}, token=tok())
        # 2 echo pages + reconstruct_by_spell itself does not need to be
        # there yet (it's the active call).
        self.assertGreaterEqual(by_spell["count"], 2)

        proof = app.invoke("reconstruct_chain_proof", {}, token=tok())
        self.assertTrue(proof["chain_ok"])
        self.assertGreater(proof["page_count"], 0)
        # Each page in the proof has only structural fields, no payload.
        sample = proof["proof"][0]
        self.assertEqual(set(sample.keys()),
                         {"seq", "prev", "hash", "sig", "ts"})

    def test_reconstruct_unknown_trace_raises(self):
        app, tok = _auth_app("rcn2")
        reconstruction.install(app)
        with self.assertRaises(ConjureError):
            app.invoke("reconstruct_by_trace",
                       {"trace_id": "nope"}, token=tok())


def main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for cls in (AMLTests, DPDPATests, SurveillanceTests, ReconTests,
                AlgoLifecycleTests, ReconstructionTests):
        suite.addTests(loader.loadTestsFromTestCase(cls))
    runner = unittest.TextTestRunner(verbosity=2, stream=sys.stdout)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
