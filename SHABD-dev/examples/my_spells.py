"""
A guided tour of every SHABD feature in one file.

    python examples/my_spells.py
    # open http://localhost:8765/dashboard

What's covered:
  * Basic spell
  * Async spell
  * Streaming spell (generator)
  * Image return (SpellImage)
  * Resource (`@app.resource`)
  * Prompt template (`@app.prompt`)
  * Spell chain (pipeline)
  * Group (multi-project namespace)
  * Semantic-typed spell (Email / Aadhaar / GSTIN / Money)
  * Context-aware spell
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shabd import (
    GSTIN,
    SHABD,
    Aadhaar,
    Context,
    Email,
    Money,
    SpellImage,
)

app = SHABD("my-spells", secret="x" * 32, require_auth=False)


# ── basic ───────────────────────────────────────────────────────────────────
@app.spell
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


# ── async ───────────────────────────────────────────────────────────────────
@app.spell
async def fetch_status() -> dict:
    """Return a fake status object."""
    return {"ok": True, "ts": time.time()}


# ── streaming generator ────────────────────────────────────────────────────
@app.spell
def countdown(n: int = 5):
    """Stream numbers down to zero."""
    for i in range(n, -1, -1):
        yield {"i": i}
        time.sleep(0.1)


# ── image return ────────────────────────────────────────────────────────────
@app.spell
def banner(text: str = "SHABD") -> SpellImage:
    """Return a tiny PNG banner."""
    # 1x1 transparent PNG; in real code you'd render something useful.
    one_px_png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4"
        b"\x89\x00\x00\x00\rIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01"
        b"\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    return SpellImage(data=one_px_png, mime_type="image/png", alt_text=text)


# ── resource ────────────────────────────────────────────────────────────────
@app.resource("/docs/{slug}", mime_type="text/markdown")
def get_doc(slug: str) -> str:
    """Return a documentation page (stub)."""
    return f"# {slug.title()}\n\nThis is the doc for `{slug}`."


# ── prompt ──────────────────────────────────────────────────────────────────
@app.prompt("code_review")
def review_prompt(language: str = "python") -> str:
    """A reusable code-review prompt."""
    return f"Review this {language} code for bugs and security issues."


# ── group: namespaced sub-app ───────────────────────────────────────────────
finance = app.group("finance")


@finance.spell
def calculate_gst(amount: float, rate: float = 18.0) -> dict:
    """Calculate Indian GST on an amount."""
    gst = round(amount * rate / 100, 2)
    return {"base": amount, "rate": rate, "gst": gst, "total": amount + gst}


# ── semantic types (PII typed at the schema level) ─────────────────────────
@app.spell
def kyc(email: Email, aadhaar: Aadhaar, gstin: GSTIN, amount: Money) -> dict:
    """KYC + invoice check — all fields are typed, validated, PII-redacted."""
    return {
        "verified": True,
        "email_domain": str(email).split("@")[1],
        "aadhaar_last4": str(aadhaar)[-4:],
        "gstin": str(gstin),
        "amount": str(amount),
    }


# ── context-aware ──────────────────────────────────────────────────────────
@app.spell
def whoami(ctx: Context) -> dict:
    """Echo the caller's identity from the auth context."""
    return {"subject": ctx.subject, "scopes": ctx.scopes,
            "transport": ctx.transport, "trace_id": ctx.trace_id}


# ── spell chain (pipeline) ──────────────────────────────────────────────────
@app.spell
def parse_number(text: str) -> dict:
    return {"value": float(text)}


@app.spell
def double_value(value: float) -> float:
    return value * 2


app.chain("parse_number | double_value", name="parse_and_double")


if __name__ == "__main__":
    if "--mcp" in sys.argv:
        app.mcp_stdio()
    else:
        print("Open http://localhost:8765/dashboard")
        app.serve(port=8765)
