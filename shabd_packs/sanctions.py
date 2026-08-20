"""
Sanctions and PEP screening pack.

When you do this for a bank:

    from shabd_packs import sanctions
    sanctions.install(app, lists=("OFAC", "UN", "RBI"))

You get three spells the model can call directly:

    screen_party(name, country, dob_or_inc_date) -> dict
    screen_transaction(party_a, party_b, amount, ccy) -> dict
    list_status(list_name) -> dict   # freshness check

Every screening call lands in the Grimoire chain, so the bank can prove
to FIU-IND that party X was screened on date Y with engine version Z.

The default in-process list is a tiny demo. Production deployments
should plug in a real list refresher (Refinitiv WorldCheck, LSEG, Dow
Jones, or RBI's published lists) by passing `loader=`.
"""
from __future__ import annotations

import time
import typing as t

from shabd import SHABD, ConjureError, Money

__all__ = ["install", "Lister"]


_DEFAULT_DEMO_LIST = {
    "OFAC": {"Vladimir Putin", "Kim Jong Un", "Acme Sanctioned Corp"},
    "UN":   {"Joseph Kony", "Al-Shabaab"},
    "RBI":  {"FraudFirm Pvt Ltd", "Doe John"},
}


class Lister:
    """Source of truth for sanctions / PEP lists. Override `.refresh()`
    to fetch from a real upstream — the default returns the bundled demo
    set so tests work offline."""

    def __init__(self, lists: t.Iterable[str] = ("OFAC", "UN", "RBI")):
        self.enabled = tuple(lists)
        self._cache: dict = {k: _DEFAULT_DEMO_LIST.get(k, set())
                             for k in self.enabled}
        self._loaded_at = time.time()

    def refresh(self) -> dict:
        self._loaded_at = time.time()
        return {"refreshed_at": self._loaded_at, "lists": self.enabled}

    def contains(self, name: str) -> list[str]:
        n = name.strip().lower()
        return [lst for lst, names in self._cache.items()
                if any(n == s.lower() for s in names)]

    def status(self) -> dict:
        return {
            "lists": self.enabled,
            "loaded_at": self._loaded_at,
            "age_seconds": round(time.time() - self._loaded_at, 1),
            "size_per_list": {k: len(v) for k, v in self._cache.items()},
        }


def install(app: SHABD, *,
            lists: t.Iterable[str] = ("OFAC", "UN", "RBI"),
            loader: Lister | None = None,
            max_concurrent: int = 100) -> Lister:
    lister = loader or Lister(lists)

    @app.spell(scopes=["compliance"], max_concurrent=max_concurrent,
               idempotent=True, cache_ttl=30, tags=["sanctions"])
    def screen_party(name: str, country: str = "",
                     dob_or_inc_date: str = "") -> dict:
        """Screen a party name against the loaded sanctions and PEP lists.

        Returns the matched lists; empty list means clear. Cached for 30 s
        per identical input so repeat checks during a long flow are cheap.
        """
        hits = lister.contains(name)
        return {
            "name": name, "country": country,
            "matched_lists": hits,
            "clear": len(hits) == 0,
            "screened_at": time.time(),
        }

    @app.spell(scopes=["compliance"], max_concurrent=max_concurrent,
               idempotent=True, tags=["sanctions"])
    def screen_transaction(party_a: str, party_b: str,
                           amount: Money) -> dict:
        """Screen both parties of a transaction in one shot."""
        a = lister.contains(party_a)
        b = lister.contains(party_b)
        return {
            "party_a": party_a, "party_b": party_b,
            "amount": str(amount),
            "matched_lists_a": a,
            "matched_lists_b": b,
            "clear": not (a or b),
            "screened_at": time.time(),
        }

    @app.spell(scopes=["compliance"], idempotent=True, cache_ttl=10,
               tags=["sanctions"])
    def list_status() -> dict:
        """Freshness check — FIU/RBI auditors will ask."""
        return lister.status()

    @app.spell(scopes=["compliance-admin"], idempotent=False,
               tags=["sanctions"])
    def refresh_lists() -> dict:
        """Operator-triggered list refresh. The result lands in the
        audit chain so the bank can prove when lists were updated."""
        return lister.refresh()

    return lister


def block_if_sanctioned(party: str, lister: Lister) -> None:
    """Helper for use inside other spells:

        @app.spell
        def transfer(..., from_party, to_party):
            block_if_sanctioned(from_party, lister)
            block_if_sanctioned(to_party, lister)
            ...
    """
    hits = lister.contains(party)
    if hits:
        raise ConjureError(
            f"party '{party}' matches sanctions list(s) {hits}",
            code="sanctions_hit",
            hint="Block the transfer and file an STR with FIU-IND.",
        )
