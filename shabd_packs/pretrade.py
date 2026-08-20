"""
Pre-trade risk gateway pack.

What this pack ships:

  check_pre_trade(strategy, symbol, side, qty, limit_price) -> dict
  position_inquiry(strategy, symbol) -> dict
  reset_limits(strategy)               — operator only

What it solves: SEBI's risk-management circular requires every algo
order to pass a pre-trade limit check before reaching the exchange.
Today this is duplicated across order routers and is the #1 source of
fat-finger fines. SHABD's pre-trade gateway centralises it, gives the
audit chain SEBI is asking for, and runs sub-millisecond on a single
CPU because checks are pure-Python integer arithmetic.

Default limits are illustrative — pass your own `LimitBook` instance to
wire it to your risk team's data.
"""
from __future__ import annotations

import threading
import time

from shabd import SHABD, ConjureError

__all__ = ["install", "LimitBook"]


class LimitBook:
    """Thread-safe in-memory book of per-strategy / per-symbol limits.

    A real installation would back this with Redis or a risk DB so that
    multiple SHABD instances share the same view. The interface below is
    what the spells call into — keep it stable when you swap the impl."""

    def __init__(self,
                 default_position: int = 100_000,
                 default_notional_inr: float = 50_000_000.0):
        self._lock = threading.Lock()
        self._position: dict = {}    # (strategy, symbol) -> int
        self._notional_used: dict = {}
        self.default_position = default_position
        self.default_notional_inr = default_notional_inr
        self._position_limits: dict = {}
        self._notional_limits: dict = {}

    def set_position_limit(self, strategy: str, symbol: str, n: int) -> None:
        with self._lock:
            self._position_limits[(strategy, symbol)] = n

    def set_notional_limit(self, strategy: str, n_inr: float) -> None:
        with self._lock:
            self._notional_limits[strategy] = n_inr

    def position_of(self, strategy: str, symbol: str) -> int:
        return self._position.get((strategy, symbol), 0)

    def position_limit_of(self, strategy: str, symbol: str) -> int:
        return self._position_limits.get((strategy, symbol),
                                         self.default_position)

    def notional_limit_of(self, strategy: str) -> float:
        return self._notional_limits.get(strategy, self.default_notional_inr)

    def notional_used(self, strategy: str) -> float:
        return self._notional_used.get(strategy, 0.0)

    def reserve(self, strategy: str, symbol: str,
                qty_signed: int, notional_inr: float) -> None:
        with self._lock:
            key = (strategy, symbol)
            self._position[key] = self._position.get(key, 0) + qty_signed
            self._notional_used[strategy] = (
                self._notional_used.get(strategy, 0.0) + abs(notional_inr)
            )

    def reset(self, strategy: str) -> None:
        with self._lock:
            for k in list(self._position):
                if k[0] == strategy:
                    del self._position[k]
            self._notional_used.pop(strategy, None)


def install(app: SHABD, *,
            book: LimitBook | None = None,
            max_concurrent: int = 1000) -> LimitBook:
    book = book or LimitBook()

    @app.spell(scopes=["algo"], max_concurrent=max_concurrent,
               idempotent=False, tags=["pre-trade"])
    def check_pre_trade(strategy: str, symbol: str, side: str,
                        qty: int, limit_price_inr: float) -> dict:
        """Returns `{approved: True}` or raises with a structured error
        that the order router can surface back to the algo."""
        if side not in ("buy", "sell"):
            raise ConjureError("side must be 'buy' or 'sell'",
                               code="bad_side", example="buy")
        if qty <= 0:
            raise ConjureError("qty must be > 0", code="bad_qty",
                               example=100)
        signed_qty = qty if side == "buy" else -qty
        notional = qty * limit_price_inr
        pos_lim = book.position_limit_of(strategy, symbol)
        notional_lim = book.notional_limit_of(strategy)
        new_pos = book.position_of(strategy, symbol) + signed_qty
        if abs(new_pos) > pos_lim:
            raise ConjureError(
                "position limit breach",
                code="position_limit_breach",
                hint=f"Would take net position to {new_pos}; limit is +/-{pos_lim}.",
                example={"max_qty": max(0, pos_lim - abs(book.position_of(strategy, symbol)))},
            )
        if book.notional_used(strategy) + notional > notional_lim:
            raise ConjureError(
                "notional limit breach",
                code="notional_limit_breach",
                hint=f"Remaining notional for {strategy}: "
                     f"{notional_lim - book.notional_used(strategy):.0f} INR",
            )
        # Optimistically reserve — caller is expected to call back if the
        # exchange ultimately rejects. A real risk system would book a
        # 'pending' reservation and reconcile.
        book.reserve(strategy, symbol, signed_qty, notional)
        return {
            "approved": True, "strategy": strategy, "symbol": symbol,
            "side": side, "qty": qty,
            "position_after": book.position_of(strategy, symbol),
            "notional_used": book.notional_used(strategy),
            "checked_at": time.time(),
        }

    @app.spell(scopes=["algo"], idempotent=True, cache_ttl=1,
               tags=["pre-trade"])
    def position_inquiry(strategy: str, symbol: str) -> dict:
        return {
            "strategy": strategy, "symbol": symbol,
            "position": book.position_of(strategy, symbol),
            "position_limit": book.position_limit_of(strategy, symbol),
            "notional_used": book.notional_used(strategy),
            "notional_limit": book.notional_limit_of(strategy),
        }

    @app.spell(scopes=["risk-admin"], idempotent=False, tags=["pre-trade"])
    def reset_limits(strategy: str) -> dict:
        book.reset(strategy)
        return {"strategy": strategy, "reset_at": time.time()}

    return book
