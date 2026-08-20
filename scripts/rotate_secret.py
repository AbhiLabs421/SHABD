#!/usr/bin/env python3
"""
Helper for SHABD's zero-downtime secret rotation.

Workflow:

  1. Run this script to generate a fresh 32-byte secret.
  2. Roll your fleet with:
         SHABD_SECRET=<new>
         SHABD_SECRET_OLD=<previous>
     SHABD will sign new tokens with the new secret while still
     accepting tokens signed with the old one.
  3. After all tokens issued before the rotation have expired
     (default TTL = 1 hour), drop SHABD_SECRET_OLD on the next deploy.

The script does not edit any files — it just prints the secret so you
can paste it into your secret store / environment.
"""
from __future__ import annotations

import secrets


def main() -> int:
    new = secrets.token_hex(32)
    print(f"NEW SHABD_SECRET (set in your secret store):\n  {new}")
    print()
    print("Then deploy with both, e.g.:")
    print("  SHABD_SECRET=<new>")
    print("  SHABD_SECRET_OLD=<previous>")
    print()
    print("After your token TTL window has elapsed, drop SHABD_SECRET_OLD.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
