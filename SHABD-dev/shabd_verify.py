"""
shabd_verify.py — independent, zero-dependency verifier for a SHABD audit log.

"Don't trust us — verify." Hand this ONE file (plus the exported audit JSONL)
to an auditor / regulator / counterparty. They run it themselves, on their own
machine, and it re-derives the Grimoire hash chain from scratch to prove the log
was **not tampered with** — no access to SHABD, no database, and (for the
tamper-evidence check) **no secret required**.

How it works
------------
Each Grimoire page commits to the one before it: `leaf_hash = SHA-256(leaf)`
where `leaf` includes `prev` = the previous page's hash. Changing any field of
any past page changes its hash, which breaks the `prev` link of every page after
it. Re-computing the SHA-256 chain — which anyone can do, no key needed — detects
that. If you also pass the signing secret, each page's HMAC signature is checked
too (proving authenticity, i.e. that it was written by a holder of the secret).

Usage
-----
    python -m shabd_verify audit.jsonl                 # verify the hash chain
    python -m shabd_verify audit.jsonl --secret <hex>  # also verify signatures
    python -m shabd_verify audit.jsonl --json          # machine-readable output

Standard library only: json, hashlib, hmac, argparse, sys.
"""
from __future__ import annotations

import hashlib
import hmac
import json

GENESIS = "0" * 64
_LEAF_FIELDS = ("prev", "seq", "ts", "trace_id", "spell",
                "subject", "args_hash", "result_hash", "ok")


def _canonical(obj) -> str:
    # MUST match shabd.Grimoire._canonical exactly.
    return json.dumps(obj, sort_keys=True, default=str, separators=(",", ":"))


def _sha(obj) -> str:
    return hashlib.sha256(_canonical(obj).encode()).hexdigest()


def _decode_secret(raw: str) -> bytes:
    raw = raw.strip()
    if len(raw) >= 32 and len(raw) % 2 == 0 and all(
            c in "0123456789abcdefABCDEF" for c in raw):
        try:
            return bytes.fromhex(raw)
        except ValueError:
            pass
    return raw.encode()


def verify_pages(pages: list[dict], *, secret: bytes | None = None) -> dict:
    """Verify a list of audit pages. Returns a structured result:

        {ok, count, head, checked_signatures, reason?, break_seq?}
    """
    prev = GENESIS
    for i, page in enumerate(pages):
        seq = page.get("seq", i)
        # 1) chain link
        if page.get("prev") != prev:
            return {"ok": False, "count": len(pages), "break_seq": seq,
                    "reason": f"chain break at seq {seq}: prev link mismatch",
                    "checked_signatures": secret is not None}
        # 2) recompute the leaf hash
        try:
            leaf = {k: page[k] for k in _LEAF_FIELDS}
        except KeyError as e:
            return {"ok": False, "count": len(pages), "break_seq": seq,
                    "reason": f"page seq {seq} missing field {e}",
                    "checked_signatures": secret is not None}
        recomputed = _sha(leaf)
        if recomputed != page.get("hash"):
            return {"ok": False, "count": len(pages), "break_seq": seq,
                    "reason": f"tamper detected at seq {seq}: hash mismatch "
                              f"(a field was altered)",
                    "checked_signatures": secret is not None}
        # 3) optional signature check
        if secret is not None:
            expected = hmac.new(secret, recomputed.encode(),
                                hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, page.get("sig", "")):
                return {"ok": False, "count": len(pages), "break_seq": seq,
                        "reason": f"signature invalid at seq {seq}",
                        "checked_signatures": True}
        prev = recomputed
    return {"ok": True, "count": len(pages), "head": prev,
            "checked_signatures": secret is not None,
            "reason": "chain intact"}


def load_jsonl(path: str) -> list[dict]:
    pages = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            pages.append(json.loads(line))
    return pages


def verify_file(path: str, *, secret: bytes | None = None) -> dict:
    return verify_pages(load_jsonl(path), secret=secret)


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys
    ap = argparse.ArgumentParser(
        prog="shabd_verify",
        description="Independently verify a SHABD audit log (Grimoire chain).")
    ap.add_argument("path", help="path to the exported audit JSONL file")
    ap.add_argument("--secret", default=None,
                    help="signing secret (hex or utf-8) to ALSO verify HMAC "
                         "signatures; omit to verify the hash chain only")
    ap.add_argument("--json", action="store_true",
                    help="emit machine-readable JSON")
    args = ap.parse_args(argv)

    secret = _decode_secret(args.secret) if args.secret else None
    try:
        result = verify_file(args.path, secret=secret)
    except FileNotFoundError:
        print(f"error: file not found: {args.path}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as e:
        print(f"error: not a valid audit JSONL file: {e}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result))
        return 0 if result["ok"] else 1

    if result["ok"]:
        sig = "hash chain + signatures" if result["checked_signatures"] \
            else "hash chain"
        print(f"✓ VERIFIED — {result['count']} audited actions, {sig} intact.")
        print(f"  head: {result['head']}")
        print("  The audit log has not been tampered with.")
        return 0
    print(f"✗ FAILED — {result['reason']}")
    print("  The audit log has been altered or is inconsistent.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
