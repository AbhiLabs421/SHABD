"""
Trading-style order placement with concurrency caps and audit chain.

    python examples/trading_orders.py

Demonstrates:

  * `max_concurrent=N` — protect the matching engine from too many
    in-flight orders at once (semaphore, not just rate limiting).
  * `Idempotency-Key` — the same client-order-id never places two trades.
  * Grimoire — every place / cancel / fill is appended, hash-chained,
    signed; a regulator can prove integrity later.
"""
from __future__ import annotations

import os
import random
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shabd import SHABD, ConjureError

app = SHABD("trader",
            secret=os.environ.get("SHABD_SECRET", "x" * 32),
            require_auth=False,
            grimoire_log_path="/tmp/trader-audit.jsonl")


_orders: dict = {}


@app.spell(max_concurrent=200, idempotent=False)
def place_order(symbol: str, side: str, qty: int, limit_price: float,
                client_order_id: str) -> dict:
    """
    Place a limit order. Safe to retry only with the *same* Idempotency-Key.
    """
    if side not in ("buy", "sell"):
        raise ConjureError("side must be buy or sell",
                           code="bad_side",
                           hint="Pass exactly 'buy' or 'sell'.",
                           example="buy")
    if qty <= 0:
        raise ConjureError("qty must be > 0", code="bad_qty",
                           example=100)

    order_id = f"O-{uuid.uuid4().hex[:10]}"
    _orders[order_id] = {
        "order_id": order_id,
        "client_order_id": client_order_id,
        "symbol": symbol, "side": side, "qty": qty,
        "limit_price": limit_price,
        "status": "open",
        "ts": time.time(),
    }
    # Simulate matching-engine latency.
    time.sleep(random.uniform(0.01, 0.05))
    return _orders[order_id]


@app.spell(idempotent=True)
def cancel_order(order_id: str) -> dict:
    if order_id not in _orders:
        raise ConjureError("unknown order", code="not_found")
    _orders[order_id]["status"] = "cancelled"
    return {"ok": True, "order_id": order_id, "status": "cancelled"}


@app.spell(idempotent=True, cache_ttl=2)
def list_open() -> list:
    return [o for o in _orders.values() if o["status"] == "open"]


if __name__ == "__main__":
    if "--client" in sys.argv:
        from shabd_client import SHABDClient
        c = SHABDClient("http://localhost:8765")
        ide = f"buy-{uuid.uuid4()}"
        body = {
            "symbol": "RELIANCE",
            "side": "buy", "qty": 100, "limit_price": 2500.50,
            "client_order_id": ide,
        }
        o1 = c.cast("place_order", body, idempotency_key=ide)
        print("Order 1:", o1)
        o2 = c.cast("place_order", body, idempotency_key=ide)  # retry
        print("Order 2 (same key — no double-place):", o2)
        assert o1["order_id"] == o2["order_id"]
        print("\nGrimoire:", c.grimoire_verify())
    else:
        app.serve(port=8765)
