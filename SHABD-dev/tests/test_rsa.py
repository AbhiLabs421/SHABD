"""Tests for shabd_rsa — pure-Python RSA (PKCS#1 v1.5 / SHA-256) + JWK.

Security-critical primitive, so it gets its own focused suite. Uses 1024-bit
keys for speed; production Praman defaults to 2048.
"""
from __future__ import annotations

import base64
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import shabd_rsa as R  # noqa: E402


class RsaCoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.priv = R.generate_keypair(1024)
        cls.pub = R.public_of(cls.priv)

    def test_key_shape(self):
        for f in ("n", "e", "d", "p", "q"):
            self.assertIn(f, self.priv)
        self.assertEqual(self.priv["e"], 65537)
        self.assertEqual(self.priv["n"], self.priv["p"] * self.priv["q"])

    def test_sign_verify_roundtrip(self):
        msg = b"the quick brown fox"
        sig = R.sign_pkcs1v15_sha256(msg, self.priv)
        self.assertTrue(R.verify_pkcs1v15_sha256(msg, sig, self.pub))

    def test_tampered_message_fails(self):
        sig = R.sign_pkcs1v15_sha256(b"amount=100", self.priv)
        self.assertFalse(R.verify_pkcs1v15_sha256(b"amount=999", sig, self.pub))

    def test_tampered_signature_fails(self):
        sig = bytearray(R.sign_pkcs1v15_sha256(b"x", self.priv))
        sig[0] ^= 0x01
        self.assertFalse(R.verify_pkcs1v15_sha256(b"x", bytes(sig), self.pub))

    def test_wrong_key_fails(self):
        sig = R.sign_pkcs1v15_sha256(b"x", self.priv)
        other = R.public_of(R.generate_keypair(1024))
        self.assertFalse(R.verify_pkcs1v15_sha256(b"x", sig, other))

    def test_wrong_length_signature_fails(self):
        self.assertFalse(R.verify_pkcs1v15_sha256(b"x", b"short", self.pub))

    def test_signature_length_matches_modulus(self):
        k = (self.pub["n"].bit_length() + 7) // 8
        self.assertEqual(len(R.sign_pkcs1v15_sha256(b"x", self.priv)), k)

    def test_math_modinv_consistency(self):
        # e * d ≡ 1 (mod lambda) — signing then verifying the raw int works
        m = 12345
        n, e, d = self.priv["n"], self.priv["e"], self.priv["d"]
        self.assertEqual(pow(pow(m, d, n), e, n), m)


class JwkTests(unittest.TestCase):
    def test_jwk_public_fields(self):
        pub = R.public_of(R.generate_keypair(1024))
        jwk = R.jwk_public(pub, "kid-1")
        self.assertEqual(jwk["kty"], "RSA")
        self.assertEqual(jwk["alg"], "RS256")
        self.assertEqual(jwk["kid"], "kid-1")
        # n and e decode back to the original integers
        def dec(s):
            return int.from_bytes(
                base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)), "big")
        self.assertEqual(dec(jwk["n"]), pub["n"])
        self.assertEqual(dec(jwk["e"]), pub["e"])

    def test_json_serialisation_roundtrip(self):
        import json
        priv = R.generate_keypair(1024)
        rt = R.key_from_jsonable(json.loads(json.dumps(R.key_to_jsonable(priv))))
        self.assertEqual(rt["n"], priv["n"])
        self.assertEqual(rt["d"], priv["d"])
        sig = R.sign_pkcs1v15_sha256(b"m", rt)
        self.assertTrue(R.verify_pkcs1v15_sha256(b"m", sig, R.public_of(rt)))


class PrimalityTests(unittest.TestCase):
    def test_known_primes_and_composites(self):
        for p in (2, 3, 5, 7, 97, 7919, 104729):
            self.assertTrue(R._is_probable_prime(p))
        for c in (1, 4, 100, 7917, 104728):
            self.assertFalse(R._is_probable_prime(c))

    def test_generated_prime_is_prime(self):
        p = R._gen_prime(256)
        self.assertTrue(R._is_probable_prime(p))
        self.assertGreaterEqual(p.bit_length(), 256)


def main() -> int:
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    ok = unittest.TextTestRunner(verbosity=2, stream=sys.stdout).run(suite)
    return 0 if ok.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
