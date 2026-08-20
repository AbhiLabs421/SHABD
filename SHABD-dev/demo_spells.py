from shabd import SHABD, Aadhaar, Email

app = SHABD("demo")


@app.spell(tags=["retail"])
def discount(price: float, pct: float) -> dict:
    """Apply a percentage discount to a price."""
    return {"final": round(price * (1 - pct / 100), 2), "saved": round(price * pct / 100, 2)}


@app.spell(tags=["hr"])
def leave_balance(employee_id: str) -> dict:
    """Look up an employee's leave balance."""
    return {"casual": 12, "sick": 5, "earned": 21}


@app.spell(scopes=["trader"], tags=["trading"])
def place_order(symbol: str, side: str, qty: int) -> dict:
    """Place a buy/sell order (scoped to the 'trader' role)."""
    return {"ok": True, "order_id": f"O-{symbol}-{side}-{qty}"}


@app.spell(scopes=["compliance"], tags=["kyc"])
def kyc_check(name: str, aadhaar: Aadhaar, email: Email) -> dict:
    """Run a KYC check — Aadhaar/Email are semantic types (auto-validated + PII-masked in audit)."""
    return {"verified": True, "name": name}
