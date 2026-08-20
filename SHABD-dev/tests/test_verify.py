"""Tests for shabd_verify — the independent, zero-dep audit-log verifier.

Proves the "don't trust us, verify" story: a standalone tool re-derives the
Grimoire hash chain and detects any tampering, WITHOUT the secret (hash chain)
and WITH it (signatures).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shabd import SHABD  # noqa: E402
from shabd_verify import main, verify_file, verify_pages  # noqa: E402

SECRET_HEX = "ab" * 16


def _make_log(n=4) -> tuple[str, list]:
    path = os.path.join(tempfile.mkdtemp(), "audit.jsonl")
    app = SHABD("v", secret=bytes.fromhex(SECRET_HEX), require_auth=False,
                grimoire_log_path=path)

    @app.spell()
    def add(a: int, b: int) -> int:
        return a + b

    for i in range(n):
        app.invoke("add", {"a": i, "b": i})
    pages = app.grimoire.pages(limit=10 ** 6)
    return path, pages


class VerifyTests(unittest.TestCase):
    def test_clean_chain_verifies_without_secret(self):
        _, pages = _make_log()
        r = verify_pages(pages)
        self.assertTrue(r["ok"])
        self.assertFalse(r["checked_signatures"])
        self.assertEqual(len(r["head"]), 64)

    def test_clean_chain_verifies_with_secret(self):
        _, pages = _make_log()
        r = verify_pages(pages, secret=bytes.fromhex(SECRET_HEX))
        self.assertTrue(r["ok"])
        self.assertTrue(r["checked_signatures"])

    def test_head_matches_grimoire(self):
        path, pages = _make_log()
        # the verifier's head must equal the last page's hash
        self.assertEqual(verify_pages(pages)["head"], pages[-1]["hash"])

    def test_tampered_field_detected_no_secret(self):
        _, pages = _make_log()
        pages[1]["subject"] = "attacker"       # alter a past page
        r = verify_pages(pages)
        self.assertFalse(r["ok"])
        self.assertEqual(r["break_seq"], 1)
        self.assertIn("tamper", r["reason"])

    def test_reordering_breaks_chain(self):
        _, pages = _make_log()
        pages[1], pages[2] = pages[2], pages[1]  # swap two pages
        self.assertFalse(verify_pages(pages)["ok"])

    def test_deleting_a_page_breaks_chain(self):
        _, pages = _make_log(5)
        del pages[2]
        self.assertFalse(verify_pages(pages)["ok"])

    def test_forged_page_without_secret_fails_signature_check(self):
        # An attacker who alters a field AND recomputes the hash (no secret)
        # still cannot produce a valid signature.
        from shabd_verify import _sha
        _, pages = _make_log()
        p = pages[1]
        p["subject"] = "attacker"
        p["hash"] = _sha({k: p[k] for k in (
            "prev", "seq", "ts", "trace_id", "spell", "subject",
            "args_hash", "result_hash", "ok")})
        # fix the following page's prev so the chain links again
        pages[2]["prev"] = p["hash"]
        pages[2]["hash"] = _sha({k: pages[2][k] for k in (
            "prev", "seq", "ts", "trace_id", "spell", "subject",
            "args_hash", "result_hash", "ok")})
        pages[3]["prev"] = pages[2]["hash"]
        pages[3]["hash"] = _sha({k: pages[3][k] for k in (
            "prev", "seq", "ts", "trace_id", "spell", "subject",
            "args_hash", "result_hash", "ok")})
        # hash chain now re-links (attacker rewrote it) — but signatures don't:
        self.assertTrue(verify_pages(pages)["ok"])   # chain-only fooled
        r = verify_pages(pages, secret=bytes.fromhex(SECRET_HEX))
        self.assertFalse(r["ok"])                    # signatures catch it
        self.assertIn("signature", r["reason"])

    def test_verify_file_roundtrip(self):
        path, _ = _make_log()
        self.assertTrue(verify_file(path)["ok"])

    def test_cli_ok_and_fail(self):
        path, pages = _make_log()
        self.assertEqual(main([path]), 0)
        self.assertEqual(main([path, "--secret", SECRET_HEX]), 0)
        # tamper the file on disk -> CLI returns nonzero
        with open(path) as fh:
            lines = fh.readlines()
        obj = json.loads(lines[1])
        obj["subject"] = "x"
        lines[1] = json.dumps(obj) + "\n"
        with open(path, "w") as fh:
            fh.writelines(lines)
        self.assertEqual(main([path]), 1)

    def test_cli_missing_file(self):
        self.assertEqual(main(["/no/such/file.jsonl"]), 2)


def main_() -> int:
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    ok = unittest.TextTestRunner(verbosity=2, stream=sys.stdout).run(suite)
    return 0 if ok.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main_())
