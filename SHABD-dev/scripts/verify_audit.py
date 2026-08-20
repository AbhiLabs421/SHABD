#!/usr/bin/env python3
"""
Standalone verifier for an on-disk Grimoire JSONL chain.

Use this from an auditor's machine, even without SHABD installed —
the only dependency is the Python standard library.

Usage:
    python scripts/verify_audit.py /var/lib/shabd/audit.jsonl
    SHABD_SECRET=hex python scripts/verify_audit.py /path/to/audit.jsonl

Exit codes:
    0   chain is intact
    1   chain is broken / signature invalid / file unreadable
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys

GENESIS = "0" * 64


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, default=str, separators=(",", ":"))


def sha(obj) -> str:
    return hashlib.sha256(canonical(obj).encode()).hexdigest()


def verify(path: str, secret: bytes | None) -> dict:
    if not os.path.exists(path):
        return {"ok": False, "reason": "file_not_found"}
    prev = GENESIS
    n = 0
    with open(path, encoding="utf-8") as fh:
        for line_no, raw in enumerate(fh, 1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                page = json.loads(raw)
            except json.JSONDecodeError:
                return {"ok": False, "reason": "corrupt_line",
                        "at_line": line_no}
            leaf = {k: page[k] for k in (
                "prev", "seq", "ts", "trace_id", "spell",
                "subject", "args_hash", "result_hash", "ok",
            )}
            if leaf["prev"] != prev:
                return {"ok": False, "reason": "chain_break",
                        "at_seq": page.get("seq"), "at_line": line_no}
            if sha(leaf) != page["hash"]:
                return {"ok": False, "reason": "hash_mismatch",
                        "at_seq": page.get("seq"), "at_line": line_no}
            if secret is not None:
                expected = hmac.new(secret, page["hash"].encode(),
                                    hashlib.sha256).hexdigest()
                if not hmac.compare_digest(expected, page["sig"]):
                    return {"ok": False, "reason": "bad_signature",
                            "at_seq": page.get("seq"), "at_line": line_no}
            prev = page["hash"]
            n += 1
    return {"ok": True, "pages": n, "head": prev,
            "signature_checked": secret is not None}


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 1
    secret_env = os.environ.get("SHABD_SECRET", "")
    secret: bytes | None = secret_env.encode() if secret_env else None
    if secret is None:
        print("Note: SHABD_SECRET not set — verifying chain structure only "
              "(signatures NOT checked).", file=sys.stderr)
    out = verify(sys.argv[1], secret)
    print(json.dumps(out, indent=2))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
