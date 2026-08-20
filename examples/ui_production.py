"""
SHABD UI — production deployment example.

Wires the full SHABD stack into the no-code UI:

  * spells from your code
  * Grimoire chain (with disk persistence)
  * Orchestrator with intents
  * Agent notary
  * Keycloak authentication (TCS Ultimatix-style password grant)

USAGE — copy this block into your shell:

    # ---- Keycloak ----
    export KEYCLOAK_URL="https://keycloak.bank.internal"
    export KEYCLOAK_REALM="Ultimatix"
    export KEYCLOAK_CLIENT_ID="Tcs-nginx-manager"
    # export KEYCLOAK_CLIENT_SECRET="..."  # only for confidential clients

    # ---- SHABD ----
    export SHABD_SECRET="$(openssl rand -hex 32)"
    export NOTARY_SECRET="$(openssl rand -hex 32)"
    export SHABD_AUDIT="/var/lib/shabd/audit.jsonl"
    export SHABD_SUPERUSERS="abhishek,risk-officer"
    export SHABD_ADMINS="amit.clerk,priya.manager"

    # ---- run ----
    python examples/ui_production.py

    # Then open  http://<server>:8080  in your browser.

For local testing WITHOUT Keycloak, leave KEYCLOAK_URL unset and add:

    export SHABD_UI_BOOTSTRAP_USER=admin
    export SHABD_UI_BOOTSTRAP_PASSWORD="any-strong-string"

Behind nginx with TLS termination, also export:

    export SHABD_UI_SECURE_COOKIES=1
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shabd import SHABD, Aadhaar, Email, Money  # noqa: E402
from shabd_notary import AgentNotary  # noqa: E402
from shabd_orchestrator import (  # noqa: E402
    Orchestrator,
    SemanticIntentClassifier,
)
from shabd_ui import KeycloakConfig, UIServer  # noqa: E402

# ---- 1. The SHABD app ------------------------------------------------------
app = SHABD(
    "prod-shabd",
    secret=os.environ.get("SHABD_SECRET", "x" * 32),
    require_auth=False,    # auth is enforced at the UI layer
    grimoire_log_path=os.environ.get("SHABD_AUDIT",
                                       "/tmp/shabd-prod-audit.jsonl"),
)


@app.spell(tags=["hr"])
def lookup_leave_balance(employee_id: str) -> dict:
    """Read an employee's current leave balance."""
    return {"employee_id": employee_id,
            "casual": 12, "sick": 5, "earned": 21}


@app.spell(scopes=["compliance"], tags=["kyc"])
def kyc_check(name: str, aadhaar: Aadhaar, email: Email) -> dict:
    """Run an Aadhaar-based KYC check. PII auto-redacted in the audit chain."""
    return {"verified": True, "name": name,
            "domain": str(email).split("@")[1]}


@app.spell(scopes=["trader"], tags=["trading"], max_concurrent=10,
           idempotent=False)
def place_order(symbol: str, side: str, qty: int,
                limit_price: float) -> dict:
    """Place a limit order — non-idempotent, requires the 'trader' role."""
    return {"ok": True, "order_id": f"O-{symbol}-{qty}",
            "side": side, "limit_price": limit_price}


@app.spell(tags=["it"])
def create_ticket(summary: str, severity: str = "P3") -> dict:
    """Open an IT helpdesk ticket."""
    return {"ok": True, "ticket": "INC-12345",
            "severity": severity, "summary": summary}


@app.spell(tags=["bank"])
def open_account(customer_name: str, opening_deposit: Money,
                 referrer: str = "") -> dict:
    """Open a savings account."""
    return {"ok": True, "account": f"A-{customer_name[:4].upper()}-001",
            "deposit": str(opening_deposit), "referrer": referrer}


# ---- 2. Orchestrator with semantic intent classification -------------------
orch = Orchestrator(
    classifier=SemanticIntentClassifier(),
    audit_app=app,
    track_provenance=True,
)


@orch.intent("hr",
              keywords=["leave", "policy", "holiday", "vacation"],
              description="HR questions — leaves, attendance, policy.")
def _hr(d):
    from shabd_agent import Agent, MockBackend
    return Agent(llm=MockBackend(["See your leave balance above."]),
                 system="You are PolicyBuddy.")


@orch.intent("it",
              keywords=["ticket", "outage", "vpn", "wifi", "laptop",
                        "printer"],
              description="IT helpdesk — raise tickets, check status.")
def _it(d):
    from shabd_agent import Agent, MockBackend
    return Agent(llm=MockBackend(["Ticket INC-12345 raised."]),
                 system="You are AiOps.")


@orch.intent("fallback")
def _fb(d):
    from shabd_agent import Agent, MockBackend
    return Agent(llm=MockBackend(["I can help with HR or IT queries."]))


# ---- 3. Notary (cross-entity audit anchoring) ------------------------------
notary = AgentNotary(
    app,
    entity=os.environ.get("SHABD_ENTITY", "your-bank"),
    publishing_secret=os.environ.get("NOTARY_SECRET", "n" * 64),
)


# ---- 4. Keycloak config (falls back to bootstrap mode) ---------------------
kc = None
if os.environ.get("KEYCLOAK_URL"):
    kc = KeycloakConfig(
        server_url=os.environ["KEYCLOAK_URL"],
        realm=os.environ.get("KEYCLOAK_REALM", "master"),
        client_id=os.environ.get("KEYCLOAK_CLIENT_ID",
                                  "Tcs-nginx-manager"),
        client_secret=os.environ.get("KEYCLOAK_CLIENT_SECRET", ""),
    )


def _csv(name: str) -> list:
    return [x.strip() for x in os.environ.get(name, "").split(",")
            if x.strip()]


# ---- 5. The UI server ------------------------------------------------------
ui = UIServer(
    app,
    keycloak=kc,
    superusers=_csv("SHABD_SUPERUSERS") or ["admin"],
    admins=_csv("SHABD_ADMINS"),
    bind=os.environ.get("SHABD_UI_BIND", "0.0.0.0"),
    port=int(os.environ.get("SHABD_UI_PORT", "8080")),
    force_secure_cookies=os.environ.get("SHABD_UI_SECURE_COOKIES",
                                          "") == "1",
    orchestrator=orch,
    notary=notary,
)


if __name__ == "__main__":
    print()
    print("=" * 70)
    print("SHABD production UI")
    print("=" * 70)
    print(f"  URL          : http://{ui.bind}:{ui.port}/")
    print(f"  Auth         : "
          f"{'Keycloak: ' + kc.realm if kc else 'BOOTSTRAP (env-password only)'}")
    print(f"  Spells       : {len(app._spells)}")
    print(f"  Audit at     : {os.environ.get('SHABD_AUDIT', '/tmp/...')}")
    print(f"  Superusers   : {ui._superusers or {'admin'}}")
    print(f"  Admins       : {ui._admins or 'none'}")
    print()
    print("  v2.9 no-code pages:")
    print("    /builder   — superuser writes Python spells from the browser")
    print("    /tokens    — admin mints scoped bearer tokens")
    print("    /scopes    — admin edits required scopes per spell")
    print("    /client    — any user proxies calls to any other SHABD server")
    if not kc:
        print()
        print("  ⚠  Set KEYCLOAK_URL/REALM/CLIENT_ID to enable SSO.")
        print("     Bootstrap login: "
              f"user={os.environ.get('SHABD_UI_BOOTSTRAP_USER','admin')}, "
              f"password=$SHABD_UI_BOOTSTRAP_PASSWORD")
    print("=" * 70)
    print()
    ui.serve()
