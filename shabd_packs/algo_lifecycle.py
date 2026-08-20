"""
Algo lifecycle audit chain (SEBI/exchange algo-approval mandate).

SEBI requires that every algorithmic trading strategy be exchange-
approved before deployment. This pack records the full lifecycle —
test, approval, deployment, version change — into the Grimoire chain
so the exchange or SEBI can verify which algo was running when.

Spells:

  register_algo(name, version, owner)
  submit_test_results(algo_id, test_report)
  request_exchange_approval(algo_id, exchange_member_id)
  record_approval_signature(algo_id, signer, signature)
  deploy_algo(algo_id, env)
  retire_algo(algo_id, reason)
  algo_history(algo_id)
"""
from __future__ import annotations

import threading
import time
import uuid

from shabd import SHABD, ConjureError

__all__ = ["install", "AlgoRegistry"]


class AlgoRegistry:
    def __init__(self):
        self._lock = threading.Lock()
        self._algos: dict = {}
        self._events: dict = {}    # algo_id -> [event dicts]

    def _add(self, algo_id: str, event: dict) -> None:
        self._events.setdefault(algo_id, []).append(event)

    def create(self, name: str, version: str, owner: str) -> dict:
        with self._lock:
            algo_id = f"ALGO-{uuid.uuid4().hex[:10]}"
            rec = {"algo_id": algo_id, "name": name, "version": version,
                   "owner": owner, "status": "registered",
                   "created_at": time.time()}
            self._algos[algo_id] = rec
            self._add(algo_id, {"event": "registered",
                                "ts": time.time(), "details": dict(rec)})
            return rec

    def event(self, algo_id: str, kind: str, details: dict) -> dict:
        with self._lock:
            if algo_id not in self._algos:
                raise ConjureError(f"unknown algo {algo_id}",
                                   code="not_found")
            now = time.time()
            self._add(algo_id, {"event": kind, "ts": now,
                                "details": details})
            self._algos[algo_id]["status"] = kind
            return {"algo_id": algo_id, "event": kind, "ts": now,
                    "details": details}

    def history(self, algo_id: str) -> list:
        with self._lock:
            return list(self._events.get(algo_id, []))


def install(app: SHABD, *,
            registry: AlgoRegistry | None = None) -> AlgoRegistry:
    registry = registry or AlgoRegistry()

    @app.spell(scopes=["quant", "risk-admin"], idempotent=False,
               tags=["algo-lifecycle"])
    def register_algo(name: str, version: str, owner: str) -> dict:
        return registry.create(name, version, owner)

    @app.spell(scopes=["quant"], idempotent=False, tags=["algo-lifecycle"])
    def submit_test_results(algo_id: str, test_report: dict) -> dict:
        return registry.event(algo_id, "tested", test_report)

    @app.spell(scopes=["risk-admin"], idempotent=False,
               tags=["algo-lifecycle"])
    def request_exchange_approval(algo_id: str,
                                  exchange_member_id: str) -> dict:
        return registry.event(algo_id, "approval-requested",
                              {"exchange_member_id": exchange_member_id})

    @app.spell(scopes=["risk-admin"], idempotent=False,
               tags=["algo-lifecycle"])
    def record_approval_signature(algo_id: str, signer: str,
                                  signature: str) -> dict:
        return registry.event(algo_id, "approved",
                              {"signer": signer, "signature": signature})

    @app.spell(scopes=["risk-admin"], idempotent=False,
               tags=["algo-lifecycle"])
    def deploy_algo(algo_id: str, env: str = "production") -> dict:
        return registry.event(algo_id, "deployed", {"env": env})

    @app.spell(scopes=["risk-admin"], idempotent=False,
               tags=["algo-lifecycle"])
    def retire_algo(algo_id: str, reason: str) -> dict:
        return registry.event(algo_id, "retired", {"reason": reason})

    @app.spell(scopes=["quant", "risk-admin", "compliance"],
               idempotent=True, cache_ttl=10, tags=["algo-lifecycle"])
    def algo_history(algo_id: str) -> dict:
        return {"algo_id": algo_id, "events": registry.history(algo_id)}

    return registry
