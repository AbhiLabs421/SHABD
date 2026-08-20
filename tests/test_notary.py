"""Tests for shabd_notary — cross-entity Grimoire notarisation."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shabd import SHABD  # noqa: E402
from shabd_notary import (  # noqa: E402
    AgentNotary,
    NotaryRoot,
    verify_countersignature,
    verify_inclusion,
    verify_root,
)


def _new_bank(name: str) -> SHABD:
    return SHABD(name, secret=name.encode() + b"0" * 32,
                 require_auth=False)


class NotaryRootTests(unittest.TestCase):
    def test_root_signature_is_deterministic(self):
        app = _new_bank("bank-A")
        n = AgentNotary(app, entity="bank-A",
                        publishing_secret=b"k" * 32)
        r1 = n.publish_root()
        r2 = NotaryRoot.from_dict(r1.to_dict())
        self.assertTrue(verify_root(r2, b"k" * 32))

    def test_root_signature_rejects_wrong_secret(self):
        app = _new_bank("bank-A")
        n = AgentNotary(app, entity="bank-A",
                        publishing_secret=b"k" * 32)
        r = n.publish_root()
        self.assertFalse(verify_root(r, b"x" * 32))


class CountersignTests(unittest.TestCase):
    def test_b_can_witness_a_root(self):
        a, b = _new_bank("A"), _new_bank("B")
        na = AgentNotary(a, entity="A", publishing_secret=b"A" * 32)
        nb = AgentNotary(b, entity="B", publishing_secret=b"B" * 32)
        root = na.publish_root()
        nb.receive_peer_root(root)
        cs = nb.countersign(root)
        self.assertTrue(verify_countersignature(cs, b"B" * 32,
                                                 root.signature))

    def test_countersig_breaks_if_witnessed_sig_swapped(self):
        a, b = _new_bank("A"), _new_bank("B")
        na = AgentNotary(a, entity="A", publishing_secret=b"A" * 32)
        nb = AgentNotary(b, entity="B", publishing_secret=b"B" * 32)
        root = na.publish_root()
        cs = nb.countersign(root)
        cs.witnessed_signature = "0" * 64  # tamper
        self.assertFalse(verify_countersignature(cs, b"B" * 32,
                                                  root.signature))


class InclusionProofTests(unittest.TestCase):
    def _setup(self):
        a = _new_bank("A")

        @a.spell
        def approve(applicant: str, amount: float) -> dict:
            return {"ok": True, "ref": applicant}

        # Make 4 decisions
        for who, amt in [("alice", 1e5), ("bob", 2e5),
                          ("carol", 5e5), ("dave", 3e5)]:
            a.invoke("approve", {"applicant": who, "amount": amt})
        return a

    def test_proof_verifies(self):
        a = self._setup()
        n = AgentNotary(a, entity="A", publishing_secret=b"A" * 32)
        root = n.publish_root()
        proof = n.build_inclusion_proof(seq=1, against=root)
        r = verify_inclusion(proof, b"A" * 32)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["seq"], 1)

    def test_proof_rejects_wrong_entity_secret(self):
        a = self._setup()
        n = AgentNotary(a, entity="A", publishing_secret=b"A" * 32)
        root = n.publish_root()
        proof = n.build_inclusion_proof(seq=1, against=root)
        r = verify_inclusion(proof, b"X" * 32)   # wrong secret
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "bad_root_signature")

    def test_proof_breaks_when_chain_edited_after_notarisation(self):
        a = self._setup()
        n = AgentNotary(a, entity="A", publishing_secret=b"A" * 32)
        root = n.publish_root()
        # Discard the pre-tamper proof; we only need the root.
        _ = n.build_inclusion_proof(seq=1, against=root)
        # A retroactively edits page seq=1's hash field — this is the
        # ONLY way to actually tamper since that field is what the
        # chain commits to.
        a.grimoire._pages[1]["hash"] = "0" * 64
        # Build a fresh proof from the tampered chain. It must NOT
        # verify against the original notarised root.
        proof2 = n.build_inclusion_proof(seq=1, against=root)
        r = verify_inclusion(proof2, b"A" * 32)
        self.assertFalse(r["ok"])
        self.assertIn(r["reason"],
                      {"chain_break", "head_mismatch",
                       "first_page_hash_mismatch"})


class EndToEndTests(unittest.TestCase):
    def test_full_co_lending_flow(self):
        # 1. NBFC publishes a root after 3 loan approvals.
        nbfc = _new_bank("NBFC")

        @nbfc.spell
        def approve_loan(applicant: str, amount: float) -> dict:
            return {"ok": True, "ref": applicant}

        for who, amt in [("alice", 1e5), ("bob", 2e5),
                          ("carol", 5e5)]:
            nbfc.invoke("approve_loan",
                        {"applicant": who, "amount": amt})

        # 2. Bank receives NBFC's root and countersigns.
        bank = _new_bank("BANK")
        n_nbfc = AgentNotary(nbfc, entity="NBFC",
                              publishing_secret=b"N" * 32)
        n_bank = AgentNotary(bank, entity="BANK",
                              publishing_secret=b"B" * 32)
        root = n_nbfc.publish_root()
        n_bank.receive_peer_root(root)
        cs = n_bank.countersign(root)
        n_nbfc.store_countersignature(cs)

        # 3. Regulator later asks for proof of loan seq=1 (bob).
        proof = n_nbfc.build_inclusion_proof(seq=1, against=root)
        result = verify_inclusion(proof, b"N" * 32)
        cs_ok = verify_countersignature(cs, b"B" * 32, root.signature)
        self.assertTrue(result["ok"])
        self.assertTrue(cs_ok)


def main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for cls in (NotaryRootTests, CountersignTests, InclusionProofTests,
                EndToEndTests):
        suite.addTests(loader.loadTestsFromTestCase(cls))
    return 0 if unittest.TextTestRunner(verbosity=2,
                                         stream=sys.stdout).run(suite).wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
