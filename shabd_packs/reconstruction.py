"""
Trade Reconstruction-as-a-Service pack.

Sales line: "Regulator pooche 'March 15 ko algo ne ye trade kyun
kiya' — humara answer ek cryptographic proof hai, spreadsheet nahi."

This pack wraps SHABD's Grimoire + replay surface into a small set of
spells priced as "reconstruction queries":

  reconstruct_by_trace(trace_id)        — pull the full audit page for a call
  reconstruct_by_spell(spell, since)    — every call to a spell in a window
  reconstruct_by_subject(subject)       — every call by a user
  reconstruct_chain_proof(seq_from, seq_to) — Merkle-style proof slice
  replay_call(trace_id)                  — actually re-execute (if safe)

Each query is logged as its own Grimoire page, so the regulator can
verify that the query itself happened, not just the underlying trade.
This is the "$50K per query" line item in the SHABD price card.
"""
from __future__ import annotations

import time

from shabd import SHABD, ConjureError

__all__ = ["install"]


def install(app: SHABD, *, max_concurrent: int = 50) -> None:

    @app.spell(scopes=["auditor", "compliance"], idempotent=True,
               cache_ttl=2, max_concurrent=max_concurrent,
               tags=["reconstruction"])
    def reconstruct_by_trace(trace_id: str) -> dict:
        """Pull every Grimoire page that shares a trace_id, plus the
        in-memory CallRecord (if still hot). The output is structured
        so a regulator can paste it into a report."""
        pages = [p for p in app.grimoire.pages()
                 if p.get("trace_id") == trace_id]
        records = [r for r in app._recent_calls
                   if r.trace_id == trace_id]
        if not pages and not records:
            raise ConjureError(f"no trail for trace_id {trace_id!r}",
                               code="not_found",
                               hint="Trace may have aged out — pull from "
                                    "the on-disk Grimoire log instead.")
        return {
            "trace_id": trace_id,
            "pages": pages,
            "in_memory_records": [
                {"spell": r.spell, "subject": r.subject,
                 "ok": r.ok, "elapsed_ms": r.elapsed_ms,
                 "error_code": r.error_code, "ts": r.ts}
                for r in records
            ],
            "verified": app.grimoire.verify().get("ok", False),
        }

    @app.spell(scopes=["auditor", "compliance"], idempotent=True,
               cache_ttl=2, max_concurrent=max_concurrent,
               tags=["reconstruction"])
    def reconstruct_by_spell(spell: str,
                             since_seq: int = 0,
                             limit: int = 200) -> dict:
        """Every audited call to one spell since a sequence point."""
        pages = [p for p in app.grimoire.pages(since_seq=since_seq,
                                                limit=10_000)
                 if p.get("spell") == spell][:limit]
        return {
            "spell": spell, "since_seq": since_seq,
            "count": len(pages), "pages": pages,
        }

    @app.spell(scopes=["auditor", "compliance"], idempotent=True,
               cache_ttl=2, max_concurrent=max_concurrent,
               tags=["reconstruction"])
    def reconstruct_by_subject(subject: str,
                               since_seq: int = 0,
                               limit: int = 500) -> dict:
        """Every audited call by one authenticated subject."""
        pages = [p for p in app.grimoire.pages(since_seq=since_seq,
                                                limit=10_000)
                 if p.get("subject") == subject][:limit]
        return {
            "subject": subject, "since_seq": since_seq,
            "count": len(pages), "pages": pages,
        }

    @app.spell(scopes=["auditor"], idempotent=True, cache_ttl=10,
               max_concurrent=max_concurrent, tags=["reconstruction"])
    def reconstruct_chain_proof(seq_from: int = 0,
                                seq_to: int | None = None) -> dict:
        """Returns the hashes that prove `[seq_from .. seq_to]` is
        intact, without dumping any payload. Pass this to an external
        auditor who only needs the proof, not the data."""
        pages = app.grimoire.pages(since_seq=seq_from, limit=10_000)
        if seq_to is not None:
            pages = [p for p in pages if p["seq"] <= seq_to]
        slim = [{"seq": p["seq"], "prev": p["prev"],
                 "hash": p["hash"], "sig": p["sig"], "ts": p["ts"]}
                for p in pages]
        v = app.grimoire.verify()
        return {
            "seq_from": seq_from,
            "seq_to": seq_to if seq_to is not None else (
                pages[-1]["seq"] if pages else None),
            "page_count": len(slim),
            "proof": slim,
            "head": app.grimoire.head(),
            "chain_ok": v.get("ok", False),
            "generated_at": time.time(),
        }

    @app.spell(scopes=["auditor"], idempotent=False,
               max_concurrent=max_concurrent, tags=["reconstruction"])
    def replay_call(trace_id: str) -> dict:
        """Re-execute the original call from its recorded args.

        The replay itself is recorded as a new Grimoire page so the
        regulator can distinguish 'original' from 'replayed'."""
        record = next((r for r in app._recent_calls
                       if r.trace_id == trace_id), None)
        if record is None:
            raise ConjureError("no in-memory snapshot for trace_id",
                               code="not_found",
                               hint="Replay only works while the args "
                                    "snapshot is in the rolling cache.")
        if record.args_snapshot is None:
            raise ConjureError("trace has no recorded args",
                               code="no_snapshot")
        if record.spell not in app._spells:
            raise ConjureError(f"spell {record.spell!r} no longer "
                                "registered", code="spell_not_found")
        result = app.invoke(record.spell, dict(record.args_snapshot))
        return {
            "original_trace_id": trace_id,
            "original_spell": record.spell,
            "replay_result": result,
            "replayed_at": time.time(),
        }
