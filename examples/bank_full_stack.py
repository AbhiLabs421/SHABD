"""
End-to-end "bank" example combining the enterprise extras and the
revenue packs.

    python examples/bank_full_stack.py            # run the server
    python examples/bank_full_stack.py --client   # exercise it

What this single file demonstrates:

  * shabd_enterprise — SQLite-backed Grimoire, RBAC, OTLP traces,
                        cluster replication (peer is a no-op for the
                        demo), and the SeparationOfDutiesPolicy.
  * shabd_packs      — sanctions screening + RegTech reports + a
                        custom KYC spell that uses the semantic types.
  * Standard SHABD   — semantic types (Aadhaar, Money), idempotency,
                        Grimoire audit, AI-native errors.

Wired together, these compose into a ~5 minute "bank-grade tool layer
for AI agents" demo you can show to a compliance team.
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shabd import SHABD, Aadhaar, Money
from shabd_enterprise import (
    ClusterPeer,
    OTLPSpanExporter,
    RBACPolicyEngine,
    SeparationOfDutiesPolicy,
    SQLiteGrimoirePersistence,
    install_enterprise,
)
from shabd_packs import ccil, pretrade, regtech, sanctions

SECRET = os.environ.get("SHABD_SECRET", "x" * 32)
DB_PATH = os.environ.get("SHABD_AUDIT_DB",
                         os.path.join(tempfile.gettempdir(),
                                      "bank-audit.db"))


def build_app() -> SHABD:
    app = SHABD("bank-full", secret=SECRET, require_auth=True,
                idempotency_ttl=86400)

    # 1) Bank-specific spell — composes semantic types + sanctions block.
    lister = sanctions.install(app)

    @app.spell(scopes=["clerk"], idempotent=False)
    def open_account(name: str, aadhaar: Aadhaar,
                     opening_deposit: Money) -> dict:
        """Open a new savings account. Sanctions-screened before save."""
        sanctions.block_if_sanctioned(name, lister)
        return {
            "ok": True,
            "account_id": f"A-{abs(hash((name, str(aadhaar)))) % 10_000_000:07d}",
            "opening_deposit": str(opening_deposit),
        }

    @app.spell(scopes=["clerk", "branch-manager"], idempotent=False)
    def wire_transfer(from_account: str, to_account: str,
                      amount: Money, approver_token: str = "") -> dict:
        """High-value wire transfer — requires a SoD approver token."""
        return {"ok": True, "from": from_account, "to": to_account,
                "amount": str(amount), "ref": f"WIRE-{from_account[-4:]}"}

    # 2) Mount the revenue packs.
    regtech.install(app, regulator="RBI", entity_code="DEMO-BANK-001")
    pretrade.install(app)
    ccil.install(app, member_id="MBR-DEMO-001")

    # 3) Wire enterprise concerns.
    rbac = RBACPolicyEngine()
    rbac.add_rule("clerk", allow=["open_account"])
    rbac.add_rule("compliance", allow_prefixes=[
        "screen_*", "generate_*", "refresh_*", "list_*", "report_*",
    ])
    rbac.add_rule("dealer", allow=["book_repo", "book_ndsom_trade"])
    rbac.add_rule("algo", allow=["check_pre_trade", "position_inquiry"])
    rbac.add_rule("risk-admin", allow=["reset_limits"])
    rbac.add_rule("branch-manager", allow=["wire_transfer"])
    rbac.add_rule("admin", allow_prefixes=["*"])

    install_enterprise(
        app,
        sqlite_store=SQLiteGrimoirePersistence(DB_PATH),
        rbac=rbac,
        otlp=OTLPSpanExporter(
            endpoint=os.environ.get(
                "SHABD_OTLP_URL", "http://localhost:4318"
            ),
            service_name="bank-full",
        ),
        cluster=ClusterPeer(
            peers=[],   # add http://peer-2:8765 etc. for HA
            hmac_secret=SECRET.encode() if isinstance(SECRET, str) else SECRET,
        ),
    )

    # 4) SoD on the high-value wire endpoint.
    SeparationOfDutiesPolicy(app, sensitive_spells=["wire_transfer"])

    return app


app = build_app()


def _client_demo() -> None:
    from shabd_client import SHABDClient

    clerk_tok = app.issue_token("amit.clerk", scopes=["clerk", "compliance"])
    manager_tok = app.issue_token("priya.manager",
                                  scopes=["branch-manager"])

    c_clerk = SHABDClient("http://localhost:8765", token=clerk_tok)
    c_manager = SHABDClient("http://localhost:8765", token=manager_tok)

    print(">> screen", c_clerk.cast("screen_party",
                                    {"name": "Plain Citizen"}))
    print(">> open", c_clerk.cast("open_account", {
        "name": "Ravi Kumar",
        "aadhaar": "123456789012",
        "opening_deposit": "10000.00 INR",
    }))
    print(">> regtech_packet:", c_clerk.cast("generate_digital_lending_audit", {
        "loan_id": "L-DEMO-1", "applicant_aadhaar": "123456789012",
        "model_version": "v2.3", "decision": "APPROVE",
        "top_factors": ["income", "credit_history", "DTI"],
        "requested_inr": "100000.00 INR",
    }))

    # Wire transfer needs an approver from a different subject.
    approver = app.issue_token("dual.approver", scopes=["branch-manager"])
    print(">> wire", c_manager.cast("wire_transfer", {
        "from_account": "A-1234567", "to_account": "A-7654321",
        "amount": "250000.00 INR", "approver_token": approver,
    }))

    print("\nGrimoire integrity:", c_clerk.grimoire_verify())


if __name__ == "__main__":
    if "--client" in sys.argv:
        _client_demo()
    else:
        print("Run `python examples/bank_full_stack.py --client` in "
              "another shell to exercise the demo.")
        print(f"Audit DB at {DB_PATH}")
        app.serve(port=8765)
