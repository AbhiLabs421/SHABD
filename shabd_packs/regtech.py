"""
RegTech pack — RBI / SEBI / FIU-IND report generators.

What this pack ships:

  generate_ctr(date)             — RBI Cash Transaction Report (₹10L+)
  generate_str(case_id, summary) — FIU-IND Suspicious Transaction Report
  generate_form_61a(period)      — High-value transaction report
  generate_ntrp(date)            — Non-Transactional Reporting Platform
  generate_digital_lending_audit(loan_id) — RBI Digital Lending guideline-
                                            compliant audit packet

The output is a plain dict (or JSON if you ask). Wire your own pipeline
to actually submit — most teams send it to a regulator portal via
their existing RTGS/SFTP feed. The Grimoire entry that lands when one
of these spells runs is the auditable proof of *what* you submitted
and *when*.

This module deliberately does not call any regulator API directly.
That is a per-bank integration. We just produce the canonical payload
in the format the regulator expects, with cryptographic provenance.
"""
from __future__ import annotations

import time
import typing as t

from shabd import SHABD, Aadhaar, ConjureError, Money

__all__ = ["install", "render_packet"]


# A tiny in-memory pretend transaction store so the example spells have
# something to chew on. In production you wire these to your real
# ledger / data warehouse.
_DEMO_TRANSACTIONS: list = []


def _between(t0: float, t1: float, ts: float) -> bool:
    return t0 <= ts < t1


def _to_epoch(date_str: str) -> float:
    return time.mktime(time.strptime(date_str, "%Y-%m-%d"))


def install(app: SHABD, *,
            regulator: str = "RBI",
            entity_code: str = "DEMO-BANK-001",
            txn_source: t.Callable[[], list] | None = None) -> None:
    """Wire the RegTech report spells onto `app`.

    `txn_source` is your read-only function returning the day's
    transactions — list of dicts with at least `ts`, `amount`, `ccy`,
    `from`, `to`, `purpose`. Defaults to an in-memory demo list.
    """
    source = txn_source or (lambda: list(_DEMO_TRANSACTIONS))

    @app.spell(scopes=["compliance"], idempotent=True, tags=["regtech"])
    def generate_ctr(date_yyyy_mm_dd: str,
                     threshold_inr: float = 1_000_000.0) -> dict:
        """RBI Cash Transaction Report — cash deposits / withdrawals
        equal to or above ₹10 lakh in a day for one customer."""
        t0 = _to_epoch(date_yyyy_mm_dd)
        t1 = t0 + 86_400
        rows = [tx for tx in source()
                if _between(t0, t1, tx["ts"])
                and tx.get("mode") == "cash"
                and tx["amount"] >= threshold_inr]
        return {
            "report_type": "CTR",
            "regulator": regulator,
            "entity_code": entity_code,
            "date": date_yyyy_mm_dd,
            "count": len(rows),
            "rows": rows,
        }

    @app.spell(scopes=["compliance"], idempotent=True, tags=["regtech"])
    def generate_str(case_id: str, summary: str,
                     parties: list[str],
                     amount_inr: float) -> dict:
        """FIU-IND Suspicious Transaction Report."""
        if not summary:
            raise ConjureError("summary is mandatory in an STR",
                               code="missing_summary",
                               hint="Provide a non-empty narrative.")
        return {
            "report_type": "STR",
            "regulator": "FIU-IND",
            "entity_code": entity_code,
            "case_id": case_id,
            "summary": summary,
            "parties": parties,
            "amount_inr": amount_inr,
            "generated_at": time.time(),
        }

    @app.spell(scopes=["compliance"], idempotent=True, tags=["regtech"])
    def generate_form_61a(quarter: str,
                          rows: list[dict]) -> dict:
        """High-value transaction report under section 285BA / Form 61A.

        Caller supplies the pre-aggregated rows; this spell wraps them
        in the canonical envelope and stamps the audit chain."""
        return {
            "report_type": "Form-61A",
            "regulator": "Income-Tax",
            "entity_code": entity_code,
            "quarter": quarter,
            "rows": rows,
        }

    @app.spell(scopes=["compliance"], idempotent=True, tags=["regtech"])
    def generate_digital_lending_audit(loan_id: str,
                                       applicant_aadhaar: Aadhaar,
                                       model_version: str,
                                       decision: str,
                                       top_factors: list[str],
                                       requested_inr: Money) -> dict:
        """RBI Digital Lending guideline-compliant audit packet.

        The point: prove to RBI that for this loan, you have (a) the
        applicant identity, (b) the model version, (c) the decision,
        and (d) the human-readable top factors that drove the decision.
        Grimoire then stamps it for non-repudiation."""
        return {
            "report_type": "DigitalLendingAudit",
            "regulator": "RBI",
            "entity_code": entity_code,
            "loan_id": loan_id,
            "applicant_aadhaar_masked": (
                str(applicant_aadhaar)[:2] + "*" * 8 +
                str(applicant_aadhaar)[-2:]
            ),
            "model_version": model_version,
            "decision": decision,
            "top_factors": top_factors,
            "requested": str(requested_inr),
            "generated_at": time.time(),
        }

    @app.spell(scopes=["compliance"], idempotent=True, tags=["regtech"])
    def generate_ntrp(date_yyyy_mm_dd: str) -> dict:
        """Non-Transactional Reporting Platform (NTRP) submission —
        for non-transaction events (KYC updates, structural changes)."""
        return {
            "report_type": "NTRP",
            "regulator": regulator,
            "entity_code": entity_code,
            "date": date_yyyy_mm_dd,
            "events": [],   # caller fills
            "generated_at": time.time(),
        }


def render_packet(report: dict) -> str:
    """Lightweight pretty-renderer for inclusion in email summaries."""
    import json
    return json.dumps(report, indent=2, default=str)
