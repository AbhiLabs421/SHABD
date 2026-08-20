"""
Settlement reconciliation pack.

Two-way reconciliation between SHABD's internal view and an external
ledger feed (RTGS / NEFT / UPI / exchange settlement file). Produces
break reports the operations team can hand to the regulator.

Spells:

  ingest_ledger_feed(rows)            — bulk ingest external rows
  ingest_internal_feed(rows)          — bulk ingest internal rows
  reconcile_day(date)                 — produce the break list
  break_summary(date)                 — counters for the daily report
"""
from __future__ import annotations

import threading
import time
import typing as t
from collections import defaultdict

from shabd import SHABD, ConjureError

__all__ = ["install", "ReconState"]


class ReconState:
    """In-memory recon store. Each day's feeds live under a date key."""

    def __init__(self):
        self._lock = threading.Lock()
        self._external: dict = defaultdict(list)   # date -> [rows]
        self._internal: dict = defaultdict(list)

    def add_external(self, date: str, rows: t.Iterable[dict]) -> None:
        with self._lock:
            self._external[date].extend(rows)

    def add_internal(self, date: str, rows: t.Iterable[dict]) -> None:
        with self._lock:
            self._internal[date].extend(rows)

    def reconcile(self, date: str, *,
                  key_field: str = "ref",
                  amount_field: str = "amount_inr",
                  amount_tolerance: float = 0.5) -> dict:
        with self._lock:
            ext = {r[key_field]: r for r in self._external[date]}
            int_ = {r[key_field]: r for r in self._internal[date]}
        only_external = [ext[k] for k in ext.keys() - int_.keys()]
        only_internal = [int_[k] for k in int_.keys() - ext.keys()]
        amount_mismatch = []
        for k in ext.keys() & int_.keys():
            e_amt = float(ext[k].get(amount_field, 0.0))
            i_amt = float(int_[k].get(amount_field, 0.0))
            if abs(e_amt - i_amt) > amount_tolerance:
                amount_mismatch.append({
                    "ref": k,
                    "external_amount": e_amt,
                    "internal_amount": i_amt,
                    "delta": round(e_amt - i_amt, 2),
                })
        return {
            "date": date,
            "matched": len(ext.keys() & int_.keys()),
            "only_external": only_external,
            "only_internal": only_internal,
            "amount_mismatches": amount_mismatch,
            "break_count": (len(only_external) + len(only_internal)
                            + len(amount_mismatch)),
            "generated_at": time.time(),
        }


def install(app: SHABD, *,
            state: ReconState | None = None) -> ReconState:
    state = state or ReconState()

    @app.spell(scopes=["recon-ops"], idempotent=False,
               tags=["reconciliation"])
    def ingest_ledger_feed(date: str, rows: list[dict]) -> dict:
        """Bulk-load external settlement rows for the day."""
        state.add_external(date, rows)
        return {"date": date, "ingested": len(rows), "source": "external"}

    @app.spell(scopes=["recon-ops"], idempotent=False,
               tags=["reconciliation"])
    def ingest_internal_feed(date: str, rows: list[dict]) -> dict:
        """Bulk-load internal settlement rows for the day."""
        state.add_internal(date, rows)
        return {"date": date, "ingested": len(rows), "source": "internal"}

    @app.spell(scopes=["recon-ops"], idempotent=True, cache_ttl=2,
               tags=["reconciliation"])
    def reconcile_day(date: str, amount_tolerance: float = 0.5) -> dict:
        """Run the recon and return the break list."""
        if not date:
            raise ConjureError("date is required",
                               code="missing_date", example="2026-06-04")
        return state.reconcile(date, amount_tolerance=amount_tolerance)

    @app.spell(scopes=["recon-ops"], idempotent=True, cache_ttl=2,
               tags=["reconciliation"])
    def break_summary(date: str) -> dict:
        """Counters for the daily operations dashboard."""
        rep = state.reconcile(date)
        return {
            "date": date,
            "matched": rep["matched"],
            "only_external": len(rep["only_external"]),
            "only_internal": len(rep["only_internal"]),
            "amount_mismatches": len(rep["amount_mismatches"]),
            "break_count": rep["break_count"],
        }

    return state
