"""
shabd_praman.py — SHABD's built-in identity provider ("Praman").

Praman (प्रमाण = proof / authority / credential) is a pure-standard-library,
zero-dependency OAuth2 / OIDC-style identity server. It is the *built-in*
option in the pluggable identity pattern:

    identity.provider: builtin   ->  Praman  (this file, nothing to install)
    identity.provider: keycloak  ->  the customer's real Keycloak (external URL)

Why it exists
-------------
An enterprise normally installs Keycloak (a Java stack + container) just to get
login, tokens and roles. In a restricted / air-gapped bank network that is
painful. Praman gives you the same essentials — users, roles, client apps,
OAuth2 grants, token issue/verify/introspect/revoke, discovery, TOTP MFA — as a
single Python module, and writes every auth event into the SHABD Grimoire so
identity actions are *tamper-evident* (which stock Keycloak does not give you).

Honest scope (Phase 1)
----------------------
* Token signing is **HS256** (HMAC). SHABD verifies its own tokens; third
  parties verify via the `/introspect` endpoint. RS256 + JWKS (so external
  services can verify offline with a public key) is a planned Phase-2 addition
  implemented with pure-Python RSA — see docs/PRODUCTION-READINESS.md.
* This is POC-grade and must get an independent security review before it is
  the auth of record for real money. HS256 + external-Keycloak remain the
  fallbacks for customers who require a certified IdP.

Everything here is standard-library only: hashlib, hmac, base64, json, time,
secrets, struct, threading, http.server.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import http.server
import json
import secrets
import socketserver
import struct
import threading
import time
import typing as t

import shabd_rsa

# Reuse the vetted scrypt password hashing from the user store — do not
# reinvent it here.
from shabd_users import _make_hash, _verify_hash

__all__ = [
    "PramanError",
    "Praman",
    "PramanRealm",
    "PramanServer",
    "IdentityProvider",
    "BuiltinIdentityProvider",
    "ExternalKeycloakProvider",
    "grimoire_audit_bridge",
    "identity_from_config",
    "jwt_encode_hs256",
    "jwt_decode_hs256",
    "totp_now",
    "totp_verify",
    "totp_provisioning_uri",
]


class PramanError(Exception):
    """Auth failure carrying an HTTP-shaped status and an OAuth2 error code."""

    def __init__(self, status: int, error: str, description: str = ""):
        super().__init__(description or error)
        self.status = status
        self.error = error              # OAuth2 error code, e.g. invalid_grant
        self.description = description


# ===========================================================================
# base64url helpers (no padding, per JWT/JOSE)
# ===========================================================================

def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64u_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


# ===========================================================================
# JWT — standard 3-part token, HS256
# ===========================================================================

def jwt_encode_hs256(claims: dict, secret: bytes, *, kid: str = "praman-hs-1") -> str:
    header = {"alg": "HS256", "typ": "JWT", "kid": kid}
    h = _b64u(json.dumps(header, separators=(",", ":"), sort_keys=True).encode())
    p = _b64u(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode())
    signing_input = f"{h}.{p}".encode("ascii")
    sig = hmac.new(secret, signing_input, hashlib.sha256).digest()
    return f"{h}.{p}.{_b64u(sig)}"


def _check_time_claims(claims: dict, leeway: int = 30) -> None:
    now = int(time.time())
    if int(claims.get("exp", 0)) < now - leeway:
        raise PramanError(401, "invalid_token", "token expired")
    if int(claims.get("nbf", 0)) > now + leeway:
        raise PramanError(401, "invalid_token", "token not yet valid")


def jwt_header(token: str) -> dict:
    """Read the JOSE header WITHOUT verifying — used only to route by alg."""
    try:
        return json.loads(_b64u_decode(token.split(".")[0]))
    except Exception:
        raise PramanError(401, "invalid_token", "bad header")


def jwt_decode_hs256(token: str, secret: bytes, *,
                     verify_exp: bool = True,
                     leeway: int = 30) -> dict:
    """Verify signature + expiry, return claims. Raises PramanError."""
    parts = token.split(".")
    if len(parts) != 3:
        raise PramanError(401, "invalid_token", "malformed JWT")
    h_b64, p_b64, sig_b64 = parts
    signing_input = f"{h_b64}.{p_b64}".encode("ascii")
    try:
        given = _b64u_decode(sig_b64)
    except Exception:
        raise PramanError(401, "invalid_token", "bad signature encoding")
    expected = hmac.new(secret, signing_input, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, given):
        raise PramanError(401, "invalid_token", "signature mismatch")
    try:
        claims = json.loads(_b64u_decode(p_b64))
    except Exception:
        raise PramanError(401, "invalid_token", "bad payload")
    if verify_exp:
        _check_time_claims(claims, leeway)
    return claims


def jwt_encode_rs256(claims: dict, priv: dict, *, kid: str) -> str:
    header = {"alg": "RS256", "typ": "JWT", "kid": kid}
    h = _b64u(json.dumps(header, separators=(",", ":"), sort_keys=True).encode())
    p = _b64u(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode())
    signing_input = f"{h}.{p}".encode("ascii")
    sig = shabd_rsa.sign_pkcs1v15_sha256(signing_input, priv)
    return f"{h}.{p}.{_b64u(sig)}"


def jwt_decode_rs256(token: str, pub: dict, *,
                     verify_exp: bool = True, leeway: int = 30) -> dict:
    parts = token.split(".")
    if len(parts) != 3:
        raise PramanError(401, "invalid_token", "malformed JWT")
    h_b64, p_b64, sig_b64 = parts
    signing_input = f"{h_b64}.{p_b64}".encode("ascii")
    try:
        sig = _b64u_decode(sig_b64)
    except Exception:
        raise PramanError(401, "invalid_token", "bad signature encoding")
    if not shabd_rsa.verify_pkcs1v15_sha256(signing_input, sig, pub):
        raise PramanError(401, "invalid_token", "signature mismatch")
    try:
        claims = json.loads(_b64u_decode(p_b64))
    except Exception:
        raise PramanError(401, "invalid_token", "bad payload")
    if verify_exp:
        _check_time_claims(claims, leeway)
    return claims


# ===========================================================================
# TOTP — RFC 6238 (MFA), pure stdlib
# ===========================================================================

def _base32_secret(nbytes: int = 20) -> str:
    return base64.b32encode(secrets.token_bytes(nbytes)).decode("ascii").rstrip("=")


def totp_now(secret_b32: str, *, t0: int | None = None,
             step: int = 30, digits: int = 6) -> str:
    if t0 is None:
        t0 = int(time.time())
    counter = t0 // step
    pad = "=" * (-len(secret_b32) % 8)
    key = base64.b32decode(secret_b32.upper() + pad)
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(code % (10 ** digits)).zfill(digits)


def totp_verify(secret_b32: str, code: str, *,
                window: int = 1, step: int = 30, digits: int = 6) -> bool:
    """Accept codes within +/- `window` steps to tolerate clock skew."""
    if not code or not code.isdigit():
        return False
    now = int(time.time())
    for w in range(-window, window + 1):
        if hmac.compare_digest(
                totp_now(secret_b32, t0=now + w * step, step=step, digits=digits),
                code.zfill(digits)):
            return True
    return False


def totp_provisioning_uri(secret_b32: str, *, account: str, issuer: str) -> str:
    from urllib.parse import quote
    label = quote(f"{issuer}:{account}")
    return (f"otpauth://totp/{label}?secret={secret_b32}"
            f"&issuer={quote(issuer)}&algorithm=SHA1&digits=6&period=30")


# ===========================================================================
# Realm store — users, roles, clients, refresh tokens, revocations
# ===========================================================================

class PramanRealm:
    """The identity 'realm': the ground truth of who/what can authenticate.

    Persisted to a JSON sidecar so it survives restarts. Passwords are stored
    only as scrypt hashes; client secrets only as sha256 hashes.
    """

    def __init__(self, name: str = "shabd", *, path: str | None = None,
                 scrypt_n: int = 2 ** 14, scrypt_r: int = 8, scrypt_p: int = 1):
        self.name = name
        self.path = path
        self._n, self._r, self._p = int(scrypt_n), int(scrypt_r), int(scrypt_p)
        self._lock = threading.RLock()
        self.users: dict[str, dict] = {}     # username -> record
        self.clients: dict[str, dict] = {}    # client_id -> record
        self.refresh: dict[str, dict] = {}    # refresh_token -> record
        self.revoked_jti: set[str] = set()
        self.rsa_key: dict = {}               # RS256 signing key (jsonable)
        self._load()

    # ---- persistence ---------------------------------------------------
    def _load(self) -> None:
        if not self.path:
            return
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return
        except Exception:
            return
        self.users = data.get("users", {})
        self.clients = data.get("clients", {})
        self.refresh = data.get("refresh", {})
        self.revoked_jti = set(data.get("revoked_jti", []))
        self.rsa_key = data.get("rsa_key", {})

    def _save(self) -> None:
        if not self.path:
            return
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({
                "users": self.users, "clients": self.clients,
                "refresh": self.refresh,
                "revoked_jti": sorted(self.revoked_jti),
                "rsa_key": self.rsa_key,
            }, fh)
        import os
        os.replace(tmp, self.path)

    # ---- users ---------------------------------------------------------
    def add_user(self, username: str, password: str, *,
                 roles: t.Iterable[str] = (), email: str = "",
                 full_name: str = "") -> dict:
        with self._lock:
            if username in self.users:
                raise PramanError(409, "conflict", f"user exists: {username}")
            rec = {
                "username": username,
                "pwd_hash": _make_hash(password, n=self._n, r=self._r, p=self._p),
                "roles": list(roles),
                "email": email, "full_name": full_name,
                "totp_secret": "", "mfa_enabled": False,
                "disabled": False,
                "failed": [],            # timestamps of recent failed logins
                "created_at": time.time(),
            }
            self.users[username] = rec
            self._save()
            return rec

    def set_password(self, username: str, password: str) -> None:
        with self._lock:
            u = self._require_user(username)
            u["pwd_hash"] = _make_hash(password, n=self._n, r=self._r, p=self._p)
            self._save()

    def _require_user(self, username: str) -> dict:
        u = self.users.get(username)
        if not u:
            raise PramanError(404, "not_found", f"no such user: {username}")
        return u

    # ---- clients (service / app credentials) ---------------------------
    def add_client(self, client_id: str, *, client_secret: str = "",
                   scopes: t.Iterable[str] = (),
                   confidential: bool = True) -> dict:
        with self._lock:
            rec = {
                "client_id": client_id,
                "secret_hash": (hashlib.sha256(client_secret.encode()).hexdigest()
                                if client_secret else ""),
                "scopes": list(scopes),
                "confidential": bool(confidential),
                "created_at": time.time(),
            }
            self.clients[client_id] = rec
            self._save()
            return rec

    def verify_client(self, client_id: str, client_secret: str) -> dict:
        c = self.clients.get(client_id)
        if not c:
            raise PramanError(401, "invalid_client", "unknown client")
        if c["secret_hash"]:
            got = hashlib.sha256((client_secret or "").encode()).hexdigest()
            if not hmac.compare_digest(got, c["secret_hash"]):
                raise PramanError(401, "invalid_client", "bad client secret")
        return c


# ===========================================================================
# Praman — the identity provider (grants, verification, OIDC surface)
# ===========================================================================

class Praman:
    """Built-in OAuth2 / OIDC-style identity provider.

    `audit` (optional) is called as audit(event:str, detail:dict) on every
    auth action; wire it to the Grimoire so identity is tamper-evident.
    """

    def __init__(self, *, issuer: str, secret: bytes,
                 realm: PramanRealm | None = None,
                 access_ttl: int = 900, refresh_ttl: int = 28800,
                 min_password_len: int = 12,
                 lockout_max: int = 5, lockout_window: int = 300,
                 token_alg: str = "HS256", rsa_bits: int = 2048,
                 audit: t.Callable[[str, dict], None] | None = None):
        if not secret or len(secret) < 16:
            raise ValueError("Praman secret must be >= 16 bytes")
        if token_alg not in ("HS256", "RS256"):
            raise ValueError("token_alg must be HS256 or RS256")
        self.issuer = issuer.rstrip("/")
        self.secret = secret
        self.realm = realm or PramanRealm()
        self.access_ttl = int(access_ttl)
        self.refresh_ttl = int(refresh_ttl)
        self.min_password_len = int(min_password_len)
        self.lockout_max = int(lockout_max)
        self.lockout_window = int(lockout_window)
        self.token_alg = token_alg
        self._audit = audit
        self._rsa_priv: dict | None = None
        self._rsa_pub: dict | None = None
        self._kid = "praman-hs-1"
        if token_alg == "RS256":
            self._ensure_rsa(rsa_bits)

    # ---- signing key management (RS256) -------------------------------
    def _ensure_rsa(self, bits: int) -> None:
        """Load the realm's RSA signing key, or generate + persist one."""
        with self.realm._lock:
            if self.realm.rsa_key:
                self._rsa_priv = shabd_rsa.key_from_jsonable(self.realm.rsa_key)
                self._kid = self.realm.rsa_key.get("kid", "praman-rs-1")
            else:
                priv = shabd_rsa.generate_keypair(bits)
                kid = "praman-rs-" + hashlib.sha256(
                    str(priv["n"]).encode()).hexdigest()[:12]
                stored = shabd_rsa.key_to_jsonable(priv)
                stored["kid"] = kid
                self.realm.rsa_key = stored
                self.realm._save()
                self._rsa_priv = priv
                self._kid = kid
            self._rsa_pub = shabd_rsa.public_of(self._rsa_priv)

    def _encode_jwt(self, claims: dict) -> str:
        if self.token_alg == "RS256":
            return jwt_encode_rs256(claims, self._rsa_priv, kid=self._kid)
        return jwt_encode_hs256(claims, self.secret, kid=self._kid)

    def _decode_jwt(self, token: str, *, verify_exp: bool = True) -> dict:
        # Enforce the configured algorithm — refuse alg substitution
        # (e.g. an HS256 token forged against an RS256 server, or "none").
        alg = jwt_header(token).get("alg")
        if alg != self.token_alg:
            raise PramanError(401, "invalid_token", f"unexpected alg: {alg}")
        if self.token_alg == "RS256":
            return jwt_decode_rs256(token, self._rsa_pub, verify_exp=verify_exp)
        return jwt_decode_hs256(token, self.secret, verify_exp=verify_exp)

    def _emit(self, event: str, detail: dict) -> None:
        if self._audit:
            try:
                self._audit(event, detail)
            except Exception:
                pass

    # ---- account management -------------------------------------------
    def validate_password(self, password: str) -> None:
        if len(password or "") < self.min_password_len:
            raise PramanError(
                400, "weak_password",
                f"password must be >= {self.min_password_len} chars")

    def create_user(self, username: str, password: str, **kw) -> dict:
        self.validate_password(password)
        rec = self.realm.add_user(username, password, **kw)
        self._emit("praman.user_created",
                   {"username": username, "roles": rec["roles"]})
        return {"username": username, "roles": rec["roles"]}

    def enroll_totp(self, username: str) -> dict:
        """Generate a TOTP secret for a user and return a provisioning URI.
        MFA becomes required once `confirm_totp` succeeds."""
        with self.realm._lock:
            u = self.realm._require_user(username)
            sec = _base32_secret()
            u["totp_secret"] = sec
            u["mfa_enabled"] = False   # not enforced until confirmed
            self.realm._save()
        self._emit("praman.totp_enrolled", {"username": username})
        return {"secret": sec,
                "otpauth_uri": totp_provisioning_uri(
                    sec, account=username, issuer=self.realm.name)}

    def confirm_totp(self, username: str, code: str) -> bool:
        with self.realm._lock:
            u = self.realm._require_user(username)
            if u.get("totp_secret") and totp_verify(u["totp_secret"], code):
                u["mfa_enabled"] = True
                self.realm._save()
                self._emit("praman.mfa_enabled", {"username": username})
                return True
        raise PramanError(400, "invalid_grant", "TOTP code incorrect")

    # ---- lockout -------------------------------------------------------
    def _check_lockout(self, u: dict) -> None:
        now = time.time()
        u["failed"] = [ts for ts in u.get("failed", [])
                       if ts > now - self.lockout_window]
        if len(u["failed"]) >= self.lockout_max:
            raise PramanError(429, "locked_out",
                              "too many failed attempts; try later")

    def _record_failure(self, u: dict) -> None:
        u.setdefault("failed", []).append(time.time())
        self.realm._save()

    # ---- authentication (Resource Owner Password) ---------------------
    def authenticate(self, username: str, password: str, *,
                     otp: str | None = None) -> dict:
        u = self.realm.users.get(username)
        if not u or u.get("disabled"):
            self._emit("praman.login_failed",
                       {"username": username, "reason": "unknown_or_disabled"})
            raise PramanError(401, "invalid_grant", "bad credentials")
        with self.realm._lock:
            self._check_lockout(u)
            if not _verify_hash(password, u["pwd_hash"]):
                self._record_failure(u)
                self._emit("praman.login_failed",
                           {"username": username, "reason": "bad_password"})
                raise PramanError(401, "invalid_grant", "bad credentials")
            if u.get("mfa_enabled"):
                if not otp or not totp_verify(u.get("totp_secret", ""), otp):
                    self._emit("praman.login_failed",
                               {"username": username, "reason": "mfa"})
                    raise PramanError(401, "mfa_required", "TOTP code required")
            u["failed"] = []
            u["last_login_at"] = time.time()
            self.realm._save()
        self._emit("praman.login_ok", {"username": username})
        return u

    # ---- token issuance ------------------------------------------------
    def _new_access(self, *, sub: str, roles: list[str], scope: str,
                    client_id: str, aud: str | None = None) -> tuple[str, dict]:
        now = int(time.time())
        jti = secrets.token_hex(12)
        claims = {
            "iss": self.issuer, "sub": sub,
            "aud": aud or client_id or "shabd",
            "iat": now, "nbf": now, "exp": now + self.access_ttl,
            "jti": jti, "typ": "access",
            "scope": scope, "roles": roles, "client_id": client_id,
        }
        return self._encode_jwt(claims), claims

    def _new_id_token(self, u: dict, *, aud: str) -> str:
        now = int(time.time())
        claims = {
            "iss": self.issuer, "sub": u["username"], "aud": aud,
            "iat": now, "exp": now + self.access_ttl, "auth_time": now,
            "name": u.get("full_name") or u["username"],
            "email": u.get("email", ""), "roles": u.get("roles", []),
        }
        return self._encode_jwt(claims)

    def _issue_refresh(self, *, sub: str, scope: str, client_id: str) -> str:
        tok = secrets.token_urlsafe(32)
        self.realm.refresh[tok] = {
            "sub": sub, "scope": scope, "client_id": client_id,
            "exp": int(time.time()) + self.refresh_ttl,
        }
        self.realm._save()
        return tok

    def _token_response(self, u: dict, *, scope: str, client_id: str,
                        with_id_token: bool = True) -> dict:
        access, claims = self._new_access(
            sub=u["username"], roles=u.get("roles", []),
            scope=scope, client_id=client_id)
        refresh = self._issue_refresh(
            sub=u["username"], scope=scope, client_id=client_id)
        resp = {
            "access_token": access, "token_type": "Bearer",
            "expires_in": self.access_ttl, "refresh_token": refresh,
            "scope": scope,
        }
        if with_id_token:
            resp["id_token"] = self._new_id_token(u, aud=client_id or "shabd")
        self._emit("praman.token_issued",
                   {"sub": u["username"], "jti": claims["jti"],
                    "grant": "password", "client_id": client_id})
        return resp

    # ---- OAuth2 /token grant dispatcher -------------------------------
    def token(self, params: dict) -> dict:
        grant = params.get("grant_type", "")
        if grant == "password":
            username = params.get("username", "")
            u = self.authenticate(username, params.get("password", ""),
                                  otp=params.get("otp"))
            scope = params.get("scope") or " ".join(u.get("roles", []))
            client_id = params.get("client_id", "shabd")
            return self._token_response(u, scope=scope, client_id=client_id)
        if grant == "refresh_token":
            return self._refresh(params.get("refresh_token", ""))
        if grant == "client_credentials":
            return self._client_credentials(params)
        raise PramanError(400, "unsupported_grant_type", grant or "missing")

    def _refresh(self, refresh_token: str) -> dict:
        rec = self.realm.refresh.get(refresh_token)
        if not rec:
            raise PramanError(400, "invalid_grant", "unknown refresh token")
        if int(rec["exp"]) < int(time.time()):
            self.realm.refresh.pop(refresh_token, None)
            self.realm._save()
            raise PramanError(400, "invalid_grant", "refresh token expired")
        u = self.realm.users.get(rec["sub"])
        if not u or u.get("disabled"):
            raise PramanError(400, "invalid_grant", "user unavailable")
        # rotate: revoke the old refresh, issue a fresh pair
        self.realm.refresh.pop(refresh_token, None)
        resp = self._token_response(u, scope=rec["scope"],
                                    client_id=rec["client_id"])
        self._emit("praman.token_refreshed", {"sub": rec["sub"]})
        return resp

    def _client_credentials(self, params: dict) -> dict:
        client_id = params.get("client_id", "")
        c = self.realm.verify_client(client_id, params.get("client_secret", ""))
        scope = params.get("scope") or " ".join(c.get("scopes", []))
        access, claims = self._new_access(
            sub=client_id, roles=[], scope=scope, client_id=client_id,
            aud=client_id)
        self._emit("praman.token_issued",
                   {"sub": client_id, "jti": claims["jti"],
                    "grant": "client_credentials", "client_id": client_id})
        return {"access_token": access, "token_type": "Bearer",
                "expires_in": self.access_ttl, "scope": scope}

    # ---- verification / introspection / revocation --------------------
    def verify_access_token(self, token: str) -> dict:
        claims = self._decode_jwt(token)
        if claims.get("iss") != self.issuer:
            raise PramanError(401, "invalid_token", "issuer mismatch")
        if claims.get("jti") in self.realm.revoked_jti:
            raise PramanError(401, "invalid_token", "token revoked")
        return claims

    def userinfo(self, token: str) -> dict:
        claims = self.verify_access_token(token)
        u = self.realm.users.get(claims.get("sub", ""))
        if not u:
            # e.g. client-credentials subject — return minimal
            return {"sub": claims.get("sub"), "roles": claims.get("roles", [])}
        return {"sub": u["username"], "name": u.get("full_name") or u["username"],
                "email": u.get("email", ""), "roles": u.get("roles", [])}

    def introspect(self, token: str) -> dict:
        try:
            claims = self.verify_access_token(token)
        except PramanError:
            return {"active": False}
        return {"active": True, "sub": claims.get("sub"),
                "scope": claims.get("scope", ""), "exp": claims.get("exp"),
                "client_id": claims.get("client_id"),
                "token_type": "Bearer", "iss": claims.get("iss")}

    def revoke(self, token: str) -> None:
        # refresh token?
        if token in self.realm.refresh:
            self.realm.refresh.pop(token, None)
            self.realm._save()
            self._emit("praman.revoked", {"kind": "refresh"})
            return
        # access token -> blacklist its jti until expiry
        try:
            claims = self._decode_jwt(token, verify_exp=False)
        except PramanError:
            return
        jti = claims.get("jti")
        if jti:
            self.realm.revoked_jti.add(jti)
            self.realm._save()
            self._emit("praman.revoked", {"kind": "access", "jti": jti})

    # ---- OIDC discovery -----------------------------------------------
    def discovery_document(self) -> dict:
        base = self.issuer
        return {
            "issuer": base,
            "token_endpoint": f"{base}/praman/token",
            "userinfo_endpoint": f"{base}/praman/userinfo",
            "introspection_endpoint": f"{base}/praman/introspect",
            "revocation_endpoint": f"{base}/praman/revoke",
            "jwks_uri": f"{base}/praman/jwks",
            "grant_types_supported": [
                "password", "refresh_token", "client_credentials"],
            "response_types_supported": ["token"],
            "token_endpoint_auth_methods_supported": [
                "client_secret_post", "none"],
            "id_token_signing_alg_values_supported": [self.token_alg],
            "scopes_supported": ["openid", "profile", "email"],
        }

    def jwks(self) -> dict:
        # RS256: publish the public key so any client verifies offline.
        # HS256 is symmetric — never publish the key; third parties verify
        # via /introspect instead.
        if self.token_alg == "RS256" and self._rsa_pub:
            return {"keys": [shabd_rsa.jwk_public(self._rsa_pub, self._kid)]}
        return {"keys": []}


# ===========================================================================
# Config-selectable identity provider abstraction
# ===========================================================================

class IdentityProvider:
    """Interface the SHABD UI / API depends on — never a concrete impl."""

    def password_login(self, username: str, password: str,
                        otp: str | None = None) -> dict:
        raise NotImplementedError

    def verify(self, token: str) -> dict:
        raise NotImplementedError


class BuiltinIdentityProvider(IdentityProvider):
    """Wraps Praman as the config `identity.provider: builtin`."""

    def __init__(self, praman: Praman):
        self.praman = praman

    def password_login(self, username, password, otp=None):
        return self.praman.token({
            "grant_type": "password", "username": username,
            "password": password, "otp": otp})

    def verify(self, token):
        return self.praman.verify_access_token(token)


class ExternalKeycloakProvider(IdentityProvider):
    """Config `identity.provider: keycloak` — password-grant against the
    customer's real Keycloak. Pure-stdlib urllib; verification is delegated to
    Keycloak's introspection/JWKS (kept thin here — the existing KeycloakConfig
    path in shabd_ui already implements the browser flow)."""

    def __init__(self, *, server_url: str, realm: str, client_id: str,
                 client_secret: str = ""):
        self.base = server_url.rstrip("/")
        self.realm = realm
        self.client_id = client_id
        self.client_secret = client_secret

    def _token_url(self) -> str:
        return (f"{self.base}/realms/{self.realm}"
                "/protocol/openid-connect/token")

    def password_login(self, username, password, otp=None):
        import urllib.error
        import urllib.parse
        import urllib.request
        data = {"grant_type": "password", "client_id": self.client_id,
                "username": username, "password": password}
        if self.client_secret:
            data["client_secret"] = self.client_secret
        if otp:
            data["totp"] = otp
        body = urllib.parse.urlencode(data).encode()
        req = urllib.request.Request(
            self._token_url(), data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            raise PramanError(e.code, "invalid_grant",
                              "keycloak rejected credentials") from None

    def verify(self, token):
        # Minimal: decode without signature check would be unsafe; a real
        # deployment verifies via Keycloak JWKS. Left for the SSO phase.
        raise PramanError(501, "not_implemented",
                          "external Keycloak verify handled by SSO flow")


_PRAMAN_EVT_PREFIX = "__praman_event:"


def grimoire_audit_bridge(app: t.Any) -> t.Callable[[str, dict], None]:
    """Return an ``audit(event, detail)`` that writes every Praman auth event
    into the SHABD Grimoire hash chain — so logins, token issuance, MFA
    changes and revocations are **tamper-evident** and share the same
    verifiable chain as business audit. This is the concrete advantage over a
    stock IdP whose event log is an editable database table.

    Pass it to Praman / ``identity_from_config`` / ``build_identity`` as the
    ``audit`` argument.
    """
    def audit(event: str, detail: dict) -> None:
        try:
            subject = detail.get("username") or detail.get("sub") or "system"
            # never record raw secrets/passwords on the chain
            safe = {k: v for k, v in (detail or {}).items()
                    if k not in ("password", "secret", "otp", "client_secret")}
            app.grimoire.append(
                trace_id=secrets.token_hex(8),
                spell=_PRAMAN_EVT_PREFIX + event,
                subject=str(subject),
                args=safe,
                result={"event": event},
                ok=("failed" not in event and "locked" not in event),
            )
        except Exception:
            pass
    return audit


def identity_from_config(cfg: dict, *, secret: bytes,
                         audit: t.Callable[[str, dict], None] | None = None,
                         realm_path: str | None = None) -> IdentityProvider:
    """Build the identity provider selected in config.yaml's `identity` block."""
    provider = (cfg or {}).get("provider", "builtin")
    if provider == "keycloak":
        kc = (cfg or {}).get("keycloak", {})
        return ExternalKeycloakProvider(
            server_url=kc.get("server_url", ""), realm=kc.get("realm", ""),
            client_id=kc.get("client_id", ""),
            client_secret=kc.get("client_secret", ""))
    b = (cfg or {}).get("builtin", {})
    realm = PramanRealm(name=b.get("realm_name", "shabd"), path=realm_path)
    praman = Praman(
        issuer=b.get("issuer", "https://shabd.local"),
        secret=secret, realm=realm,
        access_ttl=int(b.get("access_ttl", 900)),
        refresh_ttl=int(b.get("refresh_ttl", 28800)),
        min_password_len=int(b.get("password_policy", {}).get("min_len", 12)),
        token_alg=b.get("token_alg", "HS256"),
        rsa_bits=int(b.get("rsa_bits", 2048)),
        audit=audit)
    return BuiltinIdentityProvider(praman)


# ===========================================================================
# Standalone HTTP server exposing the OIDC surface (the "own server")
# ===========================================================================

def _make_handler(praman: Praman):
    class _H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code: int, obj: dict):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _form(self) -> dict:
            n = int(self.headers.get("content-length") or 0)
            raw = self.rfile.read(n).decode() if n else ""
            ctype = self.headers.get("content-type", "")
            if "application/json" in ctype:
                try:
                    return json.loads(raw or "{}")
                except Exception:
                    return {}
            import urllib.parse
            return {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}

        def _bearer(self) -> str:
            h = self.headers.get("Authorization", "")
            return h[7:].strip() if h.lower().startswith("bearer ") else ""

        def do_GET(self):  # noqa: N802
            path = self.path.split("?")[0]
            try:
                if path == "/.well-known/openid-configuration":
                    return self._send(200, praman.discovery_document())
                if path == "/praman/jwks":
                    return self._send(200, praman.jwks())
                if path == "/praman/userinfo":
                    return self._send(200, praman.userinfo(self._bearer()))
                if path == "/healthz":
                    return self._send(200, {"ok": True, "service": "praman"})
                self._send(404, {"error": "not_found"})
            except PramanError as e:
                self._send(e.status, {"error": e.error,
                                      "error_description": e.description})

        def do_POST(self):  # noqa: N802
            path = self.path.split("?")[0]
            try:
                if path == "/praman/token":
                    return self._send(200, praman.token(self._form()))
                if path == "/praman/introspect":
                    return self._send(200, praman.introspect(
                        self._form().get("token", "")))
                if path == "/praman/revoke":
                    praman.revoke(self._form().get("token", ""))
                    return self._send(200, {"revoked": True})
                self._send(404, {"error": "not_found"})
            except PramanError as e:
                self._send(e.status, {"error": e.error,
                                      "error_description": e.description})
    return _H


class PramanServer:
    """Runs Praman as its own OIDC-style HTTP server — no Docker, no image."""

    def __init__(self, praman: Praman, *, bind: str = "127.0.0.1",
                 port: int = 8899):
        self.praman = praman
        self.bind = bind
        self.port = port
        self._httpd: socketserver.TCPServer | None = None

    def _build(self) -> None:
        socketserver.ThreadingTCPServer.allow_reuse_address = True
        self._httpd = socketserver.ThreadingTCPServer(
            (self.bind, self.port), _make_handler(self.praman))
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]

    def serve(self) -> None:
        if self._httpd is None:
            self._build()
        self._httpd.serve_forever()

    def start_background(self) -> PramanServer:
        self._build()
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()
        return self

    def shutdown(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
