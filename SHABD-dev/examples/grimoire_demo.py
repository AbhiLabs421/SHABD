"""
Grimoire demo — a tamper-evident, signed audit log of every spell cast.

    python examples/grimoire_demo.py

What you'll see:
  1. Each call appends a hash-chained, HMAC-signed page.
  2. The chain verifies in O(n).
  3. Tampering with any past page is detected.
  4. PII args are hashed in their redacted form, not the raw values.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shabd import SHABD, Aadhaar, Email

app = SHABD("audit-demo", secret="super-secret-32-bytes-or-more!!", require_auth=False)


@app.spell
def transfer(from_acct: str, to_acct: str, amount: float) -> dict:
    return {"ok": True, "ref": f"TXN-{from_acct}-{to_acct}"}


@app.spell
def kyc(email: Email, aadhaar: Aadhaar) -> dict:
    return {"verified": True}


# 1) Make a few calls
print("─" * 60)
print("1. Casting spells …")
print("─" * 60)
app.invoke("transfer", {"from_acct": "A100", "to_acct": "B200", "amount": 5000.0})
app.invoke("transfer", {"from_acct": "A100", "to_acct": "C300", "amount": 100.0})
app.invoke("kyc", {"email": "alice@example.com", "aadhaar": "123456789012"})

# 2) Verify the chain
print()
print("─" * 60)
print("2. Verifying the chain …")
print("─" * 60)
print(json.dumps(app.grimoire.verify(), indent=2))

# 3) Inspect a page (notice: only hashes, no raw PII)
print()
print("─" * 60)
print("3. The KYC page — only hashes, no raw email / aadhaar")
print("─" * 60)
print(json.dumps(app.grimoire.pages()[-1], indent=2))

# 4) Tamper detection
print()
print("─" * 60)
print("4. Tamper detection — flipping `ok` on a past page")
print("─" * 60)
app.grimoire._pages[0]["ok"] = False  # simulate an evil edit
print(json.dumps(app.grimoire.verify(), indent=2))

print()
print("─" * 60)
print("In production, mount this at /grimoire/verify and an external")
print("auditor can prove integrity without seeing raw PII.")
print("─" * 60)
