"""
Healthcare / HIPAA-style PII handling with SHABD.

    python examples/healthcare_pii.py
    python examples/healthcare_pii.py --client

Demonstrates the same pattern banks use, applied to patient data:

  * Semantic types (Email, IndianPhone, Aadhaar) catch malformed PHI
    at the boundary and mark it `x-pii: true` in the manifest.
  * The Grimoire chain records *every* read/write — auditors can
    prove later "who accessed which record, when" without ever
    seeing the raw PHI (PII is masked before hashing).
  * `Idempotency-Key` makes order-placement / charge-creation safe to
    retry, which is critical in patient billing.

This pattern works equally well for India's DPDPA, the EU AI Act
Article 12 logging requirements, and HIPAA audit trails.
"""
from __future__ import annotations

import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shabd import SHABD, Aadhaar, ConjureError, Email, IndianPhone

app = SHABD(
    "clinic",
    secret=os.environ.get("SHABD_SECRET", "x" * 32),
    require_auth=False,
    grimoire_log_path=os.environ.get("SHABD_AUDIT", "/tmp/clinic-audit.jsonl"),
)


# In-memory EHR — replace with a real DB in production.
_patients: dict = {}


@app.spell(scopes=["clinician"], max_concurrent=20)
def register_patient(name: str, email: Email, phone: IndianPhone,
                     aadhaar: Aadhaar) -> dict:
    """Create a new patient record. Audit chain captures the event."""
    patient_id = f"P-{uuid.uuid4().hex[:10]}"
    _patients[patient_id] = {
        "id": patient_id, "name": name,
        "email": str(email), "phone": str(phone),
        "aadhaar": str(aadhaar),
    }
    # NOTE: the email/phone/aadhaar in the Grimoire page are *hashed in
    # their redacted form* — the auditor can prove this exact call
    # happened without ever seeing the raw PHI.
    return {"id": patient_id, "name": name}


@app.spell(scopes=["clinician"], idempotent=True, cache_ttl=5)
def lookup_patient(patient_id: str) -> dict:
    if patient_id not in _patients:
        raise ConjureError(f"unknown patient {patient_id}",
                           code="not_found")
    p = _patients[patient_id]
    # Return only what the caller needs — minimize PHI exposure.
    return {"id": p["id"], "name": p["name"]}


@app.spell(scopes=["billing"], idempotent=False)
def create_charge(patient_id: str, amount_inr: float,
                  description: str) -> dict:
    """Bill a patient — non-idempotent, MUST be called with Idempotency-Key."""
    if patient_id not in _patients:
        raise ConjureError(f"unknown patient {patient_id}",
                           code="not_found")
    return {
        "ok": True,
        "charge_id": f"C-{uuid.uuid4().hex[:10]}",
        "patient_id": patient_id,
        "amount_inr": amount_inr,
        "description": description,
    }


def _client_demo() -> None:
    from shabd_client import SHABDClient
    c = SHABDClient("http://localhost:8765")

    # Register a patient.
    res = c.cast("register_patient", {
        "name": "Ravi Kumar",
        "email": "ravi.kumar@example.com",
        "phone": "+919876543210",
        "aadhaar": "123456789012",
    })
    pid = res["id"]
    print("Registered:", res)

    # Lookup is cached + idempotent.
    print("Lookup:", c.cast("lookup_patient", {"patient_id": pid}))

    # Billing is non-idempotent — always pass an Idempotency-Key.
    ide = f"charge-{uuid.uuid4()}"
    charge_body = {"patient_id": pid, "amount_inr": 2500.0,
                   "description": "OPD consultation"}
    c1 = c.cast("create_charge", charge_body, idempotency_key=ide)
    c2 = c.cast("create_charge", charge_body, idempotency_key=ide)
    print("Charge (1st):", c1)
    print("Charge (retry, same key — same charge_id):", c2)
    assert c1["charge_id"] == c2["charge_id"]

    # Audit chain is verifiable.
    print("\nAudit:", c.grimoire_verify())


if __name__ == "__main__":
    if "--client" in sys.argv:
        _client_demo()
    else:
        print("Run `python examples/healthcare_pii.py --client` in another shell.")
        app.serve(port=8765)
