"""
Bank-style money transfer with SHABD's enterprise extras.

    python examples/bank_transfer.py             # run the server
    python examples/bank_transfer.py --client    # demo the safe-retry flow

What this example showcases:

1.  Semantic types catch malformed inputs (Aadhaar, Money) at the boundary.
2.  `Idempotency-Key` makes the transfer endpoint safe to retry — a network
    blip never double-charges the customer.
3.  `max_concurrent` caps in-flight transfers so the downstream ledger
    can't be overwhelmed.
4.  Every call appends to the Grimoire audit chain (verifiable later).
5.  W3C `traceparent` headers stitch this service into a wider trace.
"""
from __future__ import annotations

import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shabd import SHABD, Aadhaar, ConjureError, Money

app = SHABD("bank",
            secret=os.environ.get("SHABD_SECRET", "x" * 32),
            require_auth=False,
            grimoire_log_path=os.environ.get("SHABD_AUDIT", "/tmp/bank-audit.jsonl"))


# In-memory ledger — replace with a real DB in production.
_balances = {"A100": 100000.0, "B200": 5000.0, "C300": 0.0}


@app.spell(idempotent=False, max_concurrent=50, scopes=[])
def transfer(from_acct: str, to_acct: str, amount: Money,
             customer_aadhaar: Aadhaar) -> dict:
    """
    Move money between two ledger accounts.

    Non-idempotent by nature — callers MUST send an Idempotency-Key
    header for retry safety. The same key + body returns the same
    response; the same key + a different body raises a 400.
    """
    if from_acct not in _balances:
        raise ConjureError(f"unknown source account {from_acct}",
                           code="account_not_found")
    if to_acct not in _balances:
        raise ConjureError(f"unknown destination account {to_acct}",
                           code="account_not_found")

    value, ccy = str(amount).split()
    value = float(value)
    if _balances[from_acct] < value:
        raise ConjureError("insufficient funds",
                           code="insufficient_funds",
                           hint=f"Balance is {_balances[from_acct]} {ccy}",
                           example={"amount": f"{_balances[from_acct]} {ccy}"})

    _balances[from_acct] -= value
    _balances[to_acct] += value
    return {
        "ok": True,
        "ref": f"TXN-{uuid.uuid4().hex[:12]}",
        "from": from_acct, "to": to_acct, "amount": str(amount),
        "new_balances": {
            from_acct: _balances[from_acct],
            to_acct: _balances[to_acct],
        },
    }


@app.spell(idempotent=True, cache_ttl=5)
def balance(acct: str) -> dict:
    """Read an account balance. Safe to retry, cacheable."""
    if acct not in _balances:
        raise ConjureError(f"unknown account {acct}", code="account_not_found")
    return {"acct": acct, "balance": _balances[acct]}


# ---------------------------------------------------------------------------
# CLIENT DEMO — shows how a calling agent should structure a safe retry.
# ---------------------------------------------------------------------------
def _client_demo() -> None:
    from shabd_client import SHABDClient, SHABDClientError

    c = SHABDClient("http://localhost:8765", retries=2)
    print("Initial balances:", c.cast("balance", {"acct": "A100"}),
          c.cast("balance", {"acct": "B200"}))

    # Safe retry: same idempotency key + same body => same response.
    ide = f"transfer-{uuid.uuid4()}"
    body = {
        "from_acct": "A100", "to_acct": "B200",
        "amount": "5000.00 INR",
        "customer_aadhaar": "123456789012",
    }
    r1 = c.cast("transfer", body, idempotency_key=ide)
    print("Transfer 1:", r1)
    r2 = c.cast("transfer", body, idempotency_key=ide)  # network retry
    print("Transfer 2 (retry — same ref!):", r2)
    assert r1["ref"] == r2["ref"], "idempotency replay should return same ref"

    # Same key + different body => SHABD raises so the caller sees the bug.
    try:
        c.cast("transfer", {**body, "amount": "999.00 INR"},
               idempotency_key=ide)
    except SHABDClientError as e:
        print("Got expected conflict:", e.code, "—", e.hint)

    print("\nGrimoire integrity:", c.grimoire_verify())


if __name__ == "__main__":
    if "--client" in sys.argv:
        _client_demo()
    else:
        print("Run `python examples/bank_transfer.py --client` in another "
              "shell to see the safe-retry flow.")
        app.serve(port=8765)
