"""
AML / Transaction-Monitoring pack.

Built around three patterns FIU-IND and RBI inspectors look for:

  * Velocity            — same party making N transactions in T minutes
  * Structuring         — many sub-threshold deposits to evade CTR
  * Beneficial owner    — UBO present and named below given thresholds

Every check stamps the Grimoire chain so a regulator can reconstruct
which alert fired on which transaction.

This pack is *detection*, not *case management*. Pair it with
`shabd_packs.regtech` to fire an STR when an alert needs to be
reported to FIU-IND.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

from shabd import SHABD, ConjureError, Money

__all__ = ["install", "AMLState", "block_if_structuring"]


class AMLState:
    """Thread-safe in-memory state for AML rules. Swap for Redis or a
    risk DB in production — the interface below is what the spells
    use."""

    def __init__(self,
                 velocity_window_s: float = 300.0,
                 velocity_count: int = 10,
                 structuring_threshold_inr: float = 50_000.0,
                 structuring_window_s: float = 86_400.0,
                 structuring_count: int = 5):
        self._lock = threading.Lock()
        self._velocity: dict[str, deque] = defaultdict(
            lambda: deque(maxlen=10_000))
        self._structuring: dict[str, deque] = defaultdict(
            lambda: deque(maxlen=10_000))
        self.velocity_window_s = velocity_window_s
        self.velocity_count = velocity_count
        self.structuring_threshold_inr = structuring_threshold_inr
        self.structuring_window_s = structuring_window_s
        self.structuring_count = structuring_count

    def record(self, party: str, amount_inr: float) -> None:
        now = time.time()
        with self._lock:
            self._velocity[party].append(now)
            if amount_inr < self.structuring_threshold_inr:
                self._structuring[party].append((now, amount_inr))

    def velocity_count_in_window(self, party: str) -> int:
        cutoff = time.time() - self.velocity_window_s
        with self._lock:
            return sum(1 for t in self._velocity[party] if t > cutoff)

    def structuring_count_in_window(self, party: str) -> int:
        cutoff = time.time() - self.structuring_window_s
        with self._lock:
            return sum(1 for t, _ in self._structuring[party] if t > cutoff)


def install(app: SHABD, *,
            state: AMLState | None = None,
            max_concurrent: int = 200) -> AMLState:
    state = state or AMLState()

    @app.spell(scopes=["compliance"], max_concurrent=max_concurrent,
               idempotent=False, tags=["aml"])
    def record_transaction(party: str, amount: Money) -> dict:
        """Record a transaction for AML purposes. Returns any alerts
        that fired so the caller can decide to block / hold / file STR."""
        amount_str = str(amount)
        value = float(amount_str.split()[0])
        state.record(party, value)
        alerts = []
        vel = state.velocity_count_in_window(party)
        if vel >= state.velocity_count:
            alerts.append({
                "rule": "velocity",
                "count": vel,
                "window_s": state.velocity_window_s,
            })
        struc = state.structuring_count_in_window(party)
        if struc >= state.structuring_count:
            alerts.append({
                "rule": "structuring",
                "count": struc,
                "window_s": state.structuring_window_s,
                "threshold_inr": state.structuring_threshold_inr,
            })
        return {
            "party": party, "amount": amount_str,
            "alerts": alerts,
            "clear": not alerts,
            "recorded_at": time.time(),
        }

    @app.spell(scopes=["compliance"], idempotent=True, cache_ttl=10,
               tags=["aml"])
    def aml_inquiry(party: str) -> dict:
        """Read-only inquiry for a party's current AML state."""
        return {
            "party": party,
            "velocity_count": state.velocity_count_in_window(party),
            "velocity_threshold": state.velocity_count,
            "velocity_window_s": state.velocity_window_s,
            "structuring_count": state.structuring_count_in_window(party),
            "structuring_threshold": state.structuring_count,
        }

    @app.spell(scopes=["compliance"], idempotent=True, tags=["aml"])
    def beneficial_owner_check(legal_entity: str,
                               declared_ubos: list[str],
                               declared_threshold_pct: float) -> dict:
        """Beneficial-owner declaration check. RBI requires UBOs >=25%
        to be named (in some segments 10%)."""
        if declared_threshold_pct > 25.0:
            return {
                "legal_entity": legal_entity,
                "ubos": declared_ubos,
                "ok": False,
                "reason": "no UBO named at or above the 25% threshold",
            }
        if not declared_ubos:
            return {
                "legal_entity": legal_entity, "ubos": [],
                "ok": False, "reason": "UBO list is empty",
            }
        return {
            "legal_entity": legal_entity,
            "ubos": declared_ubos,
            "threshold_pct": declared_threshold_pct,
            "ok": True,
        }

    return state


def block_if_structuring(party: str, state: AMLState) -> None:
    """Helper for use inside other spells (e.g. deposit / wire)."""
    if state.structuring_count_in_window(party) >= state.structuring_count:
        raise ConjureError(
            f"party '{party}' has triggered the structuring rule",
            code="aml_structuring",
            hint="Hold the transaction and route to compliance review.",
        )
