"""
shabd_rsa.py — pure-standard-library RSA for SHABD (zero dependencies).

Why this exists
---------------
Standard OIDC clients verify tokens with **RS256** (RSA-SHA256) using a public
key published at a JWKS endpoint. Python's standard library has HMAC/SHA but no
RSA. Rather than pull in `cryptography` (a C extension) and break SHABD's
"single tree, zero deps, air-gap" promise, we implement exactly the RSA we need
in pure Python:

  * key generation  — Miller-Rabin primes, e = 65537
  * signing         — RSASSA-PKCS1-v1_5 with SHA-256 (RFC 8017)
  * verification    — the same, constant-time compare
  * JWK export       — public key as a JWKS entry so any standard client verifies

Python ints are arbitrary-precision, so RSA (which is modular exponentiation) is
a few lines: sign = pow(m, d, n), verify = pow(s, e, n).

Security notes (honest)
-----------------------
* This is a careful but *custom* PKCS#1 v1.5 implementation. It must get an
  independent review before it signs tokens for real money. RSASSA-PKCS1-v1_5
  is verification-safe when implemented correctly (we reconstruct the full
  encoded message and compare, rather than parsing — the Bleichenbacher-'06
  countermeasure).
* Randomness is from `secrets` (CSPRNG). Key generation uses 40 Miller-Rabin
  rounds (FIPS-186 comfortable margin).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

__all__ = [
    "generate_keypair", "public_of", "sign_pkcs1v15_sha256",
    "verify_pkcs1v15_sha256", "jwk_public", "key_to_jsonable",
    "key_from_jsonable",
]

# ASN.1 DigestInfo prefix for SHA-256 (RFC 8017 §9.2, Note 1).
_SHA256_DIGESTINFO = bytes.fromhex(
    "3031300d060960864801650304020105000420")

_SMALL_PRIMES = [
    2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43, 47, 53, 59, 61,
    67, 71, 73, 79, 83, 89, 97, 101, 103, 107, 109, 113, 127, 131, 137,
    139, 149, 151, 157, 163, 167, 173, 179, 181, 191, 193, 197, 199,
]


# --------------------------------------------------------------------------
# integer <-> octet string (RFC 8017 I2OSP / OS2IP)
# --------------------------------------------------------------------------
def _i2osp(x: int, xlen: int) -> bytes:
    if x < 0 or x >= (1 << (8 * xlen)):
        raise ValueError("integer too large")
    return x.to_bytes(xlen, "big")


def _os2ip(b: bytes) -> int:
    return int.from_bytes(b, "big")


# --------------------------------------------------------------------------
# number theory
# --------------------------------------------------------------------------
def _egcd(a: int, b: int) -> tuple[int, int, int]:
    old_r, r = a, b
    old_s, s = 1, 0
    old_t, t = 0, 1
    while r:
        q = old_r // r
        old_r, r = r, old_r - q * r
        old_s, s = s, old_s - q * s
        old_t, t = t, old_t - q * t
    return old_r, old_s, old_t


def _modinv(a: int, m: int) -> int:
    g, x, _ = _egcd(a % m, m)
    if g != 1:
        raise ValueError("modular inverse does not exist")
    return x % m


def _is_probable_prime(n: int, rounds: int = 40) -> bool:
    if n < 2:
        return False
    for p in _SMALL_PRIMES:
        if n % p == 0:
            return n == p
    d = n - 1
    r = 0
    while d % 2 == 0:
        d //= 2
        r += 1
    for _ in range(rounds):
        a = 2 + secrets.randbelow(n - 3)
        x = pow(a, d, n)
        if x == 1 or x == n - 1:
            continue
        for _ in range(r - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def _gen_prime(bits: int) -> int:
    while True:
        cand = secrets.randbits(bits) | (1 << (bits - 1)) | 1
        if _is_probable_prime(cand):
            return cand


# --------------------------------------------------------------------------
# key generation
# --------------------------------------------------------------------------
def generate_keypair(bits: int = 2048, e: int = 65537) -> dict:
    """Return a private key dict {n, e, d, p, q, bits}."""
    if bits < 512:
        raise ValueError("bits must be >= 512")
    half = bits // 2
    while True:
        p = _gen_prime(half)
        q = _gen_prime(bits - half)
        if p == q:
            continue
        phi = (p - 1) * (q - 1)
        if phi % e == 0:
            continue
        n = p * q
        d = _modinv(e, phi)
        return {"n": n, "e": e, "d": d, "p": p, "q": q, "bits": n.bit_length()}


def public_of(priv: dict) -> dict:
    return {"n": priv["n"], "e": priv["e"], "bits": priv["n"].bit_length()}


# --------------------------------------------------------------------------
# RSASSA-PKCS1-v1_5 with SHA-256
# --------------------------------------------------------------------------
def _emsa_pkcs1v15(msg: bytes, emlen: int) -> bytes:
    h = hashlib.sha256(msg).digest()
    tt = _SHA256_DIGESTINFO + h
    if emlen < len(tt) + 11:
        raise ValueError("intended encoded message length too short")
    ps = b"\xff" * (emlen - len(tt) - 3)
    return b"\x00\x01" + ps + b"\x00" + tt


def sign_pkcs1v15_sha256(msg: bytes, priv: dict) -> bytes:
    n, d = priv["n"], priv["d"]
    k = (n.bit_length() + 7) // 8
    em = _emsa_pkcs1v15(msg, k)
    s = pow(_os2ip(em), d, n)
    return _i2osp(s, k)


def verify_pkcs1v15_sha256(msg: bytes, sig: bytes, pub: dict) -> bool:
    n, e = pub["n"], pub["e"]
    k = (n.bit_length() + 7) // 8
    if len(sig) != k:
        return False
    m = pow(_os2ip(sig), e, n)
    try:
        em = _i2osp(m, k)
        expected = _emsa_pkcs1v15(msg, k)
    except ValueError:
        return False
    # Reconstruct-and-compare (constant time) — the safe verification path.
    return hmac.compare_digest(em, expected)


# --------------------------------------------------------------------------
# JWK / JWKS export + (de)serialisation
# --------------------------------------------------------------------------
def _int_to_b64u(x: int) -> str:
    length = (x.bit_length() + 7) // 8 or 1
    return base64.urlsafe_b64encode(x.to_bytes(length, "big")).rstrip(
        b"=").decode("ascii")


def jwk_public(pub: dict, kid: str) -> dict:
    """A public key as a JWKS entry any standard OIDC client can verify with."""
    return {
        "kty": "RSA", "use": "sig", "alg": "RS256", "kid": kid,
        "n": _int_to_b64u(pub["n"]), "e": _int_to_b64u(pub["e"]),
    }


def key_to_jsonable(priv: dict) -> dict:
    """Big ints -> decimal strings so the key survives JSON round-trips."""
    return {k: (str(v) if isinstance(v, int) else v) for k, v in priv.items()}


def key_from_jsonable(d: dict) -> dict:
    out = {}
    for k, v in d.items():
        if k in ("n", "e", "d", "p", "q") and isinstance(v, str):
            out[k] = int(v)
        else:
            out[k] = v
    return out
