"""
Semantic types — strings that carry meaning in the JSON schema.

    python examples/semantic_types_demo.py

These are first-class types: they validate at the boundary, surface their
meaning in the manifest (so an LLM can see "this is an Aadhaar, not just a
string"), and are auto-redacted in the Grimoire audit log when flagged PII.

Currently bundled:
    Email          (PII)
    IndianPhone    (PII)
    Aadhaar        (PII, India)
    GSTIN          (India, business ID — not PII)
    Money          (e.g. "1500.00 INR")
    URL
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shabd import (
    GSTIN,
    SHABD,
    URL,
    Aadhaar,
    Email,
    IndianPhone,
    Money,
    ValidationError,
)

app = SHABD("semantic-demo", secret="x" * 32, require_auth=False)


@app.spell
def onboard(
    email: Email,
    phone: IndianPhone,
    aadhaar: Aadhaar,
    gstin: GSTIN,
    payment: Money,
    callback: URL,
) -> dict:
    """Onboard a new merchant — every field is typed and validated."""
    return {
        "ok": True,
        "merchant_email": str(email),
        "merchant_phone": str(phone),
        "gstin": str(gstin),
        "payment": str(payment),
        "callback": str(callback),
    }


# 1) Inspect the auto-generated schema
print("─" * 60)
print("1. Manifest entry — semantic types show up as `x-semantic` + `x-pii`")
print("─" * 60)
print(json.dumps(app._spells["onboard"].schema, indent=2))

# 2) Happy path
print()
print("─" * 60)
print("2. Happy path")
print("─" * 60)
print(app.invoke("onboard", {
    "email": "shop@example.com",
    "phone": "+919876543210",
    "aadhaar": "123456789012",
    "gstin": "27AAPFU0939F1ZV",
    "payment": "1500.00 INR",
    "callback": "https://shop.example.com/webhook",
}))

# 3) Validation error with a helpful, AI-readable hint
print()
print("─" * 60)
print("3. Validation error — the response is structured so a calling LLM")
print("   can self-correct without a human in the loop.")
print("─" * 60)
try:
    app.invoke("onboard", {
        "email": "not-an-email",
        "phone": "+919876543210",
        "aadhaar": "123456789012",
        "gstin": "27AAPFU0939F1ZV",
        "payment": "1500.00 INR",
        "callback": "https://shop.example.com/webhook",
    })
except ValidationError as e:
    print(json.dumps(e.to_dict(), indent=2))

# 4) Confirm PII auto-redaction in the audit chain
print()
print("─" * 60)
print("4. The Grimoire page — raw email / phone / aadhaar are NOT hashed;")
print("   their redacted form is. Auditors can verify integrity without")
print("   ever seeing the original values.")
print("─" * 60)
print(json.dumps(app.grimoire.pages()[0], indent=2))
