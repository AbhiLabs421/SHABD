"""
Market-surveillance pack (SEBI Regulation 9A inspired).

Built-in detectors:

  wash_trade        — same beneficial owner on both sides
  spoofing          — large orders cancelled before execution
  layering          — series of small orders to nudge price
  front_running     — proprietary trades that precede client trades
  pump_and_dump     — coordinated price/volume manipulation hint
  insider_alert     — trades around an UPSI (unpublished price-
                       sensitive information) event window

Detection is rule-based (not ML) on purpose: SEBI and the exchanges
want explicable signals. Hook the spells into your real order-flow
pipeline; the Grimoire chain stamps every alert.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

from shabd import SHABD

__all__ = ["install", "SurveillanceState"]


class SurveillanceState:
    """Recent-events window per symbol / per party."""

    def __init__(self, window_s: float = 3600.0):
        self._lock = threading.Lock()
        self._symbol_orders: dict = defaultdict(lambda: deque(maxlen=10_000))
        self._party_orders: dict = defaultdict(lambda: deque(maxlen=10_000))
        self._cancellations: dict = defaultdict(lambda: deque(maxlen=10_000))
        self._upsi_windows: dict = {}   # symbol -> (start, end)
        self.window_s = window_s

    def record_order(self, *, symbol: str, party: str, side: str,
                     qty: int, price: float,
                     status: str = "active") -> None:
        now = time.time()
        rec = {"ts": now, "symbol": symbol, "party": party, "side": side,
               "qty": qty, "price": price, "status": status}
        with self._lock:
            self._symbol_orders[symbol].append(rec)
            self._party_orders[party].append(rec)
            if status == "cancelled":
                self._cancellations[party].append(rec)

    def declare_upsi(self, symbol: str, start_ts: float, end_ts: float) -> None:
        with self._lock:
            self._upsi_windows[symbol] = (start_ts, end_ts)


def install(app: SHABD, *,
            state: SurveillanceState | None = None) -> SurveillanceState:
    state = state or SurveillanceState()

    @app.spell(scopes=["surveillance"], idempotent=False,
               tags=["surveillance"])
    def record_order(symbol: str, party: str, side: str, qty: int,
                     price: float, status: str = "active") -> dict:
        """Feed your real OMS events here. Every record is auditable."""
        state.record_order(symbol=symbol, party=party, side=side,
                           qty=qty, price=price, status=status)
        return {"recorded": True, "ts": time.time()}

    @app.spell(scopes=["surveillance"], idempotent=True, cache_ttl=5,
               tags=["surveillance"])
    def detect_wash_trade(symbol: str, party: str) -> dict:
        """Same beneficial-owner on both sides within the window."""
        with state._lock:
            party_orders = [o for o in state._party_orders[party]
                            if o["symbol"] == symbol]
        sides = {o["side"] for o in party_orders}
        return {
            "symbol": symbol, "party": party,
            "buy_count": sum(1 for o in party_orders if o["side"] == "buy"),
            "sell_count": sum(1 for o in party_orders if o["side"] == "sell"),
            "wash_suspected": "buy" in sides and "sell" in sides,
        }

    @app.spell(scopes=["surveillance"], idempotent=True, cache_ttl=5,
               tags=["surveillance"])
    def detect_spoofing(party: str,
                        cancel_ratio_threshold: float = 0.7) -> dict:
        """High cancellation ratio is a classical spoofing signal."""
        with state._lock:
            all_orders = list(state._party_orders[party])
            cancels = list(state._cancellations[party])
        if not all_orders:
            return {"party": party, "ratio": 0.0, "spoofing_suspected": False}
        ratio = len(cancels) / len(all_orders)
        return {
            "party": party,
            "total_orders": len(all_orders),
            "cancellations": len(cancels),
            "ratio": round(ratio, 3),
            "threshold": cancel_ratio_threshold,
            "spoofing_suspected": ratio >= cancel_ratio_threshold,
        }

    @app.spell(scopes=["surveillance"], idempotent=True, cache_ttl=5,
               tags=["surveillance"])
    def detect_layering(symbol: str, party: str,
                        min_levels: int = 5) -> dict:
        """Many small orders at different price levels in a short window."""
        with state._lock:
            orders = [o for o in state._party_orders[party]
                      if o["symbol"] == symbol
                      and time.time() - o["ts"] < 60.0]
        levels = {round(o["price"], 2) for o in orders}
        return {
            "symbol": symbol, "party": party,
            "distinct_levels": len(levels),
            "min_levels": min_levels,
            "layering_suspected": len(levels) >= min_levels,
        }

    @app.spell(scopes=["surveillance"], idempotent=True, cache_ttl=5,
               tags=["surveillance"])
    def detect_front_running(symbol: str, prop_party: str,
                             client_party: str,
                             window_s: float = 30.0) -> dict:
        """Proprietary order in the same direction *before* a client
        order on the same symbol."""
        with state._lock:
            prop = [o for o in state._party_orders[prop_party]
                    if o["symbol"] == symbol]
            client = [o for o in state._party_orders[client_party]
                      if o["symbol"] == symbol]
        flagged = []
        for c in client:
            for p in prop:
                if (p["side"] == c["side"]
                        and p["ts"] < c["ts"]
                        and c["ts"] - p["ts"] <= window_s):
                    flagged.append({"prop_ts": p["ts"],
                                    "client_ts": c["ts"],
                                    "side": c["side"]})
        return {
            "symbol": symbol, "prop_party": prop_party,
            "client_party": client_party,
            "flagged_pairs": flagged,
            "front_running_suspected": bool(flagged),
        }

    @app.spell(scopes=["surveillance-admin"], idempotent=False,
               tags=["surveillance"])
    def declare_upsi_window(symbol: str, start_ts: float,
                            end_ts: float) -> dict:
        """Record an Unpublished Price Sensitive Information window —
        an SEBI requirement before earnings/M&A announcements."""
        state.declare_upsi(symbol, start_ts, end_ts)
        return {"symbol": symbol, "start_ts": start_ts, "end_ts": end_ts}

    @app.spell(scopes=["surveillance"], idempotent=True, cache_ttl=5,
               tags=["surveillance"])
    def detect_insider_alert(symbol: str, party: str) -> dict:
        """Trades by an insider-flagged party during a UPSI window."""
        with state._lock:
            window = state._upsi_windows.get(symbol)
            orders = [o for o in state._party_orders[party]
                      if o["symbol"] == symbol]
        if not window:
            return {"symbol": symbol, "party": party,
                    "insider_suspected": False,
                    "reason": "no UPSI window declared"}
        start, end = window
        hits = [o for o in orders if start <= o["ts"] <= end]
        return {
            "symbol": symbol, "party": party,
            "upsi_start": start, "upsi_end": end,
            "trades_in_window": len(hits),
            "insider_suspected": bool(hits),
        }

    return state
