#!/usr/bin/env python3
"""
Pretty-print pages from a Grimoire JSONL audit file.

Usage:
    python scripts/dump_grimoire.py /var/lib/shabd/audit.jsonl
    python scripts/dump_grimoire.py /path/to/audit.jsonl --since 100 --limit 20
"""
from __future__ import annotations

import argparse
import json
import os
import sys


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("path")
    p.add_argument("--since", type=int, default=0,
                   help="first seq to show (default: 0)")
    p.add_argument("--limit", type=int, default=50,
                   help="how many pages to show (default: 50)")
    p.add_argument("--tail", action="store_true",
                   help="show the last N pages instead of the first N")
    args = p.parse_args()

    if not os.path.exists(args.path):
        print(f"file not found: {args.path}", file=sys.stderr)
        return 1

    pages = []
    with open(args.path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                pages.append(json.loads(line))
            except json.JSONDecodeError:
                pass

    pages = [p for p in pages if p.get("seq", 0) >= args.since]
    if args.tail:
        pages = pages[-args.limit:]
    else:
        pages = pages[: args.limit]

    print(f"Showing {len(pages)} pages from {args.path}\n")
    for page in pages:
        status = "OK " if page.get("ok") else "ERR"
        print(f"  seq={page['seq']:<5}  ts={page['ts']:.1f}  "
              f"{status}  {page['spell']:<25}  "
              f"trace={page['trace_id'][:8]}…  "
              f"hash={page['hash'][:12]}…")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
