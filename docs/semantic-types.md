# Semantic Types

Semantic types are strings that **carry their meaning into the schema**. An
`Email` is not just a `str` — the LLM sees `format: email`, the validator
enforces a pattern, and the Grimoire audit log knows it's PII.

## Bundled types

| Type | Format | PII? | Example |
|---|---|---|---|
| `Email` | RFC-5322-ish | ✅ | `alice@example.com` |
| `IndianPhone` | 10 digits, optional `+91`/`0` prefix | ✅ | `+919876543210` |
| `Aadhaar` | 12 digits (India national ID) | ✅ | `234517895432` |
| `GSTIN` | 15-char Indian tax ID | ➖ | `27AAPFU0939F1ZV` |
| `Money` | `AMOUNT CCY` (ISO-4217) | ➖ | `1500.00 INR` |
| `URL` | http(s) URL | ➖ | `https://example.com` |

## Use them like normal type hints

```python
from shabd import SHABD, Email, Aadhaar, GSTIN, IndianPhone, Money, URL

app = SHABD("kyc", secret="x" * 32, require_auth=False)

@app.spell
def onboard(
    email: Email,
    phone: IndianPhone,
    aadhaar: Aadhaar,
    gstin: GSTIN,
    payment: Money,
    callback: URL,
) -> dict:
    """All six fields are typed, pattern-validated, and PII-flagged."""
    return {
        "verified": True,
        "merchant_email": str(email),
        "gstin": str(gstin),
    }
```

## What the schema looks like

```json
{
  "email": {
    "type": "string",
    "format": "email",
    "x-semantic": "email",
    "x-pii": true,
    "description": "user@domain.tld",
    "example": "alice@example.com",
    "pattern": "^[A-Za-z0-9._%+\\-]+@[A-Za-z0-9.\\-]+\\.[A-Za-z]{2,}$"
  }
}
```

The non-standard `x-semantic` and `x-pii` extension keys let downstream
tools (your audit pipeline, your DLP scanner, an LLM) make smart decisions
without having to guess from the field name.

## Validation behavior

* **Happy path** — `app.invoke("onboard", {"email": "a@b.com", ...})` works.
* **Bad format** — raises a `ValidationError` with `hint` and `example`:

```python
{
  "error": {
    "code": "validation_failed",
    "message": "email does not match expected format",
    "hint": "user@domain.tld",
    "example": "alice@example.com"
  }
}
```

## PII auto-redaction in the audit log

When a spell argument is flagged `x-pii: true`, its raw value never enters
the Grimoire chain. The value is masked (`se***************om`) **before**
hashing, so an auditor can verify integrity without ever seeing the
original PII.

See [Grimoire](grimoire.md) for the chain mechanics.

## Adding your own semantic type

```python
from shabd import _SemanticType, ValidationError

class PAN(_SemanticType):
    """Indian Permanent Account Number — 10 chars, ABCDE1234F."""
    _semantic_name = "pan"
    _semantic_format = "10-char PAN (e.g. ABCDE1234F)"
    _is_pii = True
    _pattern = r"^[A-Z]{5}[0-9]{4}[A-Z]$"
    _example = "ABCDE1234F"

@app.spell
def tax_lookup(pan: PAN) -> dict:
    return {"holder": str(pan)}
```

The subclass automatically participates in schema generation, validation,
and PII redaction.

## Try it

```bash
python examples/semantic_types_demo.py
```
