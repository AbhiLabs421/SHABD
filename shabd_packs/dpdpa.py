"""
DPDPA (Digital Personal Data Protection Act) consent vault pack.

Penalty for non-compliance with India's DPDPA can go up to ₹250 crore.
This pack gives you the minimum surface every Data Fiduciary has to
expose under the act:

  * record_consent       — capture a consent with purpose + duration
  * withdraw_consent     — Data Principal exercises the right
  * verify_consent       — has this purpose been consented to right now?
  * data_subject_request — Right to Access / Erasure / Correction
  * consent_audit_log    — for the Data Protection Officer

Every operation lands in the Grimoire chain, so the DPB can audit it.
"""
from __future__ import annotations

import threading
import time
import uuid

from shabd import SHABD, ConjureError, Email, IndianPhone

__all__ = ["install", "ConsentLedger"]


class ConsentLedger:
    """Thread-safe consent store. Swap for Postgres / Oracle for
    production scale."""

    def __init__(self):
        self._lock = threading.Lock()
        # (subject_id, purpose) -> dict(consent_id, granted_at, expires_at, withdrawn_at)
        self._consents: dict = {}
        # subject_id -> list of consent_ids (for audit)
        self._subject_index: dict = {}

    def grant(self, subject_id: str, purpose: str,
              ttl_days: float, channel: str) -> dict:
        cid = f"C-{uuid.uuid4().hex[:12]}"
        now = time.time()
        record = {
            "consent_id": cid,
            "subject_id": subject_id,
            "purpose": purpose,
            "channel": channel,
            "granted_at": now,
            "expires_at": now + ttl_days * 86400,
            "withdrawn_at": None,
        }
        with self._lock:
            self._consents[(subject_id, purpose)] = record
            self._subject_index.setdefault(subject_id, []).append(cid)
        return record

    def withdraw(self, subject_id: str, purpose: str) -> dict | None:
        with self._lock:
            rec = self._consents.get((subject_id, purpose))
            if rec is None:
                return None
            rec["withdrawn_at"] = time.time()
            return dict(rec)

    def verify(self, subject_id: str, purpose: str) -> bool:
        with self._lock:
            rec = self._consents.get((subject_id, purpose))
        if rec is None:
            return False
        now = time.time()
        if rec["withdrawn_at"] is not None and rec["withdrawn_at"] <= now:
            return False
        if rec["expires_at"] <= now:
            return False
        return True

    def list_for_subject(self, subject_id: str) -> list:
        with self._lock:
            cids = list(self._subject_index.get(subject_id, []))
        return [r for r in self._consents.values()
                if r["consent_id"] in cids]


def install(app: SHABD, *,
            ledger: ConsentLedger | None = None,
            max_concurrent: int = 200) -> ConsentLedger:
    ledger = ledger or ConsentLedger()

    @app.spell(scopes=["dpo", "operator"], max_concurrent=max_concurrent,
               idempotent=False, tags=["dpdpa"])
    def record_consent(subject_id: str, purpose: str,
                       ttl_days: float = 365.0,
                       channel: str = "web") -> dict:
        """Capture a Data Principal's consent for a specific purpose."""
        if not purpose:
            raise ConjureError("purpose must not be empty",
                               code="missing_purpose",
                               hint="Use a stable purpose identifier "
                                    "(e.g. 'marketing.email').")
        return ledger.grant(subject_id, purpose, ttl_days, channel)

    @app.spell(scopes=["dpo", "subject"], idempotent=False, tags=["dpdpa"])
    def withdraw_consent(subject_id: str, purpose: str) -> dict:
        """Data Principal withdraws consent for a purpose."""
        rec = ledger.withdraw(subject_id, purpose)
        if rec is None:
            raise ConjureError("no consent on file for that purpose",
                               code="not_found")
        return rec

    @app.spell(scopes=["operator"], idempotent=True, tags=["dpdpa"])
    def verify_consent(subject_id: str, purpose: str) -> dict:
        """Caller MUST call this before processing personal data."""
        ok = ledger.verify(subject_id, purpose)
        return {"subject_id": subject_id, "purpose": purpose,
                "consented": ok, "checked_at": time.time()}

    @app.spell(scopes=["dpo"], idempotent=False, tags=["dpdpa"])
    def data_subject_request(subject_id: str, request_type: str,
                             contact_email: Email = None,  # type: ignore
                             contact_phone: IndianPhone = None) -> dict:  # type: ignore
        """Right to Access / Erasure / Correction. The spell records
        the request; your downstream pipeline does the actual fulfilment."""
        if request_type not in ("access", "erasure", "correction"):
            raise ConjureError(
                "request_type must be access / erasure / correction",
                code="bad_request_type", example="access",
            )
        return {
            "request_id": f"DSR-{uuid.uuid4().hex[:10]}",
            "subject_id": subject_id,
            "request_type": request_type,
            "contact_email": str(contact_email) if contact_email else "",
            "contact_phone": str(contact_phone) if contact_phone else "",
            "received_at": time.time(),
            "consents_on_file": [r["consent_id"]
                                  for r in ledger.list_for_subject(subject_id)],
            "sla_due_at": time.time() + 30 * 86400,   # 30 days
        }

    @app.spell(scopes=["dpo"], idempotent=True, tags=["dpdpa"])
    def consent_audit_log(subject_id: str) -> dict:
        """All consents on file for a subject. Used by the DPO."""
        return {
            "subject_id": subject_id,
            "consents": ledger.list_for_subject(subject_id),
            "generated_at": time.time(),
        }

    return ledger
