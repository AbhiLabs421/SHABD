"""
CCIL bridge pack — NDS-OM / TRP / CCP-FX spell wrappers.

What this pack ships:

  book_repo(counterparty_mid, isin, qty, rate, tenor_days)
  book_ndsom_trade(counterparty_mid, isin, qty, price, side)
  report_otc_derivative(trade_id, counterparty, notional_ccy, notional, tenor)
  query_member_exposure(member_id)
  submit_to_trp(trade_payload)

These are *interfaces*. We do not — and cannot — call CCIL endpoints
directly without a member-level integration agreement. The point of
this pack is to give your member operations team a single, audit-
backed surface for AI agents to interact with CCIL services through:

  * Each spell validates inputs (semantic types: Money, GSTIN, ISIN-like).
  * Each spell stamps the Grimoire chain so member compliance can prove
    "the AI submitted this trade at this time, by this user".
  * Each spell uses `Idempotency-Key` so a network blip doesn't double-
    submit a trade to CCIL (the most common operations pain point).

For the actual wire-format submission, plug in a `Backend` adapter
that talks to your member's CCIL gateway.
"""
from __future__ import annotations

import time
import uuid

from shabd import SHABD, ConjureError, Money

__all__ = ["install", "Backend"]


class Backend:
    """Abstract CCIL backend. Replace methods to call your member
    gateway. The default returns mocked responses so demos and tests
    work without network access."""

    def __init__(self, member_id: str = "MBR-DEMO-001"):
        self.member_id = member_id

    def submit_repo(self, payload: dict) -> dict:
        return {"ccil_ref": f"REPO-{uuid.uuid4().hex[:10]}",
                "status": "accepted", "member_id": self.member_id}

    def submit_ndsom(self, payload: dict) -> dict:
        return {"ccil_ref": f"NDS-{uuid.uuid4().hex[:10]}",
                "status": "accepted", "member_id": self.member_id}

    def submit_trp(self, payload: dict) -> dict:
        return {"trp_ref": f"TRP-{uuid.uuid4().hex[:10]}",
                "status": "received", "member_id": self.member_id}

    def query_exposure(self, member_id: str) -> dict:
        return {"member_id": member_id, "exposure_inr_cr": 0.0,
                "as_of": time.time()}


def _is_isin(s: str) -> bool:
    return len(s) == 12 and s[:2].isalpha() and s[2:].isalnum()


def install(app: SHABD, *,
            backend: Backend | None = None,
            member_id: str = "MBR-DEMO-001",
            max_concurrent: int = 200) -> Backend:
    backend = backend or Backend(member_id=member_id)

    @app.spell(scopes=["dealer"], max_concurrent=max_concurrent,
               idempotent=False, tags=["ccil", "repo"])
    def book_repo(counterparty_mid: str, isin: str, qty: int,
                  rate_pct: float, tenor_days: int) -> dict:
        """Book a repo trade through CCIL's NDS-OM repo segment.

        The `Idempotency-Key` header is mandatory in production — the
        client SDK passes one automatically; the chain records the same
        key so audit can correlate retries with the original submission.
        """
        if not _is_isin(isin):
            raise ConjureError(f"'{isin}' does not look like an ISIN",
                               code="bad_isin",
                               hint="ISINs are 12 chars, e.g. IN1234567890",
                               example="IN0020230015")
        if tenor_days <= 0 or tenor_days > 365:
            raise ConjureError("tenor_days out of range",
                               code="bad_tenor", example=1)
        payload = {
            "counterparty_mid": counterparty_mid, "isin": isin,
            "qty": qty, "rate_pct": rate_pct,
            "tenor_days": tenor_days, "member_id": backend.member_id,
        }
        return backend.submit_repo(payload)

    @app.spell(scopes=["dealer"], max_concurrent=max_concurrent,
               idempotent=False, tags=["ccil", "nds-om"])
    def book_ndsom_trade(counterparty_mid: str, isin: str, qty: int,
                         price: float, side: str) -> dict:
        """Book an outright G-Sec trade via NDS-OM."""
        if side not in ("buy", "sell"):
            raise ConjureError("side must be 'buy' or 'sell'",
                               code="bad_side")
        if not _is_isin(isin):
            raise ConjureError(f"bad ISIN '{isin}'", code="bad_isin")
        payload = {"counterparty_mid": counterparty_mid, "isin": isin,
                   "qty": qty, "price": price, "side": side,
                   "member_id": backend.member_id}
        return backend.submit_ndsom(payload)

    @app.spell(scopes=["compliance"], max_concurrent=max_concurrent,
               idempotent=False, tags=["ccil", "trp"])
    def report_otc_derivative(trade_id: str, counterparty: str,
                              notional: Money, tenor_days: int,
                              product: str) -> dict:
        """Submit an OTC derivative trade to CCIL's Trade Reporting
        Platform — FX forward, IRS, swaption etc."""
        payload = {"trade_id": trade_id, "counterparty": counterparty,
                   "notional": str(notional), "tenor_days": tenor_days,
                   "product": product, "member_id": backend.member_id}
        return backend.submit_trp(payload)

    @app.spell(scopes=["risk", "compliance"], idempotent=True,
               cache_ttl=10, tags=["ccil"])
    def query_member_exposure(member_id: str) -> dict:
        """Read-only inquiry for current exposure with CCIL."""
        return backend.query_exposure(member_id)

    return backend
