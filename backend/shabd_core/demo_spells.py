"""
demo_spells.py — the starter spell set, in ONE place.

Both the spells_service (which runs them over HTTP) and the agent_service
(which lets an LLM call them) import `register(app)` so the two never drift.
Replace / extend these with your real business tools.
"""
from __future__ import annotations


def register(app) -> None:
    @app.spell(tags=["math"])
    def add(a: float, b: float) -> float:
        """Add two numbers."""
        return a + b

    @app.spell(tags=["math"])
    def multiply(a: float, b: float) -> float:
        """Multiply two numbers."""
        return a * b

    @app.spell(tags=["finance"])
    def calculate_gst(amount: float, rate: float = 18.0) -> dict:
        """Compute GST for an amount at the given percentage rate."""
        gst = round(amount * rate / 100, 2)
        return {"amount": amount, "rate": rate, "gst": gst,
                "total": round(amount + gst, 2)}

    @app.spell(tags=["text"])
    def reverse(text: str) -> str:
        """Reverse a string."""
        return text[::-1]
