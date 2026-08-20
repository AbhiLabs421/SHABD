"""
shabd_ui.py — Production no-code web UI for every SHABD feature.

A single-file, stdlib-only HTTP/HTML/JS UI that exposes the whole SHABD
stack — spells, Grimoire chain, agent loop, orchestrator, notary, audit
log, user management, spell builder, token issuance, scope editor and
a remote-SHABD/MCP client console — through a clean browser dashboard.

What's included
===============

  * Keycloak OIDC password-grant authentication (the TCS Ultimatix
    pattern). Login form posts username/password, the server exchanges
    it with Keycloak, stores access_token + refresh_token in an
    HttpOnly cookie session.
  * Three roles enforced server-side:
      - superuser : can manage everything including other users
      - admin     : can run spells, agents, see every audit page
      - user      : can run their own spells, see their own pages
    Roles come from the Keycloak token's `realm_access.roles` claim;
    a `superusers=[...]` allow-list overrides for bootstrap.
  * Pages (each a route + an HTML view):
      /              Dashboard — counts + chain status + recent calls
      /login         Login form
      /spells        Every registered spell, schema-rendered form,
                      one-click invoke, audit trail per spell
      /grimoire      Chain explorer, verify button, tamper indicator,
                      filter by trace/spell/subject
      /audit         Recent calls table with filters
      /agent         No-code agent playground — pick model, add system
                      prompt, choose tools, run, watch step trace
      /orchestrator  Manage intents, classify a query, see route
      /notary        Publish a root, view peer roots, build inclusion
                      proofs, download proofs as JSON
      /users         Admin only: list users, change roles, view session
                      activity
      /builder       Superuser only: paste Python, register as a spell
                      live (sandboxed exec). Lets non-developers add
                      tools without redeploying the server.
      /tokens        Admin only: mint scoped bearer tokens for clients,
                      agents, scripts. The same token an LLM would carry.
      /scopes        Admin only: view and edit which scopes each spell
                      requires.
      /client        Any signed-in user: server-side proxy to ANY other
                      SHABD server. Enter a base URL + token, browse its
                      manifest, invoke any of its spells from this UI.
      /settings      View current config (read-only; secrets masked)
  * REST JSON endpoints behind every page so an SPA or external client
    can call them too — every page just shows what its JSON endpoint
    returns.
  * Cross-Site Request Forgery protection via a per-session token.
  * Audit-on-action: every privileged operation lands as its own
    Grimoire page so the UI itself leaves a tamper-evident trail.

Drop in this snippet to wire it up:

    from shabd import SHABD
    from shabd_ui import UIServer, KeycloakConfig

    app = SHABD("prod-shabd",
                 secret=os.environ["SHABD_SECRET"],
                 grimoire_log_path="/var/lib/shabd/audit.jsonl")
    @app.spell
    def hello(name: str) -> str: return f"Hello {name}"

    ui = UIServer(app,
                   keycloak=KeycloakConfig(
                       server_url=os.environ["KEYCLOAK_URL"],
                       realm=os.environ["KEYCLOAK_REALM"],
                       client_id=os.environ["KEYCLOAK_CLIENT_ID"]),
                   superusers=["abhishek"],
                   bind="0.0.0.0",
                   port=8080)
    ui.serve()

Production notes
================
* TLS termination should be done by nginx/Envoy in front. The UI binds
  to plain HTTP on the bind/port you give it; cookies are flagged
  `Secure` only if `force_secure_cookies=True` (default in production
  helpers).
* Session storage is in-memory by default — fine for a single replica.
  Pass `session_store=...` (any dict-shaped store) to swap in Redis.
* Keycloak's `password` grant must be allowed for the client and the
  client must have `Direct Access Grants Enabled`. Otherwise 400.
* The whole UI is one file — read it, audit it, ship it. Pure stdlib.
"""
from __future__ import annotations

import html as _html
import http.server
import json
import logging
import os
import secrets
import socketserver
import threading
import time
import typing as t
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from http.cookies import SimpleCookie

log = logging.getLogger("shabd.ui")

__all__ = ["UIServer", "KeycloakConfig", "Session", "UIError"]


def main(argv=None) -> int:
    """Entry point for `python -m shabd_ui`."""
    from shabd_ui_cli import main as _main
    return _main(argv)


# ============================================================================
# Errors
# ============================================================================

class UIError(Exception):
    """HTTP-style error carrying a status code."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


# ============================================================================
# Keycloak configuration
# ============================================================================

@dataclass
class KeycloakConfig:
    """Settings for OIDC password-grant against Keycloak."""

    server_url: str            # e.g. https://keycloak.bank.internal
    realm: str                 # e.g. Ultimatix
    client_id: str             # e.g. Tcs-nginx-manager
    client_secret: str = ""    # only if confidential client
    timeout: float = 8.0

    @property
    def token_endpoint(self) -> str:
        base = self.server_url.rstrip("/")
        return (f"{base}/realms/{self.realm}/"
                "protocol/openid-connect/token")

    @property
    def userinfo_endpoint(self) -> str:
        base = self.server_url.rstrip("/")
        return (f"{base}/realms/{self.realm}/"
                "protocol/openid-connect/userinfo")

    def exchange_password(self, username: str, password: str) -> dict:
        """POST password grant. Returns the decoded JSON or raises
        UIError(401)."""
        body = {
            "grant_type": "password",
            "client_id": self.client_id,
            "username": username,
            "password": password,
        }
        if self.client_secret:
            body["client_secret"] = self.client_secret
        data = urllib.parse.urlencode(body).encode()
        req = urllib.request.Request(
            self.token_endpoint, data=data, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read())
            except Exception:
                detail = {"error": str(e)}
            raise UIError(401, detail.get(
                "error_description", detail.get("error", "auth_failed"))) from None
        except Exception as e:
            raise UIError(503, f"keycloak unreachable: {e}") from None

    def fetch_userinfo(self, access_token: str) -> dict:
        req = urllib.request.Request(
            self.userinfo_endpoint,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read())
        except Exception:
            return {}


def _decode_jwt_payload(token: str) -> dict:
    """Decode the (unverified) middle segment of a JWT. Verification is
    Keycloak's job; we only need the claims for role + sub display."""
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        seg = parts[1]
        pad = "=" * (4 - len(seg) % 4)
        import base64 as _b64
        return json.loads(_b64.urlsafe_b64decode(seg + pad))
    except Exception:
        return {}


# ============================================================================
# Spell builder — sandbox primitives
# ============================================================================
#
# `_SAFE_BUILTINS` is a curated namespace handed to `exec()` when a
# superuser registers a spell from the browser. It is NOT a full Python
# sandbox (real isolation needs subinterpreters/containers); it is a
# guard-rail that:
#   * Blocks accidental shell-outs from the form (no `os`, `subprocess`,
#     `socket`, `open`, `eval`, `exec` in the default namespace).
#   * Still lets typical pure-logic spells work (math, dict/list ops,
#     dataclasses via `__import__`).
#   * Allows `__import__` so the spell body can `import json` etc. — a
#     determined superuser CAN still escape; this is policy, not jail.
# Audit logs every successful create_spell call to the Grimoire chain,
# so the action is tamper-evident even if the code itself is malicious.

_SAFE_BUILTINS: dict = {
    "abs": abs, "all": all, "any": any, "ascii": ascii, "bin": bin,
    "bool": bool, "bytearray": bytearray, "bytes": bytes,
    "callable": callable, "chr": chr, "complex": complex,
    "dict": dict, "divmod": divmod, "enumerate": enumerate,
    "filter": filter, "float": float, "format": format,
    "frozenset": frozenset, "getattr": getattr, "hasattr": hasattr,
    "hash": hash, "hex": hex, "id": id, "int": int,
    "isinstance": isinstance, "issubclass": issubclass, "iter": iter,
    "len": len, "list": list, "map": map, "max": max, "min": min,
    "next": next, "object": object, "oct": oct, "ord": ord,
    "pow": pow, "print": print, "range": range, "repr": repr,
    "reversed": reversed, "round": round, "set": set, "setattr": setattr,
    "slice": slice, "sorted": sorted, "str": str, "sum": sum,
    "tuple": tuple, "type": type, "zip": zip,
    "True": True, "False": False, "None": None,
    "Exception": Exception, "ValueError": ValueError,
    "TypeError": TypeError, "KeyError": KeyError, "IndexError": IndexError,
    "RuntimeError": RuntimeError, "ArithmeticError": ArithmeticError,
    "ZeroDivisionError": ZeroDivisionError,
    "__import__": __import__,
    "__build_class__": __build_class__,
    "__name__": "__shabd_spell__",
}


def _compile_spell_source(name: str, source: str) -> t.Callable:
    """Compile a Python source string and return the function named `name`.
    Raises UIError on syntax / runtime / missing-function errors."""
    if not name or not name.isidentifier():
        raise UIError(400, "spell name must be a valid Python identifier")
    if len(source) > 32 * 1024:
        raise UIError(413, "source too large (32 KiB max)")
    try:
        code = compile(source, f"<spell:{name}>", "exec")
    except SyntaxError as e:
        raise UIError(
            400, f"syntax error line {e.lineno}: {e.msg}") from None
    glb: dict = {"__builtins__": _SAFE_BUILTINS}
    try:
        exec(code, glb)  # noqa: S102 — superuser-gated, audited
    except Exception as e:
        raise UIError(
            400, f"{type(e).__name__}: {e}") from None
    fn = glb.get(name)
    if not callable(fn):
        raise UIError(
            400, f"source must define a function named '{name}'")
    return fn


# ============================================================================
# Sessions
# ============================================================================

@dataclass
class Session:
    sid: str
    username: str
    roles: list[str]
    access_token: str
    refresh_token: str = ""
    csrf: str = field(default_factory=lambda: secrets.token_urlsafe(16))
    last_active: float = field(default_factory=time.time)
    created_at: float = field(default_factory=time.time)

    def is_superuser(self) -> bool:
        return "superuser" in self.roles

    def is_admin(self) -> bool:
        return "admin" in self.roles or self.is_superuser()


class _SessionStore:
    def __init__(self, ttl_s: float = 8 * 3600):
        self._store: dict[str, Session] = {}
        self._lock = threading.Lock()
        self.ttl_s = ttl_s

    def put(self, s: Session) -> None:
        with self._lock:
            self._store[s.sid] = s

    def get(self, sid: str) -> Session | None:
        with self._lock:
            s = self._store.get(sid)
            if s and time.time() - s.last_active < self.ttl_s:
                s.last_active = time.time()
                return s
            if s:
                del self._store[sid]
            return None

    def drop(self, sid: str) -> None:
        with self._lock:
            self._store.pop(sid, None)

    def all(self) -> list[Session]:
        with self._lock:
            return list(self._store.values())


# ============================================================================
# HTML template — one polished single-page shell, vanilla CSS+JS
# ============================================================================

_LOGIN_HTML = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><title>SHABD — Login</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
* { box-sizing: border-box; }
body { margin:0; font-family: ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
       background:#faf9f5; color:#2d2b26;
       min-height:100vh; display:grid; place-items:center; }
.card { background:#ffffff; border:1px solid #e7e3d9; border-radius:16px;
        padding:38px 40px; width:min(420px,92vw); box-shadow:0 10px 40px rgba(45,43,38,0.08); }
h1 { margin:0 0 4px; font-size:28px; letter-spacing:-0.5px; color:#c96442; }
.sub { color:#78756c; font-size:13px; margin-bottom:24px; }
label { display:block; font-size:12px; color:#5c5a52; margin:14px 0 6px;
        text-transform:uppercase; letter-spacing:1px; }
input { width:100%; padding:12px 14px; background:#fff; border:1px solid #e7e3d9;
        border-radius:10px; color:#2d2b26; font-size:14px; outline:none;
        transition:border-color 0.15s; }
input:focus { border-color:#c96442; }
button { margin-top:22px; width:100%; padding:13px; background:#c96442;
         border:0; border-radius:10px; color:#fff; font-size:15px; font-weight:600;
         cursor:pointer; transition:filter 0.12s; }
button:hover { filter:brightness(1.06); }
.err { color:#c0392b; font-size:13px; margin-top:14px; padding:10px;
       background:rgba(192,57,43,0.08); border-radius:8px; border:1px solid #f0c9c2; }
.foot { margin-top:24px; font-size:11px; color:#94918a; text-align:center; }
</style></head><body>
<form class="card" method="POST" action="/login">
  <h1>🔮 SHABD</h1>
  <div class="sub">Spell Hub for Agentic Builders &amp; Developers</div>
  <label for="u">Username</label>
  <input id="u" name="username" autocomplete="username" required autofocus>
  <label for="p">Password</label>
  <input id="p" name="password" type="password" autocomplete="current-password" required>
  __ERROR__
  <button type="submit">Sign in</button>
  <div class="foot">__AUTH_LINE__ · <a href="/register" style="color:#c96442">create an account</a></div>
</form>
</body></html>"""

_REGISTER_HTML = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><title>SHABD — Register</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
* { box-sizing: border-box; }
body { margin:0; font-family: ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
       background:#faf9f5; color:#2d2b26;
       min-height:100vh; display:grid; place-items:center; }
.card { background:#ffffff; border:1px solid #e7e3d9; border-radius:16px;
        padding:38px 40px; width:min(440px,92vw); box-shadow:0 10px 40px rgba(45,43,38,0.08); }
h1 { margin:0 0 4px; font-size:28px; letter-spacing:-0.5px; color:#c96442; }
.sub { color:#78756c; font-size:13px; margin-bottom:18px; }
.badge { font-size:12px; color:#2e7d51; margin-bottom:18px;
         padding:10px; border:1px solid #bfe0cd; border-radius:8px;
         background:rgba(46,125,81,0.06); }
label { display:block; font-size:12px; color:#5c5a52; margin:14px 0 6px;
        text-transform:uppercase; letter-spacing:1px; }
input { width:100%; padding:12px 14px; background:#fff; border:1px solid #e7e3d9;
        border-radius:10px; color:#2d2b26; font-size:14px; outline:none; }
input:focus { border-color:#c96442; }
button { margin-top:22px; width:100%; padding:13px; background:#c96442;
         border:0; border-radius:10px; color:#fff; font-size:15px; font-weight:600; cursor:pointer; }
.err { color:#c0392b; font-size:13px; margin-top:14px; padding:10px;
       background:rgba(192,57,43,0.08); border-radius:8px; border:1px solid #f0c9c2; }
.foot { margin-top:18px; font-size:11px; color:#94918a; text-align:center; }
.foot a { color:#c96442; text-decoration:none; }
</style></head><body>
<form class="card" method="POST" action="/register">
  <h1>🔮 SHABD</h1>
  <div class="sub">Create your account</div>
  <div class="badge">__BADGE__</div>
  <label for="u">Username (letters, digits, . _ -)</label>
  <input id="u" name="username" autocomplete="username" required autofocus>
  <label for="p">Password (min 8 chars)</label>
  <input id="p" name="password" type="password" autocomplete="new-password" required minlength="8">
  <label for="p2">Confirm password</label>
  <input id="p2" name="password2" type="password" autocomplete="new-password" required minlength="8">
  __ERROR__
  <button type="submit">Create account</button>
  <div class="foot">Already have an account? <a href="/login">Sign in</a></div>
</form>
</body></html>"""

_APP_HTML = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><title>SHABD Console</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
* { box-sizing: border-box; }
:root {
  /* Claude-inspired warm light theme */
  --bg:#faf9f5; --panel:#ffffff; --panel2:#f0ede4; --line:#e7e3d9;
  --text:#2d2b26; --dim:#78756c; --accent:#c96442; --accent2:#b4502f;
  --ok:#2e7d51; --warn:#b07d18; --err:#c0392b; --info:#3b7ea1;
}
body { margin:0; font-family: ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
       background:var(--bg); color:var(--text); min-height:100vh; display:flex; }
nav { width:240px; background:var(--panel); border-right:1px solid var(--line);
      padding:24px 0; flex-shrink:0; position:sticky; top:0; height:100vh;
      overflow-y:auto; }
nav .brand { padding:0 24px 24px; border-bottom:1px solid var(--line);
             margin-bottom:18px; }
nav .brand h1 { margin:0; font-size:20px; color:var(--accent); }
nav .brand .who { font-size:11px; color:var(--dim); margin-top:6px;
                  text-transform:uppercase; letter-spacing:1px; }
nav a { display:block; padding:12px 24px; color:var(--dim); text-decoration:none;
        font-size:14px; transition: all 0.15s; border-left:3px solid transparent; }
nav a:hover { color:var(--text); background:var(--panel2); }
nav a.active { color:var(--accent); background:var(--panel2); border-left-color:var(--accent); }
nav a .icon { display:inline-block; width:20px; }
nav .foot { position:absolute; bottom:0; left:0; right:0; padding:14px 24px;
            border-top:1px solid var(--line); }
nav .foot a { display:block; padding:8px 0; color:var(--err); font-size:13px; }
.themebar { display:flex; gap:8px; margin-bottom:10px; }
.themebar .sw { width:20px; height:20px; border-radius:50%; padding:0;
                border:2px solid #fff; box-shadow:0 0 0 1px var(--line);
                cursor:pointer; transition:transform .1s; }
.themebar .sw:hover { transform:scale(1.15); }
.themebar .sw.active { box-shadow:0 0 0 2px var(--accent); }
main { flex:1; padding:32px 40px; overflow-x:hidden; }
.head { display:flex; justify-content:space-between; align-items:center;
        margin-bottom:24px; }
.head h2 { margin:0; font-size:24px; }
.cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr));
         gap:16px; margin-bottom:24px; }
.card { background:var(--panel); border:1px solid var(--line); border-radius:10px;
        padding:18px 20px; }
.card .label { font-size:11px; color:var(--dim); text-transform:uppercase;
               letter-spacing:1px; margin-bottom:8px; }
.card .value { font-size:26px; font-weight:600; }
.card .delta { font-size:12px; color:var(--dim); margin-top:4px; }
.panel { background:var(--panel); border:1px solid var(--line); border-radius:10px;
         padding:20px 22px; margin-bottom:18px; }
.panel h3 { margin:0 0 12px; font-size:16px; color:var(--accent); }
table { width:100%; border-collapse:collapse; font-size:13px; }
table th, table td { text-align:left; padding:9px 8px; border-bottom:1px solid var(--line); }
table th { color:var(--dim); text-transform:uppercase; font-size:11px; letter-spacing:1px; }
table tr:hover td { background:var(--panel2); }
button, .btn { background:var(--accent2); color:#fff; border:0; border-radius:8px;
               padding:8px 16px; font-size:13px; font-weight:500; cursor:pointer;
               text-decoration:none; display:inline-block; transition:filter 0.12s; }
button:hover, .btn:hover { filter:brightness(1.06); }
button.ghost { background:#fff; border:1px solid var(--line); color:var(--text); }
button.danger { background:#c0392b; }
input, select, textarea { background:#fff; border:1px solid var(--line);
       color:var(--text); padding:9px 12px; border-radius:8px; font-size:13px;
       font-family:inherit; outline:none; transition:border-color 0.15s; }
input:focus, select:focus, textarea:focus { border-color:var(--accent); }
input.full, textarea.full, select.full { width:100%; }
textarea { font-family: ui-monospace,Menlo,Consolas,monospace; min-height:90px;
           resize:vertical; }
.row { display:flex; gap:12px; align-items:flex-end; margin-bottom:14px;
       flex-wrap:wrap; }
.row > div { flex:1; min-width:180px; }
.row label { display:block; font-size:11px; color:var(--dim); margin-bottom:4px;
             text-transform:uppercase; letter-spacing:1px; }
.tag { display:inline-block; padding:3px 9px; border-radius:11px; font-size:11px;
       font-weight:500; }
.tag.ok { background:rgba(46,125,81,0.12); color:var(--ok); }
.tag.err { background:rgba(192,57,43,0.12); color:var(--err); }
.tag.warn { background:rgba(176,125,24,0.14); color:var(--warn); }
.tag.info { background:rgba(201,100,66,0.12); color:var(--accent); }
pre { background:#2d2b26; border:1px solid var(--line); border-radius:8px;
      padding:14px; font-size:12px; overflow-x:auto; line-height:1.6; color:#f0ede4;
      font-family: ui-monospace,Menlo,Consolas,monospace; }
.empty { text-align:center; padding:40px 0; color:var(--dim); font-size:13px; }
.split { display:grid; grid-template-columns: 1fr 1fr; gap:18px; }
@media (max-width:900px) { .split { grid-template-columns: 1fr; } nav { width:200px; } }
.kbd { font-family:ui-monospace,Menlo,Consolas,monospace;
       background:var(--bg); padding:2px 6px; border-radius:4px;
       border:1px solid var(--line); font-size:12px; }
</style></head><body>
<nav>
  <div class="brand">
    <h1>🔮 SHABD</h1>
    <div class="who">__USER_BADGE__</div>
  </div>
  <a href="/" data-page="dashboard"><span class="icon">📊</span> Dashboard</a>
  <a href="/spells" data-page="spells"><span class="icon">✨</span> Spells</a>
  <a href="/grimoire" data-page="grimoire"><span class="icon">🔗</span> Grimoire</a>
  <a href="/audit" data-page="audit"><span class="icon">📜</span> Audit Log</a>
  <a href="/agent" data-page="agent"><span class="icon">🤖</span> Agent Lab</a>
  <a href="/chains" data-page="chains"><span class="icon">⛓️</span> Spell Chains</a>
  <a href="/knowledge" data-page="knowledge"><span class="icon">📚</span> Knowledge Base</a>
  <a href="/sql-intelligence" data-page="sql-intelligence"><span class="icon">🗄️</span> SQL Intelligence</a>
  <a href="/nova" data-page="nova"><span class="icon">🌟</span> Nova</a>
  <a href="/orchestrator" data-page="orchestrator"><span class="icon">🎯</span> Orchestrator</a>
  <a href="/notary" data-page="notary"><span class="icon">🤝</span> Notary</a>
  <a href="/client" data-page="client"><span class="icon">🌐</span> Client Console</a>
  <a href="/sources" data-page="sources"><span class="icon">🔌</span> Tool Sources</a>
  <a href="/api-docs" data-page="api-docs"><span class="icon">📖</span> API Docs</a>
  __ADMIN_NAV__
  __SUPER_NAV__
  <a href="/settings" data-page="settings"><span class="icon">⚙️</span> Settings</a>
  <div class="foot">
    <div class="themebar" title="Theme">
      <button class="sw" data-theme="claude" style="background:#c96442" title="Warm"></button>
      <button class="sw" data-theme="purple" style="background:#7c3aed" title="Purple"></button>
      <button class="sw" data-theme="green" style="background:#16a34a" title="Green"></button>
      <button class="sw" data-theme="blue" style="background:#2563eb" title="Blue"></button>
      <button class="sw" data-theme="white" style="background:#334155" title="White"></button>
    </div>
    <a href="/logout">↩  Sign out</a>
  </div>
</nav>
<main id="main">__BODY__</main>
<script>
const csrf = "__CSRF__";
async function api(path, opts={}) {
  opts.headers = Object.assign({'X-CSRF': csrf,
                                  'Accept': 'application/json'},
                                 opts.headers || {});
  if (opts.body && typeof opts.body !== 'string') {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(opts.body);
  }
  const r = await fetch(path, opts);
  const t = await r.text();
  try { return { status: r.status, body: JSON.parse(t) }; }
  catch { return { status: r.status, body: t }; }
}
// Highlight current nav
const here = location.pathname;
document.querySelectorAll('nav a[data-page]').forEach(a => {
  if (a.getAttribute('href') === here ||
      (here === '/' && a.dataset.page === 'dashboard')) {
    a.classList.add('active');
  }
});
// ---------- Theme switcher ----------
const THEMES = {
  claude: {bg:'#faf9f5',panel:'#ffffff',panel2:'#f0ede4',line:'#e7e3d9',
           text:'#2d2b26',dim:'#78756c',accent:'#c96442',accent2:'#b4502f'},
  purple: {bg:'#faf7fd',panel:'#ffffff',panel2:'#f2ecfa',line:'#ece2f7',
           text:'#2a2438',dim:'#7a6f8c',accent:'#7c3aed',accent2:'#6d28d9'},
  green:  {bg:'#f5faf6',panel:'#ffffff',panel2:'#e9f5ec',line:'#dcefe0',
           text:'#22302a',dim:'#6b7d70',accent:'#16a34a',accent2:'#15803d'},
  blue:   {bg:'#f4f8fd',panel:'#ffffff',panel2:'#e8f0fb',line:'#dbe8f6',
           text:'#20293a',dim:'#6a7690',accent:'#2563eb',accent2:'#1d4ed8'},
  white:  {bg:'#ffffff',panel:'#ffffff',panel2:'#f1f5f9',line:'#e5e7eb',
           text:'#1e293b',dim:'#64748b',accent:'#334155',accent2:'#1e293b'},
};
function applyTheme(name) {
  const t = THEMES[name] || THEMES.claude;
  const r = document.documentElement.style;
  for (const k in t) r.setProperty('--'+k, t[k]);
  try { localStorage.setItem('shabd_theme', name); } catch(e) {}
  document.querySelectorAll('.themebar .sw').forEach(b =>
    b.classList.toggle('active', b.dataset.theme === name));
}
document.querySelectorAll('.themebar .sw').forEach(b =>
  b.addEventListener('click', () => applyTheme(b.dataset.theme)));
(function(){ let s='claude'; try{ s=localStorage.getItem('shabd_theme')||'claude'; }catch(e){}
  applyTheme(s); })();
__PAGE_SCRIPT__
</script></body></html>"""


# ============================================================================
# UIServer
# ============================================================================

class UIServer:
    """HTTP server that exposes SHABD through a no-code web UI."""

    def __init__(self, app: t.Any, *,
                 keycloak: KeycloakConfig | None = None,
                 superusers: t.Iterable[str] = (),
                 admins: t.Iterable[str] = (),
                 bind: str = "127.0.0.1",
                 port: int = 8080,
                 force_secure_cookies: bool = False,
                 session_ttl_s: float = 8 * 3600,
                 orchestrator: t.Any = None,
                 notary: t.Any = None,
                 users: t.Any = None,
                 allow_self_register: bool = True):
        self.app = app
        self.keycloak = keycloak
        self._superusers = {u.lower() for u in superusers}
        self._admins = {u.lower() for u in admins}
        self.bind = bind
        self.port = port
        self.force_secure_cookies = force_secure_cookies
        # Security headers (enterprise hardening). All safe to send; HSTS is
        # ignored by browsers over plain HTTP, so it's harmless in dev. The CSP
        # is deliberately relaxed to allow the UI's own inline scripts/styles
        # while still blocking external script/frame sources. Toggle off with
        # ui.security_headers = False (e.g. behind a gateway that sets its own).
        self.security_headers = True
        self.hsts = "max-age=31536000; includeSubDomains"
        self.csp = ("default-src 'self'; "
                    "script-src 'self' 'unsafe-inline'; "
                    "style-src 'self' 'unsafe-inline'; "
                    "img-src 'self' data:; "
                    "connect-src 'self'; "
                    "base-uri 'self'; form-action 'self'; "
                    "frame-ancestors 'none'")
        self.permissions_policy = "geolocation=(), microphone=(), camera=()"
        self.sessions = _SessionStore(ttl_s=session_ttl_s)
        self.orchestrator = orchestrator
        self.notary = notary
        self._login_throttle: dict[str, list[float]] = {}
        self._throttle_lock = threading.Lock()
        self._dynamic_spells: dict[str, dict] = {}
        # v2.10 — saved named agents. Each entry:
        # {name, system, tools (list of spell names), created_by, ts}
        self._agents: dict[str, dict] = {}
        # v2.10 — current LLM config (read by Agent Lab).
        self._llm_config: dict = {
            "backend": "none",   # none | ollama | openai | anthropic
            "base_url": "",
            "model": "",
            "api_key": "",       # never returned in API responses
        }
        # Sidecar state file lives next to the audit log so config +
        # named agents survive restart. (Audit chain still records
        # what changed, this file holds the current values.)
        self._state_path: str | None = None
        try:
            gl = getattr(app, "_grimoire_log", None)
            if gl is not None and getattr(gl, "path", None):
                self._state_path = gl.path + ".state.json"
        except Exception:
            self._state_path = None
        # v2.11 — tracked + revocable tokens
        self._issued_tokens: dict[str, dict] = {}
        self._revoked_jtis: set[str] = set()
        # v2.12 — UI-managed orchestrator intents
        self._intents: dict[str, dict] = {}
        # v2.13 — external tool sources (MCP / other SHABD servers).
        # Each entry: {name, type, url, token, transport, tools[],
        # connected, error}. Imported tools become local proxy spells
        # tagged "source:<name>", so they show up everywhere.
        self._tool_sources: dict[str, dict] = {}
        self._mcp_clients: dict[str, t.Any] = {}  # live MCPClient objs
        # v2.17 — UI-created spell chains (deterministic pipelines)
        self._chains: dict[str, dict] = {}
        # v2.18 — multi-agent flows (sequential / parallel orchestrators)
        self._flows: dict[str, dict] = {}
        # v2.19 — visual-studio chatbots (system + tools + agents + graph)
        self._chatbots: dict[str, dict] = {}
        # v2.22 — knowledge bases (document RAG). Pure-stdlib TF-IDF.
        self._kbs: dict[str, dict] = {}
        # v2.23 — external service connectors (e.g. an external SQL /
        # text-to-SQL / RAG API). We only proxy + expose as a tool.
        self._sql_services: dict[str, dict] = {}
        # v2.25 — Nova: an external multi-tenant RAG pipeline service
        # (Tenants -> Pipelines -> Ingest -> Query) driven from our UI.
        self._nova: dict = {"base_url": "", "api_key": "",
                            "auth_style": "bearer"}
        self._nova_exposed: dict[str, dict] = {}   # spell -> {pid,name}
        self._load_state_file()
        self._replay_admin_state()
        self._wrap_token_verify()
        # Re-register UI-built spells + chains from the state file so
        # they survive a restart. Spells first (chains depend on them).
        try:
            self._recreate_dynamic_on_boot()
        except Exception:
            log.exception("dynamic spell/chain recreate failed")
        # Best-effort re-import of external tool sources (won't crash
        # boot if a remote is down).
        try:
            self.reconnect_sources_on_boot()
        except Exception:
            log.exception("tool-source reconnect failed")
        # Built-in user store. Defaults:
        #   * Keycloak set → no built-in store (Keycloak is auth)
        #   * SHABD_UI_BOOTSTRAP_PASSWORD env var set → no store
        #       (legacy single-user env mode, useful for tests)
        #   * Otherwise → auto-create UserStore on app.grimoire
        # Pass `users=False` to disable explicitly.
        if users is None and keycloak is None:
            if not os.environ.get("SHABD_UI_BOOTSTRAP_PASSWORD"):
                from shabd_users import UserStore
                users = UserStore(app)
        self.users = users if users not in (None, False) else None
        self.allow_self_register = bool(allow_self_register)

    # ---- auth ----

    def _roles_for(self, username: str, token: str) -> list[str]:
        roles: set[str] = set()
        claims = _decode_jwt_payload(token)
        for r in (claims.get("realm_access") or {}).get("roles", []):
            roles.add(r)
        lu = username.lower()
        if lu in self._superusers:
            roles.add("superuser")
            roles.add("admin")
            roles.add("user")
        elif lu in self._admins:
            roles.add("admin")
            roles.add("user")
        else:
            roles.add("user")
        return sorted(roles)

    def _login(self, username: str, password: str) -> Session:
        # Throttle: 5 attempts/30s per username
        now = time.time()
        with self._throttle_lock:
            recent = [t for t in self._login_throttle.get(username, [])
                       if now - t < 30]
            if len(recent) >= 5:
                raise UIError(429, "too many attempts, slow down")
            recent.append(now)
            self._login_throttle[username] = recent

        if self.keycloak is None:
            # 1) Built-in users (hash-chained, default)
            if self.users is not None:
                try:
                    u = self.users.login(username, password)
                except Exception as e:
                    status = getattr(e, "status", 401)
                    raise UIError(status, "bad credentials") from None
                sess = Session(
                    sid=secrets.token_urlsafe(24),
                    username=u.username,
                    roles=list(u.roles),
                    access_token="builtin",
                )
                self.sessions.put(sess)
                return sess
            # 2) Env bootstrap fallback (single user, no DB)
            expected = os.environ.get(
                "SHABD_UI_BOOTSTRAP_PASSWORD", "")
            boot_user = os.environ.get(
                "SHABD_UI_BOOTSTRAP_USER", "admin")
            if (not expected or username != boot_user
                    or password != expected):
                raise UIError(401, "bad credentials")
            sess = Session(
                sid=secrets.token_urlsafe(24),
                username=username,
                roles=["superuser", "admin", "user"],
                access_token="bootstrap",
            )
            self.sessions.put(sess)
            return sess

        tok = self.keycloak.exchange_password(username, password)
        roles = self._roles_for(username, tok.get("access_token", ""))
        sess = Session(
            sid=secrets.token_urlsafe(24),
            username=username,
            roles=roles,
            access_token=tok.get("access_token", ""),
            refresh_token=tok.get("refresh_token", ""),
        )
        self.sessions.put(sess)
        return sess

    # ---- self-registration / built-in user mgmt ----

    def _register(self, username: str, password: str) -> Session:
        """Self-service registration. Returns an authenticated session.
        First user to register is auto-promoted to superuser."""
        if self.users is None:
            raise UIError(404, "registration disabled on this server")
        if not self.allow_self_register and not self.users.is_first_run():
            raise UIError(
                403,
                "self-registration is closed; ask an admin to invite you")
        try:
            u = self.users.register(
                username, password, actor=username)
        except Exception as e:
            status = getattr(e, "status", 400)
            raise UIError(
                status, getattr(e, "message", str(e))) from None
        sess = Session(
            sid=secrets.token_urlsafe(24),
            username=u.username,
            roles=list(u.roles),
            access_token="builtin",
        )
        self.sessions.put(sess)
        return sess

    def admin_create_user(self, sess: Session, *,
                          username: str, password: str,
                          roles: t.Iterable[str]) -> dict:
        if self.users is None:
            raise UIError(404, "user store not enabled")
        try:
            u = self.users.register(
                username, password, roles=list(roles),
                actor=sess.username)
        except Exception as e:
            status = getattr(e, "status", 400)
            raise UIError(
                status, getattr(e, "message", str(e))) from None
        self._audit_admin_action(sess, "create_user",
                                  {"username": username,
                                   "roles": list(roles)}, True)
        return u.to_public()

    def admin_set_roles(self, sess: Session, *,
                        username: str,
                        roles: t.Iterable[str]) -> dict:
        if self.users is None:
            raise UIError(404, "user store not enabled")
        try:
            self.users.set_roles(
                username, list(roles), actor=sess.username)
        except Exception as e:
            status = getattr(e, "status", 400)
            raise UIError(
                status, getattr(e, "message", str(e))) from None
        self._audit_admin_action(sess, "set_roles",
                                  {"username": username,
                                   "roles": list(roles)}, True)
        return self.users.get(username).to_public()

    def admin_delete_user(self, sess: Session, *,
                          username: str) -> dict:
        if self.users is None:
            raise UIError(404, "user store not enabled")
        if username == sess.username:
            raise UIError(400, "cannot delete yourself")
        try:
            self.users.delete(username, actor=sess.username)
        except Exception as e:
            status = getattr(e, "status", 400)
            raise UIError(
                status, getattr(e, "message", str(e))) from None
        # Drop any live sessions for this user
        for s in list(self.sessions.all()):
            if s.username == username:
                self.sessions.drop(s.sid)
        self._audit_admin_action(sess, "delete_user",
                                  {"username": username}, True)
        return {"ok": True, "username": username}

    def admin_reset_password(self, sess: Session, *,
                              username: str,
                              new_password: str) -> dict:
        if self.users is None:
            raise UIError(404, "user store not enabled")
        try:
            self.users.set_password(
                username, new_password, actor=sess.username)
        except Exception as e:
            status = getattr(e, "status", 400)
            raise UIError(
                status, getattr(e, "message", str(e))) from None
        self._audit_admin_action(sess, "reset_password",
                                  {"username": username}, True)
        return {"ok": True, "username": username}

    # ---- admin operations (UI-driven, audited) ----

    # ---- v2.10: persistent admin state ----

    def _load_state_file(self) -> None:
        if not self._state_path:
            return
        try:
            with open(self._state_path) as f:
                d = json.load(f)
            cfg = d.get("llm_config") or {}
            if isinstance(cfg, dict):
                self._llm_config.update(cfg)
            ags = d.get("agents") or {}
            if isinstance(ags, dict):
                self._agents.update(ags)
            ds = d.get("dynamic_spells") or {}
            if isinstance(ds, dict):
                self._dynamic_spells.update(ds)
            issued = d.get("issued_tokens") or {}
            if isinstance(issued, dict):
                self._issued_tokens.update(issued)
            revoked = d.get("revoked_jtis") or []
            if isinstance(revoked, list):
                self._revoked_jtis.update(revoked)
            intents = d.get("intents") or {}
            if isinstance(intents, dict):
                self._intents.update(intents)
            sources = d.get("tool_sources") or {}
            if isinstance(sources, dict):
                self._tool_sources.update(sources)
            chains = d.get("chains") or {}
            if isinstance(chains, dict):
                self._chains.update(chains)
            flows = d.get("flows") or {}
            if isinstance(flows, dict):
                self._flows.update(flows)
            bots = d.get("chatbots") or {}
            if isinstance(bots, dict):
                self._chatbots.update(bots)
            kbs = d.get("knowledge_bases") or {}
            if isinstance(kbs, dict):
                self._kbs.update(kbs)
            svcs = d.get("sql_services") or {}
            if isinstance(svcs, dict):
                self._sql_services.update(svcs)
            nova = d.get("nova") or {}
            if isinstance(nova, dict):
                self._nova.update(nova)
            nova_x = d.get("nova_exposed") or {}
            if isinstance(nova_x, dict):
                self._nova_exposed.update(nova_x)
        except FileNotFoundError:
            pass
        except Exception:
            log.exception("could not load state file")

    def _save_state_file(self) -> None:
        if not self._state_path:
            return
        try:
            tmp = self._state_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({
                    "llm_config": self._llm_config,
                    "agents": self._agents,
                    "dynamic_spells": self._dynamic_spells,
                    "issued_tokens": self._issued_tokens,
                    "revoked_jtis": sorted(self._revoked_jtis),
                    "intents": self._intents,
                    "tool_sources": {
                        n: {k: v for k, v in s.items()
                            if k != "tools"}  # tools re-imported on boot
                        for n, s in self._tool_sources.items()
                    },
                    "chains": self._chains,
                    "flows": self._flows,
                    "chatbots": self._chatbots,
                    "knowledge_bases": self._kbs,
                    "sql_services": self._sql_services,
                    "nova": self._nova,
                    "nova_exposed": self._nova_exposed,
                }, f, separators=(",", ":"))
            os.replace(tmp, self._state_path)
        except Exception:
            log.exception("could not save state file")

    def _wrap_token_verify(self) -> None:
        """Intercept the app's token verifier so revoked JTIs fail
        even though the HMAC signature is still valid."""
        try:
            original = self.app.tokens.verify
        except AttributeError:
            return
        revoked = self._revoked_jtis

        def _verify(token: str) -> dict:
            payload = original(token)
            jti = payload.get("jti", "")
            if jti and jti in revoked:
                # Use the same error class app uses elsewhere so the
                # HTTP server returns the same 401 envelope.
                from shabd import AuthError
                raise AuthError("token revoked")
            return payload

        self.app.tokens.verify = _verify  # type: ignore[assignment]

    def _recreate_dynamic_on_boot(self) -> None:
        """Re-register UI-built spells and chains from the state file so
        they are live (callable) again after a restart, not just listed
        in metadata. Spells are recreated before chains because a chain
        references its step spells."""
        # 1) Dynamic spells
        for name, info in list(self._dynamic_spells.items()):
            if name in self.app._spells:
                continue
            src = info.get("source")
            if not src:
                continue
            try:
                fn = _compile_spell_source(name, src)
                self.app.spell(
                    name=name,
                    description=info.get("description", ""),
                    scopes=info.get("scopes", []),
                    tags=info.get("tags", []),
                    idempotent=True,
                )(fn)
            except Exception as e:
                log.warning("could not recreate spell %s: %s", name, e)
        # 2) Chains (their step spells must already exist)
        for name, info in list(self._chains.items()):
            if name in self.app._spells:
                continue
            steps = info.get("steps", [])
            if len(steps) < 2 or any(
                    s not in self.app._spells for s in steps):
                log.warning("skipping chain %s — missing steps", name)
                continue
            try:
                self.app.chain(
                    " | ".join(steps), name=name,
                    description=info.get("description") or None,
                    scopes=info.get("scopes", []))
            except Exception as e:
                log.warning("could not recreate chain %s: %s", name, e)
        # 3) Re-expose knowledge-base spells that were exposed before.
        for kb_name, kb in list(self._kbs.items()):
            if not kb.get("exposed"):
                continue
            if f"kb_{kb_name}" in self.app._spells:
                continue
            try:
                self._reexpose_kb_spell(kb_name)
            except Exception as e:
                log.warning("could not re-expose KB %s: %s", kb_name, e)
        # 4) Re-expose external SQL-Intelligence connector spells.
        for svc_name, svc in list(self._sql_services.items()):
            if not svc.get("exposed"):
                continue
            if f"sql_{svc_name}" in self.app._spells:
                continue
            try:
                self._register_sql_spell(svc_name)
            except Exception as e:
                log.warning("could not re-expose SQL svc %s: %s",
                            svc_name, e)
        # 5) Re-expose Nova pipeline tools.
        for spell_name in list(self._nova_exposed.keys()):
            if spell_name in self.app._spells:
                continue
            try:
                self._register_nova_spell(spell_name)
            except Exception as e:
                log.warning("could not re-expose Nova %s: %s",
                            spell_name, e)

    def _reexpose_kb_spell(self, name: str) -> None:
        """Register the kb_<name> spell without an audit page (used on
        boot). Mirrors expose_kb's closure."""
        spell_name = f"kb_{name}"
        ui_ref = self

        def _kb_answer(question: str) -> dict:
            hits = ui_ref.query_kb(name, question, top_k=4)
            if not hits:
                return {"answer": "No relevant information found in "
                                  f"the '{name}' knowledge base.",
                        "sources": []}
            context = "\n\n---\n\n".join(h["text"] for h in hits)
            from shabd_agent import MockBackend
            backend = ui_ref.build_llm_backend()
            if isinstance(backend, MockBackend):
                answer = ("(no LLM set — returning the most relevant "
                          "passages)\n\n" + context)
            else:
                msgs = [
                    {"role": "system",
                     "content": ("Answer using ONLY the context. If not "
                                 "present, say you don't know.")},
                    {"role": "user",
                     "content": f"Context:\n{context}\n\nQuestion: {question}"},
                ]
                try:
                    answer = backend.chat(msgs, []).text or context
                except Exception as e:
                    answer = f"(LLM error: {e})\n\n{context}"
            return {"answer": answer,
                    "sources": [h["source"] for h in hits]}

        _kb_answer.__name__ = spell_name
        _kb_answer.__doc__ = (
            f"Answer questions from the '{name}' knowledge base.")
        self.app.spell(name=spell_name, description=_kb_answer.__doc__,
                       tags=["kb"], idempotent=True)(_kb_answer)

    def _replay_admin_state(self) -> None:
        """Reconstruct LLM config and saved agents from the Grimoire
        chain so they survive restart without a separate database."""
        try:
            pages = self.app.grimoire.pages(limit=10 ** 9)
        except Exception:
            return
        for p in pages:
            spell = p.get("spell", "")
            args = p.get("_args_plain", {})
            if spell == "__ui_admin:set_llm_config":
                cfg = args.get("config") or {}
                if isinstance(cfg, dict):
                    self._llm_config.update(cfg)
            elif spell == "__ui_admin:save_agent":
                ag = args.get("agent") or {}
                if isinstance(ag, dict) and ag.get("name"):
                    self._agents[ag["name"]] = ag
            elif spell == "__ui_admin:delete_agent":
                name = args.get("name")
                if name:
                    self._agents.pop(name, None)
            elif spell == "__ui_admin:update_spell_source":
                # Re-apply dynamic spell metadata so the list page
                # tells the user "this came from the UI".
                meta = args.get("meta") or {}
                if meta.get("name"):
                    self._dynamic_spells[meta["name"]] = meta

    def _audit_admin_action(self, sess: Session, action: str,
                             detail: dict, ok: bool) -> None:
        """Drop a synthetic page into the Grimoire so every privileged
        UI action is tamper-evident even if the action itself is benign."""
        try:
            self.app.grimoire.append(
                trace_id=secrets.token_hex(8),
                spell=f"__ui_admin:{action}",
                subject=sess.username,
                args=detail,
                result={"ok": ok},
                ok=ok,
            )
        except Exception:
            log.exception("audit append failed for %s", action)

    def create_spell(self, sess: Session, *, name: str, source: str,
                     description: str = "",
                     scopes: t.Iterable[str] = (),
                     tags: t.Iterable[str] = ()) -> dict:
        """Compile `source`, register the function as a spell. Superuser
        only. Audited. Returns the new spell's metadata."""
        if name in self.app._spells:
            raise UIError(409, f"spell '{name}' already exists")
        fn = _compile_spell_source(name, source)
        try:
            self.app.spell(
                name=name,
                description=description or (fn.__doc__ or "").strip(),
                scopes=list(scopes),
                tags=list(tags),
                # Builder spells are pure-Python read-style functions
                # by default; marking them idempotent means a weak LLM
                # that retries the same call gets the cached result
                # instead of a confusing duplicate_call error.
                idempotent=True,
            )(fn)
        except Exception as e:
            raise UIError(400, str(e)) from None
        import hashlib as _h
        src_hash = _h.sha256(source.encode()).hexdigest()
        self._dynamic_spells[name] = {
            "source": source, "hash": src_hash,
            "description": description or "",
            "scopes": list(scopes), "tags": list(tags),
            "created_by": sess.username, "created_at": time.time(),
        }
        self._save_state_file()
        spell = self.app._spells[name]
        meta = {
            "name": spell.name,
            "description": spell.description,
            "scopes": list(spell.scopes or []),
            "tags": list(spell.tags or []),
            "schema": spell.schema,
            "source_hash": src_hash[:16],
        }
        self._audit_admin_action(sess, "create_spell", {
            "name": name, "source_hash": src_hash[:16],
            "scopes": list(scopes), "tags": list(tags),
        }, True)
        return meta

    def create_chain(self, sess: Session, *,
                     name: str, steps: t.Iterable[str],
                     description: str = "",
                     scopes: t.Iterable[str] = ()) -> dict:
        """Create a deterministic spell pipeline: step[0] runs, its
        output feeds step[1], and so on. The chain itself becomes a
        callable spell (usable in /spells, /query, agents, manifest).
        No LLM — fixed order, fast, predictable."""
        name = (name or "").strip()
        if not name or not name.replace("_", "").replace(
                "-", "").isalnum():
            raise UIError(400, "chain name must be alphanumeric / _ / -")
        steps_list = [s.strip() for s in steps if s.strip()]
        if len(steps_list) < 2:
            raise UIError(400, "a chain needs at least 2 steps")
        for s in steps_list:
            if s not in self.app._spells:
                raise UIError(404, f"unknown spell in chain: {s}")
        if name in self.app._spells:
            raise UIError(409, f"a spell/chain named '{name}' exists")
        pipeline = " | ".join(steps_list)
        try:
            self.app.chain(pipeline, name=name,
                            description=description or None,
                            scopes=list(scopes))
        except Exception as e:
            raise UIError(400, str(e)) from None
        self._chains[name] = {
            "name": name, "steps": steps_list,
            "description": description, "scopes": list(scopes),
            "created_by": sess.username, "ts": time.time(),
        }
        self._save_state_file()
        self._audit_admin_action(sess, "create_chain", {
            "name": name, "steps": steps_list,
        }, True)
        return self._chains[name]

    def delete_chain(self, sess: Session, name: str) -> dict:
        if name not in self._chains:
            raise UIError(404, f"unknown chain: {name}")
        self.app._spells.pop(name, None)
        if hasattr(self.app, "_chains"):
            self.app._chains.pop(name, None)
        self._chains.pop(name, None)
        self._save_state_file()
        self._audit_admin_action(
            sess, "delete_chain", {"name": name}, True)
        return {"ok": True, "name": name}

    def list_chains(self) -> list[dict]:
        out = []
        for c in self._chains.values():
            out.append({
                "name": c["name"], "steps": c.get("steps", []),
                "description": c.get("description", ""),
                "scopes": c.get("scopes", []),
                "live": c["name"] in self.app._spells,
            })
        return out

    def delete_spell(self, sess: Session, name: str) -> dict:
        """Remove a UI-registered spell. Spells registered in code are
        read-only — refusing the delete is intentional, otherwise a
        compromised UI session could nuke production tools."""
        if name not in self.app._spells:
            raise UIError(404, f"unknown spell: {name}")
        if name not in self._dynamic_spells:
            raise UIError(
                403,
                "spell is registered in code and cannot be deleted "
                "from the UI",
            )
        del self.app._spells[name]
        info = self._dynamic_spells.pop(name)
        self._save_state_file()
        self._audit_admin_action(sess, "delete_spell", {
            "name": name, "source_hash": info["hash"][:16],
        }, True)
        return {"ok": True, "name": name}

    def update_scopes(self, sess: Session, name: str,
                       scopes: t.Iterable[str]) -> dict:
        """Replace the required scopes on a spell. Affects every future
        call (including ones from existing tokens — they won't satisfy
        a scope they weren't issued for)."""
        if name not in self.app._spells:
            raise UIError(404, f"unknown spell: {name}")
        spell = self.app._spells[name]
        old = list(spell.scopes or [])
        new = list(scopes)
        spell.scopes = new
        self._audit_admin_action(sess, "update_scopes", {
            "name": name, "from": old, "to": new,
        }, True)
        return {"name": name, "scopes": new}

    def issue_token(self, sess: Session, *, subject: str,
                     scopes: t.Iterable[str], ttl: int = 3600) -> dict:
        """Mint a bearer token signed with the SHABD secret. The token
        body, expiry and scopes are fully visible (HMAC, not encryption)
        — fine, since scoping is the whole point."""
        if not subject:
            raise UIError(400, "subject is required")
        ttl = int(ttl)
        if not (60 <= ttl <= 7 * 86400):
            raise UIError(400, "ttl must be between 60 s and 7 days")
        try:
            token = self.app.issue_token(
                subject, list(scopes), ttl=ttl)
        except Exception as e:
            raise UIError(400, str(e)) from None
        # Track jti → metadata so we can list + revoke later.
        try:
            import base64 as _b64
            payload_b64 = token.split(".")[0]
            pad = "=" * (-len(payload_b64) % 4)
            payload = json.loads(
                _b64.urlsafe_b64decode(payload_b64 + pad))
            jti = payload.get("jti", "")
            if jti:
                self._issued_tokens[jti] = {
                    "subject": subject,
                    "scopes": list(scopes),
                    "issued_at": time.time(),
                    "issued_by": sess.username,
                    "exp": payload.get("exp", 0),
                    "revoked": False,
                }
                self._save_state_file()
        except Exception:
            log.exception("could not record issued token")
        self._audit_admin_action(sess, "issue_token", {
            "subject": subject, "scopes": list(scopes), "ttl": ttl,
        }, True)
        return {
            "token": token, "subject": subject,
            "scopes": list(scopes), "expires_in_s": ttl,
        }

    def list_issued_tokens(self) -> list[dict]:
        out = []
        now = time.time()
        for jti, meta in self._issued_tokens.items():
            out.append({
                "jti": jti,
                "subject": meta.get("subject", ""),
                "scopes": meta.get("scopes", []),
                "issued_at": meta.get("issued_at", 0),
                "issued_by": meta.get("issued_by", ""),
                "exp": meta.get("exp", 0),
                "expired": meta.get("exp", 0) < now,
                "revoked": jti in self._revoked_jtis,
            })
        return sorted(out, key=lambda x: -x["issued_at"])

    def revoke_token(self, sess: Session, jti: str) -> dict:
        if not jti:
            raise UIError(400, "jti is required")
        meta = self._issued_tokens.get(jti)
        if not meta:
            raise UIError(404, "no issued token with that jti")
        if jti in self._revoked_jtis:
            return {"ok": True, "already_revoked": True}
        self._revoked_jtis.add(jti)
        meta["revoked"] = True
        self._save_state_file()
        self._audit_admin_action(sess, "revoke_token", {
            "jti": jti, "subject": meta.get("subject", ""),
        }, True)
        return {"ok": True, "jti": jti}

    # ---- v2.10: LLM config + named agents ----

    # ---- v2.13: external tool sources (MCP + other SHABD servers) ----

    def _proxy_spell_names(self, source: str) -> list[str]:
        """Names of local spells imported from a given source."""
        return [n for n, sp in self.app._spells.items()
                if f"source:{source}" in (sp.tags or [])]

    def connect_tool_source(self, sess: Session, *,
                             name: str, kind: str,
                             url: str, token: str = "",
                             transport: str = "http") -> dict:
        """Register an external tool source and import its tools as
        local proxy spells. kind = 'mcp' | 'shabd'."""
        name = (name or "").strip()
        if not name or not name.replace("_", "").replace(
                "-", "").isalnum():
            raise UIError(400, "source name must be alphanumeric / _ / -")
        kind = (kind or "").lower()
        if kind not in ("mcp", "shabd"):
            raise UIError(400, "kind must be 'mcp' or 'shabd'")
        if not url or not (url.startswith("http://")
                           or url.startswith("https://")):
            raise UIError(400, "url must be http(s)://")
        if name in self._tool_sources:
            # Reconnect: drop old imported spells first.
            self.disconnect_tool_source(sess, name, _audit=False)

        imported: list[str] = []
        error = ""
        try:
            if kind == "mcp":
                imported = self._import_mcp(name, url, token, transport)
            else:
                imported = self._import_shabd(name, url, token)
        except Exception as e:
            error = str(e)
            log.exception("tool source connect failed")

        self._tool_sources[name] = {
            "name": name, "type": kind, "url": url,
            "token": token, "transport": transport,
            "tools": imported, "connected": not error,
            "error": error,
        }
        self._save_state_file()
        self._audit_admin_action(sess, "connect_tool_source", {
            "name": name, "type": kind, "url": url,
            "tools_imported": len(imported), "ok": not error,
        }, not error)
        if error:
            raise UIError(502, f"connected with errors: {error}")
        return {"name": name, "type": kind,
                "tools": imported, "count": len(imported)}

    def _import_mcp(self, name: str, url: str, token: str,
                    transport: str) -> list[str]:
        from shabd import MCPClient
        client = MCPClient(
            name=name, transport=transport or "http",
            url=url, auth_token=token, prefix=True, timeout=15.0)
        client.connect()
        client.register_on(self.app)
        self._mcp_clients[name] = client
        # MCPClient prefixes with "<name>__"; also tag for our tracking.
        imported = []
        for sp_name, sp in self.app._spells.items():
            if sp_name.startswith(f"{name}__"):
                if f"source:{name}" not in (sp.tags or []):
                    sp.tags = list(sp.tags or []) + [f"source:{name}"]
                imported.append(sp_name)
        return imported

    def _import_shabd(self, name: str, url: str,
                      token: str) -> list[str]:
        """Pull another SHABD server's manifest and register each
        remote spell as a local proxy spell."""
        from shabd import Spell
        from shabd_client import SHABDClient
        client = SHABDClient(url, token=token or None, timeout=10.0)
        manifest = client.manifest()
        imported = []
        for entry in manifest.get("spells", []):
            remote_name = entry["name"]
            local_name = f"{name}__{remote_name}"
            if local_name in self.app._spells:
                continue
            schema = entry.get("input_schema") or {
                "type": "object", "properties": {}}
            desc = (entry.get("description") or remote_name).strip()

            def _make_proxy(c, rn):
                def _proxy(**kwargs):
                    return c.cast(rn, kwargs)
                _proxy.__name__ = rn
                _proxy.__doc__ = desc
                return _proxy

            spell_obj = Spell(
                name=local_name, func=_make_proxy(client, remote_name),
                description=desc, schema=schema, returns_schema={},
                is_async=False, is_streaming=False,
                wants_context=False, scopes=[], rate_limit=None,
                rate_window=60.0, cache_ttl=None, timeout=10.0,
                retries=1, tags=["shabd-proxy", f"source:{name}"],
                group="", idempotent=True)
            self.app._spells[local_name] = spell_obj
            imported.append(local_name)
        return imported

    def disconnect_tool_source(self, sess: Session, name: str,
                                *, _audit: bool = True) -> dict:
        if name not in self._tool_sources:
            raise UIError(404, f"unknown tool source: {name}")
        removed = self._proxy_spell_names(name)
        for sp in removed:
            self.app._spells.pop(sp, None)
        cli = self._mcp_clients.pop(name, None)
        if cli is not None:
            try:
                cli.close()
            except Exception:
                pass
        self._tool_sources.pop(name, None)
        self._save_state_file()
        if _audit:
            self._audit_admin_action(
                sess, "disconnect_tool_source",
                {"name": name, "removed": len(removed)}, True)
        return {"ok": True, "name": name, "removed": removed}

    def list_tool_sources(self) -> list[dict]:
        out = []
        for s in self._tool_sources.values():
            live = self._proxy_spell_names(s["name"])
            out.append({
                "name": s["name"], "type": s["type"],
                "url": s["url"], "transport": s.get("transport", ""),
                "connected": bool(live),
                "error": s.get("error", ""),
                "tools": live,
            })
        return out

    def reconnect_sources_on_boot(self) -> None:
        """Best-effort re-import of every saved source. Called once
        after construction; failures leave the source marked with an
        error but don't crash startup."""
        for name, s in list(self._tool_sources.items()):
            if self._proxy_spell_names(name):
                continue  # already present
            try:
                if s["type"] == "mcp":
                    self._import_mcp(
                        name, s["url"], s.get("token", ""),
                        s.get("transport", "http"))
                else:
                    self._import_shabd(
                        name, s["url"], s.get("token", ""))
                s["connected"] = True
                s["error"] = ""
            except Exception as e:
                s["connected"] = False
                s["error"] = str(e)
                log.warning("could not reconnect source %s: %s",
                            name, e)

    # ---- v2.12: UI-managed orchestrator intents ----

    def save_intent(self, sess: Session, *,
                    name: str, keywords: t.Iterable[str],
                    description: str = "",
                    route_to: str = "") -> dict:
        """Register an intent that routes matching queries to a saved
        agent. Stored in state.json so it survives restart."""
        if not name or not name.replace("_", "").replace(
                "-", "").isalnum():
            raise UIError(400, "intent name must be alphanumeric / _ / -")
        if route_to and route_to not in self._agents:
            raise UIError(404, f"no saved agent named '{route_to}'")
        intent = {
            "name": name,
            "keywords": [k.strip() for k in keywords if k.strip()],
            "description": description,
            "route_to": route_to,
            "created_by": sess.username,
            "ts": time.time(),
        }
        self._intents[name] = intent
        self._save_state_file()
        self._audit_admin_action(sess, "save_intent", {
            "name": name, "route_to": route_to,
            "keywords": intent["keywords"],
        }, True)
        return intent

    def delete_intent(self, sess: Session, name: str) -> dict:
        if name not in self._intents:
            raise UIError(404, f"unknown intent: {name}")
        self._intents.pop(name, None)
        self._save_state_file()
        self._audit_admin_action(
            sess, "delete_intent", {"name": name}, True)
        return {"ok": True, "name": name}

    def classify_query(self, query: str) -> dict:
        """Run the 5-stage classifier over the UI-registered intents."""
        query = (query or "").strip()
        if not query:
            raise UIError(400, "query is required")
        if not self._intents:
            return {
                "intent": None, "confidence": 0.0, "via": "no_intents",
                "answer": None,
                "message": ("No intents registered yet. Add one on "
                            "this page — give it keywords and pick "
                            "which saved agent should handle it."),
                "intents": [],
            }
        from shabd_orchestrator import (
            IntentSpec,
            SemanticIntentClassifier,
        )
        specs = [
            IntentSpec(
                name=i["name"],
                builder=lambda d: None,  # not used for classify-only
                keywords=list(i.get("keywords", [])),
                description=i.get("description", ""))
            for i in self._intents.values()
        ]
        classifier = SemanticIntentClassifier()
        intent, conf, via = classifier.classify(query, specs)
        routed = self._intents.get(intent, {})
        return {
            "intent": intent,
            "confidence": round(conf, 3),
            "via": via,
            "route_to": routed.get("route_to", ""),
            "intents": [
                {"name": i["name"], "keywords": i.get("keywords", []),
                 "description": i.get("description", ""),
                 "route_to": i.get("route_to", "")}
                for i in self._intents.values()
            ],
        }

    def ask_agent(self, *, agent_name: str, question: str,
                   subject: str = "api") -> dict:
        """Public-facing 'just ask' entry point. Used by the
        /query/<agent> HTTP endpoint. No session — token-authenticated
        at the HTTP layer. Returns a compact answer payload."""
        if agent_name not in self._agents:
            raise UIError(404, f"no agent named '{agent_name}'")
        question = (question or "").strip()
        if not question:
            raise UIError(400, "question is required")
        # Build a throwaway session-like object for run_agent's audit.
        pseudo = Session(sid="api", username=subject,
                          roles=["user"], access_token="api")
        run = self.run_agent(pseudo, name=agent_name, prompt=question)
        if not run.get("ok"):
            return {"ok": False, "error": run.get("error"),
                    "agent": agent_name}
        return {
            "ok": True,
            "agent": agent_name,
            "question": question,
            "answer": run.get("answer"),
            "steps": len(run.get("steps", [])),
        }

    def openapi_spec(self, *, base_url: str = "") -> dict:
        """Generate an OpenAPI 3.0 document covering every public
        endpoint: native SHABD wire format, per-spell invoke, per-agent
        /query, and the orchestrator /ask. Importable into Postman /
        Swagger UI."""
        paths: dict = {}
        bearer = [{"bearerAuth": []}]

        # Native fixed endpoints
        paths["/healthz"] = {"get": {
            "summary": "Liveness probe", "security": [],
            "responses": {"200": {"description": "ok"}}}}
        paths["/manifest"] = {"get": {
            "summary": "List all tools (MCP-compatible)",
            "security": [],
            "responses": {"200": {"description": "manifest"}}}}
        paths["/grimoire/verify"] = {"get": {
            "summary": "Verify the audit chain",
            "responses": {"200": {"description": "verify result"}}}}
        paths["/grimoire/head"] = {"get": {
            "summary": "Latest audit chain head",
            "responses": {"200": {"description": "head hash"}}}}

        # Per-spell invoke
        for name, spell in self.app._spells.items():
            schema = spell.schema or {
                "type": "object", "properties": {}}
            paths[f"/spells/{name}"] = {"post": {
                "summary": (spell.description or name)[:120],
                "tags": ["spells"],
                "security": bearer if spell.scopes else [],
                "requestBody": {
                    "required": True,
                    "content": {"application/json": {
                        "schema": schema}}},
                "responses": {
                    "200": {"description": "result"},
                    "401": {"description": "missing/invalid token"},
                    "403": {"description": "scope not satisfied"},
                },
            }}

        # Per-agent /query
        for name in self._agents:
            paths[f"/query/{name}"] = {"post": {
                "summary": f"Ask the '{name}' agent",
                "tags": ["agents"],
                "security": bearer,
                "requestBody": {
                    "required": True,
                    "content": {"application/json": {"schema": {
                        "type": "object",
                        "properties": {
                            "question": {"type": "string"}},
                        "required": ["question"]}}}},
                "responses": {"200": {"description": "answer"}},
            }}

        # Orchestrator /ask
        if self._intents:
            paths["/ask"] = {"post": {
                "summary": "Ask — orchestrator routes to the right agent",
                "tags": ["agents"],
                "security": bearer,
                "requestBody": {
                    "required": True,
                    "content": {"application/json": {"schema": {
                        "type": "object",
                        "properties": {
                            "question": {"type": "string"}},
                        "required": ["question"]}}}},
                "responses": {"200": {"description": "routed answer"}},
            }}

        return {
            "openapi": "3.0.3",
            "info": {
                "title": f"SHABD — {self.app.name}",
                "version": "1.0.0",
                "description": ("Auto-generated from the live SHABD "
                                "server. Tools, agents and the "
                                "orchestrator as HTTP endpoints."),
            },
            "servers": [{"url": base_url or "/"}],
            "components": {"securitySchemes": {"bearerAuth": {
                "type": "http", "scheme": "bearer"}}},
            "paths": paths,
        }

    # ---- v2.18: multi-agent flows (sequential / parallel) ----

    def save_flow(self, sess: Session, *,
                  name: str, kind: str,
                  agents: t.Iterable[str],
                  description: str = "",
                  synthesizer_system: str = "") -> dict:
        """Create/replace a named multi-agent orchestrator.

        kind="sequential": agents run one after another; each agent gets
        the original question plus the previous agent's result, so the
        output of step N feeds step N+1.

        kind="parallel": agents run independently (concurrently) on the
        same question; their answers are then synthesized into one final
        answer by the configured LLM."""
        name = (name or "").strip()
        if not name or not name.replace("_", "").replace(
                "-", "").isalnum():
            raise UIError(400, "flow name must be alphanumeric / _ / -")
        kind = (kind or "").lower()
        if kind not in ("sequential", "parallel"):
            raise UIError(400, "kind must be 'sequential' or 'parallel'")
        agents_list = [a.strip() for a in agents if a.strip()]
        if len(agents_list) < 2:
            raise UIError(400, "a flow needs at least 2 agents")
        for a in agents_list:
            if a not in self._agents:
                raise UIError(404, f"no saved agent named '{a}'")
        flow = {
            "name": name, "kind": kind,
            "agents": agents_list,
            "description": description,
            "synthesizer_system": synthesizer_system,
            "created_by": sess.username, "ts": time.time(),
        }
        self._flows[name] = flow
        self._save_state_file()
        self._audit_admin_action(sess, "save_flow", {
            "name": name, "kind": kind, "agents": agents_list,
        }, True)
        return flow

    def delete_flow(self, sess: Session, name: str) -> dict:
        if name not in self._flows:
            raise UIError(404, f"unknown flow: {name}")
        self._flows.pop(name, None)
        self._save_state_file()
        self._audit_admin_action(
            sess, "delete_flow", {"name": name}, True)
        return {"ok": True, "name": name}

    def list_flows(self) -> list[dict]:
        out = []
        for f in self._flows.values():
            out.append({
                "name": f["name"], "kind": f["kind"],
                "agents": f.get("agents", []),
                "description": f.get("description", ""),
                "live": all(a in self._agents
                            for a in f.get("agents", [])),
            })
        return out

    def _synthesize(self, flow: dict, question: str,
                    results: list[dict]) -> str:
        """Combine parallel agent results into one answer via the
        configured LLM. Falls back to a plain concatenation when no LLM
        is set."""
        from shabd_agent import MockBackend
        joined = "\n".join(
            f"- {r['agent']}: {r['answer']}" for r in results)
        backend = self.build_llm_backend()
        if isinstance(backend, MockBackend):
            return ("(no LLM configured — set one at /settings to get a "
                    "synthesized answer)\n" + joined)
        system = (flow.get("synthesizer_system")
                  or "You are a synthesis agent. Combine the results "
                     "from several agents into one clear, correct "
                     "answer to the user's question.")
        msgs = [
            {"role": "system", "content": system},
            {"role": "user",
             "content": (f"User question: {question}\n\n"
                         f"Results from agents:\n{joined}\n\n"
                         "Give the final answer.")},
        ]
        try:
            turn = backend.chat(msgs, [])
            return turn.text or joined
        except Exception as e:
            return f"(synthesis failed: {e})\n{joined}"

    def run_flow(self, sess: Session, *,
                 name: str, question: str) -> dict:
        """Execute a saved flow. Returns the final answer plus a
        per-agent trace."""
        if name not in self._flows:
            raise UIError(404, f"no flow named '{name}'")
        question = (question or "").strip()
        if not question:
            raise UIError(400, "question is required")
        flow = self._flows[name]
        agents = flow["agents"]

        if flow["kind"] == "sequential":
            prev = ""
            trace = []
            for i, ag in enumerate(agents):
                prompt = (question if i == 0 else
                          f"{question}\n\nPrevious step result: {prev}")
                run = self.run_agent(sess, name=ag, prompt=prompt)
                ans = (run.get("answer") if run.get("ok")
                       else f"(error: {run.get('error')})")
                trace.append({"agent": ag, "answer": ans})
                prev = ans
            return {"ok": True, "flow": name, "kind": "sequential",
                    "answer": prev, "trace": trace}

        # parallel
        import concurrent.futures as _cf

        def _run_one(ag: str) -> dict:
            run = self.run_agent(sess, name=ag, prompt=question)
            return {"agent": ag,
                    "answer": (run.get("answer") if run.get("ok")
                               else f"(error: {run.get('error')})")}

        with _cf.ThreadPoolExecutor(
                max_workers=min(8, len(agents))) as ex:
            results = list(ex.map(_run_one, agents))
        final = self._synthesize(flow, question, results)
        return {"ok": True, "flow": name, "kind": "parallel",
                "answer": final, "trace": results}

    # ---- v2.22: knowledge bases (document RAG, pure stdlib) ----

    @staticmethod
    def _kb_tokens(text: str) -> list[str]:
        import re as _re
        return _re.findall(r"[a-z0-9]+", (text or "").lower())

    @staticmethod
    def _kb_chunk(text: str, size: int = 500, overlap: int = 80) -> list[str]:
        """Split on paragraph boundaries, then pack into ~size-char
        chunks with a little overlap for context continuity."""
        text = (text or "").strip()
        if not text:
            return []
        paras = [p.strip() for p in text.split("\n\n") if p.strip()]
        chunks, cur = [], ""
        for p in paras:
            if len(cur) + len(p) + 2 <= size:
                cur = (cur + "\n\n" + p).strip()
            else:
                if cur:
                    chunks.append(cur)
                # a single huge paragraph → hard-split
                while len(p) > size:
                    chunks.append(p[:size])
                    p = p[max(0, size - overlap):]
                cur = p
        if cur:
            chunks.append(cur)
        return chunks

    def create_kb(self, sess: Session, *, name: str,
                  description: str = "") -> dict:
        name = (name or "").strip()
        if not name or not name.replace("_", "").replace(
                "-", "").isalnum():
            raise UIError(400, "KB name must be alphanumeric / _ / -")
        if name in self._kbs:
            raise UIError(409, f"knowledge base '{name}' already exists")
        self._kbs[name] = {
            "name": name, "description": description,
            "chunks": [], "exposed": False,
            "created_by": sess.username, "ts": time.time(),
        }
        self._save_state_file()
        self._audit_admin_action(
            sess, "create_kb", {"name": name}, True)
        return self._public_kb(self._kbs[name])

    def add_kb_text(self, sess: Session, *, name: str,
                    text: str, source: str = "pasted") -> dict:
        if name not in self._kbs:
            raise UIError(404, f"no knowledge base '{name}'")
        pieces = self._kb_chunk(text)
        if not pieces:
            raise UIError(400, "no text to add")
        kb = self._kbs[name]
        for p in pieces:
            kb["chunks"].append({"text": p, "source": source})
        self._save_state_file()
        # If the KB was already exposed, its spell keeps working (it
        # reads chunks live), so nothing else to do.
        self._audit_admin_action(sess, "add_kb_text", {
            "name": name, "source": source,
            "chunks_added": len(pieces),
        }, True)
        return {"ok": True, "name": name,
                "chunks_added": len(pieces),
                "total_chunks": len(kb["chunks"])}

    def query_kb(self, name: str, question: str,
                 top_k: int = 4) -> list[dict]:
        """TF-IDF cosine retrieval over the KB's chunks. Pure stdlib."""
        import math
        kb = self._kbs.get(name)
        if not kb or not kb["chunks"]:
            return []
        chunks = kb["chunks"]
        q_terms = self._kb_tokens(question)
        if not q_terms:
            return []
        # document frequency across chunks
        n = len(chunks)
        toks = [self._kb_tokens(c["text"]) for c in chunks]
        df: dict[str, int] = {}
        for tl in toks:
            for t_ in set(tl):
                df[t_] = df.get(t_, 0) + 1

        def idf(t_):
            return math.log((n + 1) / (df.get(t_, 0) + 1)) + 1

        def vec(tl):
            tf: dict[str, float] = {}
            for t_ in tl:
                tf[t_] = tf.get(t_, 0) + 1
            return {t_: (c / len(tl)) * idf(t_) for t_, c in tf.items()}

        qv = vec(q_terms)
        qn = math.sqrt(sum(v * v for v in qv.values())) or 1.0
        scored = []
        for i, tl in enumerate(toks):
            if not tl:
                continue
            dv = vec(tl)
            dot = sum(qv.get(t_, 0) * dv.get(t_, 0) for t_ in qv)
            dn = math.sqrt(sum(v * v for v in dv.values())) or 1.0
            score = dot / (qn * dn)
            if score > 0:
                scored.append((score, i))
        scored.sort(reverse=True)
        out = []
        for score, i in scored[:top_k]:
            out.append({"text": chunks[i]["text"],
                        "source": chunks[i]["source"],
                        "score": round(score, 4)})
        return out

    def expose_kb(self, sess: Session, name: str) -> dict:
        """Register a spell `kb_<name>(question)` that does RAG over
        the KB — retrieve top chunks and (if an LLM is set) compose an
        answer grounded in them. The spell then appears everywhere:
        Spells, /manifest, Agent Lab, Studio, /query, /ask."""
        if name not in self._kbs:
            raise UIError(404, f"no knowledge base '{name}'")
        spell_name = f"kb_{name}"
        ui_ref = self

        def _kb_answer(question: str) -> dict:
            hits = ui_ref.query_kb(name, question, top_k=4)
            if not hits:
                return {"answer": "No relevant information found in "
                                  f"the '{name}' knowledge base.",
                        "sources": []}
            context = "\n\n---\n\n".join(h["text"] for h in hits)
            from shabd_agent import MockBackend
            backend = ui_ref.build_llm_backend()
            if isinstance(backend, MockBackend):
                answer = ("(no LLM set — returning the most relevant "
                          "passages)\n\n" + context)
            else:
                msgs = [
                    {"role": "system",
                     "content": ("Answer the question using ONLY the "
                                 "context. If the answer is not in the "
                                 "context, say you don't know. Be "
                                 "concise.")},
                    {"role": "user",
                     "content": (f"Context:\n{context}\n\n"
                                 f"Question: {question}")},
                ]
                try:
                    answer = backend.chat(msgs, []).text or context
                except Exception as e:
                    answer = f"(LLM error: {e})\n\n{context}"
            return {"answer": answer,
                    "sources": [h["source"] for h in hits]}

        _kb_answer.__name__ = spell_name
        _kb_answer.__doc__ = (
            f"Answer questions from the '{name}' knowledge base "
            "(retrieval-augmented).")
        # Replace if it already exists (re-expose).
        self.app._spells.pop(spell_name, None)
        self.app.spell(
            name=spell_name,
            description=_kb_answer.__doc__,
            tags=["kb"], idempotent=True)(_kb_answer)
        self._kbs[name]["exposed"] = True
        self._save_state_file()
        self._audit_admin_action(
            sess, "expose_kb", {"name": name, "spell": spell_name},
            True)
        return {"ok": True, "name": name, "spell": spell_name}

    def unexpose_kb(self, sess: Session, name: str) -> dict:
        spell_name = f"kb_{name}"
        self.app._spells.pop(spell_name, None)
        if name in self._kbs:
            self._kbs[name]["exposed"] = False
            self._save_state_file()
        return {"ok": True, "name": name}

    def delete_kb(self, sess: Session, name: str) -> dict:
        if name not in self._kbs:
            raise UIError(404, f"no knowledge base '{name}'")
        self.app._spells.pop(f"kb_{name}", None)
        self._kbs.pop(name, None)
        self._save_state_file()
        self._audit_admin_action(
            sess, "delete_kb", {"name": name}, True)
        return {"ok": True, "name": name}

    def _public_kb(self, kb: dict) -> dict:
        return {
            "name": kb["name"],
            "description": kb.get("description", ""),
            "chunks": len(kb.get("chunks", [])),
            "sources": sorted({c["source"]
                               for c in kb.get("chunks", [])}),
            "exposed": kb.get("exposed", False),
            "spell": f"kb_{kb['name']}" if kb.get("exposed") else None,
        }

    def list_kbs(self) -> list[dict]:
        return [self._public_kb(k) for k in self._kbs.values()]

    # ---- v2.23: SQL Intelligence — external-service connector ----
    #
    # We DO NOT run the SQL/text-to-SQL engine. We connect to an
    # external service (which owns its own DB, schemas, models) through
    # its HTTP API, and expose it as a SHABD tool. Same pattern as the
    # Knowledge Base: configure once, "expose as tool", use everywhere.

    def create_sql_service(self, sess: Session, *,
                           name: str, base_url: str,
                           api_key: str = "",
                           auth_style: str = "bearer",
                           ask_path: str = "/query/ask",
                           query_field: str = "query",
                           answer_field: str = "answer",
                           description: str = "",
                           extra: dict | None = None) -> dict:
        name = (name or "").strip()
        if not name or not name.replace("_", "").replace(
                "-", "").isalnum():
            raise UIError(400, "service name must be alphanumeric / _ / -")
        if not base_url or not (base_url.startswith("http://")
                                or base_url.startswith("https://")):
            raise UIError(400, "base_url must be http(s)://")
        auth_style = (auth_style or "bearer").lower()
        if auth_style not in ("bearer", "x-api-key", "x-user-id", "none"):
            raise UIError(400, "bad auth_style")
        # Optional /query/ask body fields (from the service's OpenAPI):
        # top_k, collection, table, platform. Only non-empty ones are
        # stored and sent. thread_id is managed per chat session.
        extra = extra or {}
        clean_extra: dict = {}
        if extra.get("top_k"):
            try:
                clean_extra["top_k"] = int(extra["top_k"])
            except (ValueError, TypeError):
                pass
        for k in ("collection", "table", "platform"):
            v = (extra.get(k) or "").strip() if isinstance(
                extra.get(k), str) else extra.get(k)
            if v:
                clean_extra[k] = v
        self._sql_services[name] = {
            "name": name, "base_url": base_url.rstrip("/"),
            "api_key": api_key or "",
            "auth_style": auth_style,
            "ask_path": ask_path or "/query/ask",
            "query_field": query_field or "query",
            "answer_field": answer_field or "answer",
            "description": description,
            "extra": clean_extra,
            "exposed": False,
            "created_by": sess.username, "ts": time.time(),
        }
        self._save_state_file()
        self._audit_admin_action(sess, "create_sql_service", {
            "name": name, "base_url": base_url,
            "ask_path": ask_path, "has_key": bool(api_key),
        }, True)
        return self._public_sql(self._sql_services[name])

    def _sql_call(self, svc: dict, question: str, *,
                  thread_id: str | None = None,
                  timeout: float = 60.0) -> dict:
        """Server-side proxy to the external service's ask endpoint.
        Merges the service's optional fields (top_k / collection /
        table / platform) and an optional thread_id into the body."""
        url = svc["base_url"].rstrip("/") + svc.get(
            "ask_path", "/query/ask")
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json"}
        key = svc.get("api_key", "")
        style = svc.get("auth_style", "bearer")
        if key and style == "bearer":
            headers["Authorization"] = f"Bearer {key}"
        elif key and style == "x-api-key":
            headers["X-API-Key"] = key
        elif key and style == "x-user-id":
            headers["X-User-Id"] = key
        payload = {svc.get("query_field", "query"): question}
        payload.update(svc.get("extra") or {})
        if thread_id:
            payload["thread_id"] = thread_id
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            url, data=data, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                resp = json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            raise UIError(
                e.code if e.code in (400, 401, 403, 404, 429) else 502,
                f"external service error {e.code}: {detail}") from None
        except Exception as e:
            raise UIError(
                504, f"external service unreachable: {e}") from None
        af = svc.get("answer_field", "answer")
        answer = resp.get(af)
        if answer is None:
            # fall back to a few common shapes
            answer = (resp.get("answer") or resp.get("answer_md")
                      or resp.get("result") or json.dumps(resp)[:1000])
        out = {"answer": answer}
        sources = resp.get("sources")
        if isinstance(sources, list):
            out["sources"] = sources
        # Surface the thread id the service returned (for chat continuity)
        tid = resp.get("thread_id")
        if tid:
            out["thread_id"] = tid
        return out

    def test_sql_service(self, name: str, question: str,
                         thread_id: str | None = None) -> dict:
        svc = self._sql_services.get(name)
        if not svc:
            raise UIError(404, f"no service named '{name}'")
        return self._sql_call(svc, question, thread_id=thread_id)

    def expose_sql_service(self, sess: Session, name: str) -> dict:
        svc = self._sql_services.get(name)
        if not svc:
            raise UIError(404, f"no service named '{name}'")
        spell_name = f"sql_{name}"
        self._register_sql_spell(name)
        svc["exposed"] = True
        self._save_state_file()
        self._audit_admin_action(sess, "expose_sql_service", {
            "name": name, "spell": spell_name,
        }, True)
        return {"ok": True, "name": name, "spell": spell_name}

    def _register_sql_spell(self, name: str) -> None:
        spell_name = f"sql_{name}"
        ui_ref = self

        def _sql_query(question: str) -> dict:
            svc = ui_ref._sql_services.get(name)
            if not svc:
                return {"answer": f"service '{name}' is not configured"}
            try:
                return ui_ref._sql_call(svc, question)
            except UIError as e:
                return {"answer": f"(service error: {e.message})"}

        _sql_query.__name__ = spell_name
        _sql_query.__doc__ = (
            self._sql_services[name].get("description")
            or f"Ask the external '{name}' SQL Intelligence service.")
        self.app._spells.pop(spell_name, None)
        self.app.spell(name=spell_name, description=_sql_query.__doc__,
                       tags=["sql-intelligence"], idempotent=False)(
            _sql_query)

    def unexpose_sql_service(self, sess: Session, name: str) -> dict:
        self.app._spells.pop(f"sql_{name}", None)
        if name in self._sql_services:
            self._sql_services[name]["exposed"] = False
            self._save_state_file()
        return {"ok": True, "name": name}

    def delete_sql_service(self, sess: Session, name: str) -> dict:
        if name not in self._sql_services:
            raise UIError(404, f"no service named '{name}'")
        self.app._spells.pop(f"sql_{name}", None)
        self._sql_services.pop(name, None)
        self._save_state_file()
        self._audit_admin_action(
            sess, "delete_sql_service", {"name": name}, True)
        return {"ok": True, "name": name}

    def _public_sql(self, svc: dict) -> dict:
        return {
            "name": svc["name"], "base_url": svc["base_url"],
            "ask_path": svc.get("ask_path", "/query/ask"),
            "auth_style": svc.get("auth_style", "bearer"),
            "query_field": svc.get("query_field", "query"),
            "answer_field": svc.get("answer_field", "answer"),
            "description": svc.get("description", ""),
            "extra": svc.get("extra", {}),
            "has_key": bool(svc.get("api_key")),
            "exposed": svc.get("exposed", False),
            "spell": f"sql_{svc['name']}" if svc.get("exposed") else None,
        }

    def list_sql_services(self) -> list[dict]:
        return [self._public_sql(s) for s in self._sql_services.values()]

    # ---- v2.25: Nova — external RAG pipeline service, driven from UI ----
    #
    # The external service owns tenants, pipelines, ingestion and the
    # vector store. SHABD drives it through its HTTP API and exposes any
    # pipeline's query as a tool. No engine code lives here.

    def nova_get_config(self, redact: bool = True) -> dict:
        c = dict(self._nova)
        if redact and c.get("api_key"):
            c["api_key"] = "***"
        c["configured"] = bool(self._nova.get("base_url"))
        return c

    def nova_set_config(self, sess: Session, *, base_url: str,
                        api_key: str = "",
                        auth_style: str = "bearer") -> dict:
        if base_url and not (base_url.startswith("http://")
                             or base_url.startswith("https://")):
            raise UIError(400, "base_url must be http(s)://")
        auth_style = (auth_style or "bearer").lower()
        if auth_style not in ("bearer", "x-api-key", "none"):
            raise UIError(400, "bad auth_style")
        self._nova = {"base_url": base_url.rstrip("/"),
                      "api_key": api_key or "",
                      "auth_style": auth_style}
        self._save_state_file()
        self._audit_admin_action(sess, "nova_set_config", {
            "base_url": base_url, "has_key": bool(api_key)}, True)
        return self.nova_get_config()

    def _nova_headers(self) -> dict:
        h = {"Accept": "application/json"}
        key = self._nova.get("api_key", "")
        style = self._nova.get("auth_style", "bearer")
        if key and style == "bearer":
            h["Authorization"] = f"Bearer {key}"
        elif key and style == "x-api-key":
            h["X-API-Key"] = key
        return h

    def _nova_call(self, method: str, path: str, *,
                   body: dict | None = None,
                   params: dict | None = None,
                   multipart: dict | None = None,
                   timeout: float = 90.0) -> t.Any:
        base = self._nova.get("base_url", "")
        if not base:
            raise UIError(400, "Nova service is not configured")
        url = base.rstrip("/") + path
        if params:
            url += "?" + urllib.parse.urlencode(
                {k: v for k, v in params.items() if v not in (None, "")})
        headers = self._nova_headers()
        data = None
        if multipart is not None:
            boundary = "----shabd" + secrets.token_hex(8)
            data = self._build_multipart(boundary, multipart)
            headers["Content-Type"] = (
                f"multipart/form-data; boundary={boundary}")
        elif body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:400]
            code = e.code if e.code in (400, 401, 403, 404, 409, 422) \
                else 502
            raise UIError(
                code, f"Nova {e.code}: {detail}") from None
        except Exception as e:
            raise UIError(504, f"Nova unreachable: {e}") from None

    @staticmethod
    def _build_multipart(boundary: str, mp: dict) -> bytes:
        b = boundary.encode()
        out: list[bytes] = []
        for k, v in (mp.get("fields") or {}).items():
            out += [b"--" + b,
                    f'Content-Disposition: form-data; name="{k}"'.encode(),
                    b"", str(v).encode()]
        f = mp.get("file")
        if f:
            if len(f) == 3:
                fn, content, ctype = f
            else:
                fn, content = f
                ctype = "text/plain"
            if isinstance(content, str):
                content = content.encode()
            out += [b"--" + b,
                    (f'Content-Disposition: form-data; name="file"; '
                     f'filename="{fn}"').encode(),
                    f"Content-Type: {ctype}".encode(), b"", content]
        out += [b"--" + b + b"--", b""]
        return b"\r\n".join(out)

    # -- tenants --
    def nova_tenants(self) -> list[dict]:
        r = self._nova_call("GET", "/tenants",
                            params={"limit": 200})
        return r.get("items", r if isinstance(r, list) else [])

    def nova_create_tenant(self, sess: Session, *, name: str,
                           description: str = "") -> dict:
        r = self._nova_call("POST", "/tenants",
                            body={"name": name,
                                  "description": description or None})
        self._audit_admin_action(
            sess, "nova_create_tenant", {"name": name}, True)
        return r

    def nova_delete_tenant(self, sess: Session, tid: str) -> dict:
        self._nova_call("DELETE", f"/tenants/{tid}")
        self._audit_admin_action(
            sess, "nova_delete_tenant", {"tenant_id": tid}, True)
        return {"ok": True}

    # -- pipelines --
    def nova_pipelines(self, tenant_id: str = "") -> list[dict]:
        r = self._nova_call("GET", "/pipelines",
                            params={"tenant_id": tenant_id or None,
                                    "limit": 200})
        return r.get("items", r if isinstance(r, list) else [])

    def nova_create_pipeline(self, sess: Session, *,
                             tenant_id: str, name: str,
                             description: str = "",
                             config: dict | None = None,
                             collection_name: str = "",
                             table_name: str = "") -> dict:
        payload: dict = {"tenant_id": tenant_id, "name": name}
        if description:
            payload["description"] = description
        if config:
            payload["config"] = config
        if collection_name:
            payload["collection_name"] = collection_name
        if table_name:
            payload["table_name"] = table_name
        r = self._nova_call("POST", "/pipelines", body=payload)
        self._audit_admin_action(sess, "nova_create_pipeline", {
            "name": name, "tenant_id": tenant_id}, True)
        return r

    def nova_delete_pipeline(self, sess: Session, pid: str) -> dict:
        self._nova_call("DELETE", f"/pipelines/{pid}")
        # drop any exposed tool for this pipeline
        for spell, meta in list(self._nova_exposed.items()):
            if meta.get("pid") == pid:
                self.app._spells.pop(spell, None)
                self._nova_exposed.pop(spell, None)
        self._save_state_file()
        self._audit_admin_action(
            sess, "nova_delete_pipeline", {"pipeline_id": pid}, True)
        return {"ok": True}

    def nova_pipeline_stats(self, pid: str) -> dict:
        return self._nova_call("GET", f"/pipelines/{pid}/stats")

    def nova_ingest_text(self, sess: Session, *, pid: str,
                         filename: str, text: str) -> dict:
        if not text.strip():
            raise UIError(400, "no text to ingest")
        fn = filename or "document.txt"
        if not fn.lower().endswith((".txt", ".md")):
            fn += ".txt"
        r = self._nova_call(
            "POST", f"/pipelines/{pid}/ingest",
            multipart={"file": (fn, text)})
        self._audit_admin_action(sess, "nova_ingest", {
            "pipeline_id": pid, "filename": fn,
            "chunks": r.get("chunks_indexed")}, True)
        return r

    def nova_ingest_file(self, sess: Session, *, pid: str,
                         filename: str, content: bytes,
                         content_type: str = "application/octet-stream"
                         ) -> dict:
        """Forward an uploaded file (PDF / DOCX / text …) to the Nova
        pipeline's /ingest endpoint as multipart, unchanged."""
        if not content:
            raise UIError(400, "empty file")
        if len(content) > 32 * 1024 * 1024:
            raise UIError(413, "file too large (32 MiB max)")
        fn = filename or "document"
        r = self._nova_call(
            "POST", f"/pipelines/{pid}/ingest",
            multipart={"file": (fn, content, content_type)})
        self._audit_admin_action(sess, "nova_ingest_file", {
            "pipeline_id": pid, "filename": fn,
            "bytes": len(content),
            "chunks": r.get("chunks_indexed")}, True)
        return r

    def nova_query(self, pid: str, question: str, *,
                   top_k: int | None = None,
                   retriever: str = "") -> dict:
        body: dict = {"query": question}
        if top_k:
            body["top_k"] = int(top_k)
        if retriever:
            body["retriever"] = retriever
        return self._nova_call(
            "POST", f"/pipelines/{pid}/query", body=body)

    # -- expose a pipeline as a SHABD tool --
    def nova_expose_pipeline(self, sess: Session, *,
                             pid: str, name: str) -> dict:
        safe = "".join(c if (c.isalnum() or c in "_-") else "_"
                       for c in (name or pid))
        spell_name = f"nova_{safe}"
        self._nova_exposed[spell_name] = {"pid": pid, "name": name}
        self._register_nova_spell(spell_name)
        self._save_state_file()
        self._audit_admin_action(sess, "nova_expose", {
            "pipeline_id": pid, "spell": spell_name}, True)
        return {"ok": True, "spell": spell_name}

    def _register_nova_spell(self, spell_name: str) -> None:
        meta = self._nova_exposed.get(spell_name)
        if not meta:
            return
        pid = meta["pid"]
        ui_ref = self

        def _nova_answer(question: str) -> dict:
            try:
                r = ui_ref.nova_query(pid, question, top_k=5)
            except UIError as e:
                return {"answer": f"(Nova error: {e.message})",
                        "results": []}
            results = r.get("results", [])
            context = "\n\n---\n\n".join(
                x.get("text", "") for x in results)
            from shabd_agent import MockBackend
            backend = ui_ref.build_llm_backend()
            if isinstance(backend, MockBackend) or not context:
                answer = (context[:1500] if context
                          else "No relevant results.")
            else:
                msgs = [
                    {"role": "system",
                     "content": ("Answer using ONLY the context. If not "
                                 "present, say you don't know.")},
                    {"role": "user",
                     "content": f"Context:\n{context}\n\nQuestion: {question}"},
                ]
                try:
                    answer = backend.chat(msgs, []).text or context
                except Exception as e:
                    answer = f"(LLM error: {e})\n\n{context}"
            return {"answer": answer,
                    "sources": [x.get("filename") or x.get("id")
                                for x in results]}

        _nova_answer.__name__ = spell_name
        _nova_answer.__doc__ = (
            f"Query the Nova RAG pipeline '{meta.get('name', pid)}'.")
        self.app._spells.pop(spell_name, None)
        self.app.spell(name=spell_name, description=_nova_answer.__doc__,
                       tags=["nova"], idempotent=True)(_nova_answer)

    def nova_unexpose(self, sess: Session, spell_name: str) -> dict:
        self.app._spells.pop(spell_name, None)
        self._nova_exposed.pop(spell_name, None)
        self._save_state_file()
        return {"ok": True}

    def nova_exposed_map(self) -> dict:
        """pipeline_id -> spell_name, for the UI to show exposed state."""
        return {m["pid"]: s for s, m in self._nova_exposed.items()}

    # ---- v2.19: visual-studio chatbots ----

    def save_chatbot(self, sess: Session, *,
                     name: str, system: str = "",
                     greeting: str = "",
                     tools: t.Iterable[str] = (),
                     agents: t.Iterable[str] = (),
                     graph: t.Any = None,
                     force_tools: bool = False) -> dict:
        """Save a chatbot built in the visual studio. A chatbot is a
        system prompt + a toolset (selected spells + the tools of any
        selected agents) + an optional saved node graph for re-editing."""
        name = (name or "").strip()
        if not name or not name.replace("_", "").replace(
                "-", "").isalnum():
            raise UIError(400, "chatbot name must be alphanumeric / _ / -")
        tools_list = [t for t in tools if t in self.app._spells]
        agents_list = [a for a in agents if a in self._agents]
        bot = {
            "name": name,
            "system": system or "You are a helpful assistant.",
            "greeting": greeting or "Hi! How can I help?",
            "tools": tools_list,
            "agents": agents_list,
            "force_tools": bool(force_tools),
            "graph": graph or {},
            "created_by": sess.username,
            "ts": time.time(),
        }
        self._chatbots[name] = bot
        self._save_state_file()
        self._audit_admin_action(sess, "save_chatbot", {
            "name": name, "tools": tools_list, "agents": agents_list,
        }, True)
        return self._public_bot(bot)

    def _public_bot(self, bot: dict) -> dict:
        return {
            "name": bot["name"], "system": bot.get("system", ""),
            "greeting": bot.get("greeting", ""),
            "tools": bot.get("tools", []),
            "agents": bot.get("agents", []),
            "force_tools": bot.get("force_tools", False),
            "graph": bot.get("graph", {}),
        }

    def get_chatbot(self, name: str) -> dict | None:
        b = self._chatbots.get(name)
        return self._public_bot(b) if b else None

    def list_chatbots(self) -> list[dict]:
        return [
            {"name": b["name"],
             "tools": b.get("tools", []),
             "agents": b.get("agents", []),
             "greeting": b.get("greeting", "")}
            for b in self._chatbots.values()
        ]

    def delete_chatbot(self, sess: Session, name: str) -> dict:
        if name not in self._chatbots:
            raise UIError(404, f"unknown chatbot: {name}")
        self._chatbots.pop(name, None)
        self._save_state_file()
        self._audit_admin_action(
            sess, "delete_chatbot", {"name": name}, True)
        return {"ok": True, "name": name}

    def _chatbot_toolset(self, bot: dict) -> list[str]:
        """A chatbot's effective tools = its own spells plus every tool
        of each selected agent (deduped, only those still registered)."""
        tools = list(bot.get("tools", []))
        for ag in bot.get("agents", []):
            a = self._agents.get(ag)
            if a:
                tools += a.get("tools", [])
        seen, out = set(), []
        for t_ in tools:
            if t_ in self.app._spells and t_ not in seen:
                seen.add(t_)
                out.append(t_)
        return out

    def run_chatbot(self, sess: Session, *,
                    name: str, message: str,
                    history: list | None = None) -> dict:
        """Run one chat turn against a saved chatbot. Each turn is
        recorded to the Grimoire chain (tamper-evident conversation)."""
        if name not in self._chatbots:
            raise UIError(404, f"no chatbot named '{name}'")
        message = (message or "").strip()
        if not message:
            raise UIError(400, "message is required")
        bot = self._chatbots[name]
        tools = self._chatbot_toolset(bot)
        # Light context: prepend the last few turns into the prompt.
        prompt = message
        if history:
            recent = history[-6:]
            convo = "\n".join(
                f"{h.get('role', 'user')}: {h.get('text', '')}"
                for h in recent if isinstance(h, dict))
            if convo:
                prompt = (f"Conversation so far:\n{convo}\n\n"
                          f"User: {message}")
        run = self.run_agent(
            sess, name=None, prompt=prompt,
            system=bot.get("system"), tools=tools,
            force_tools=bot.get("force_tools", False))
        reply = (run.get("answer") if run.get("ok")
                 else f"(error: {run.get('error')})")
        # Audit the chat turn itself.
        try:
            self.app.grimoire.append(
                trace_id=secrets.token_hex(8),
                spell=f"__chat:{name}",
                subject=sess.username,
                args={"message": message[:500]},
                result={"reply": (reply or "")[:500]},
                ok=bool(run.get("ok")))
        except Exception:
            log.exception("chat audit append failed")
        return {"ok": bool(run.get("ok")), "bot": name,
                "reply": reply,
                "tools_used": [
                    {"name": tc["name"], "arguments": tc["arguments"]}
                    for s in run.get("steps", [])
                    for tc in s.get("tool_calls", [])
                ]}

    def ask_orchestrator(self, *, question: str,
                          subject: str = "api") -> dict:
        """Public 'just ask' over the orchestrator. Classifies the
        question, routes to the winning intent's agent, runs it, and
        returns the answer plus which intent/agent handled it. Used by
        the public POST /ask endpoint. No session — token-authed at the
        HTTP layer."""
        question = (question or "").strip()
        if not question:
            raise UIError(400, "question is required")
        decision = self.classify_query(question)
        intent = decision.get("intent")
        if not intent:
            return {"ok": False,
                    "answer": None,
                    "reason": decision.get("via", "no_match"),
                    "message": decision.get(
                        "message", "no intent matched")}
        route_to = decision.get("route_to")
        if not route_to:
            return {"ok": False, "intent": intent,
                    "confidence": decision.get("confidence"),
                    "via": decision.get("via"),
                    "answer": None,
                    "message": (f"matched intent '{intent}' but it has "
                                "no agent assigned")}
        pseudo = Session(sid="api", username=subject,
                          roles=["user"], access_token="api")
        run = self.run_agent(pseudo, name=route_to, prompt=question)
        if not run.get("ok"):
            return {"ok": False, "intent": intent,
                    "agent": route_to,
                    "error": run.get("error")}
        return {
            "ok": True,
            "question": question,
            "intent": intent,
            "via": decision.get("via"),
            "confidence": decision.get("confidence"),
            "agent": route_to,
            "answer": run.get("answer"),
            "steps": len(run.get("steps", [])),
        }

    def route_and_run(self, sess: Session, *, query: str) -> dict:
        """Classify a query, then actually RUN the saved agent the
        winning intent points at. This is the full orchestrator flow:
        one user sentence → routed → answered."""
        decision = self.classify_query(query)
        intent = decision.get("intent")
        route_to = decision.get("route_to")
        if not intent:
            return {**decision, "ran": False}
        if not route_to:
            return {**decision, "ran": False,
                    "message": (f"Intent '{intent}' matched but has no "
                                "agent assigned. Edit it and pick one.")}
        run = self.run_agent(sess, name=route_to, prompt=query)
        return {**decision, "ran": True, "result": run}

    def get_llm_config(self, redact: bool = True) -> dict:
        cfg = dict(self._llm_config)
        if redact and cfg.get("api_key"):
            cfg["api_key"] = "***"
        return cfg

    def set_llm_config(self, sess: Session, *,
                        backend: str, base_url: str,
                        model: str, api_key: str = "") -> dict:
        backend = (backend or "none").lower()
        if backend not in ("none", "ollama", "openai", "anthropic"):
            raise UIError(400, "backend must be none/ollama/openai/anthropic")
        if backend != "none":
            if not base_url or not (
                    base_url.startswith("http://")
                    or base_url.startswith("https://")):
                raise UIError(400, "base_url must be an http(s) URL")
            if not model:
                raise UIError(400, "model is required")
        self._llm_config = {
            "backend": backend, "base_url": base_url,
            "model": model, "api_key": api_key or "",
        }
        self._save_state_file()
        self._audit_admin_action(sess, "set_llm_config", {
            "backend": backend, "model": model,
            "has_api_key": bool(api_key),
        }, True)
        return self.get_llm_config(redact=True)

    def build_llm_backend(self, *, force_tools: bool = False):
        """Construct an actual LLMBackend from current config. Returns
        a MockBackend if backend is 'none' or shabd_agent missing."""
        from shabd_agent import MockBackend, OpenAICompatBackend
        cfg = self._llm_config
        be = (cfg.get("backend") or "none").lower()
        if be == "none":
            return MockBackend(["(no LLM backend configured — set one "
                                "at /settings to get real answers)"])
        if be in ("ollama", "openai"):
            base = cfg.get("base_url", "")
            if be == "ollama" and not base.rstrip("/").endswith("/v1"):
                base = base.rstrip("/") + "/v1"
            return OpenAICompatBackend(
                base_url=base, model=cfg.get("model", ""),
                api_key=cfg.get("api_key", ""),
                force_tools=force_tools)
        if be == "anthropic":
            from shabd_agent import AnthropicBackend
            return AnthropicBackend(
                api_key=cfg.get("api_key", ""),
                model=cfg.get("model", "claude-opus-4-8"),
                base_url=cfg.get("base_url") or "https://api.anthropic.com")
        return MockBackend(["unknown backend"])

    def save_agent(self, sess: Session, *,
                    name: str, system: str,
                    tools: t.Iterable[str],
                    description: str = "",
                    max_steps: int = 6,
                    force_tools: bool = False) -> dict:
        if not name or not name.replace("_", "").replace(
                "-", "").isalnum():
            raise UIError(400, "agent name must be alphanumeric / _ / -")
        if len(name) > 64:
            raise UIError(400, "name too long")
        tools_list = list(tools or [])
        for tool_name in tools_list:
            if tool_name not in self.app._spells:
                raise UIError(404, f"unknown spell: {tool_name}")
        max_steps = max(1, min(int(max_steps), 50))
        agent = {
            "name": name, "system": system or "",
            "tools": tools_list,
            "description": description,
            "max_steps": max_steps,
            "force_tools": bool(force_tools),
            "created_by": sess.username,
            "ts": time.time(),
        }
        self._agents[name] = agent
        self._save_state_file()
        self._audit_admin_action(sess, "save_agent", {
            "name": name, "tools": tools_list,
            "max_steps": max_steps,
        }, True)
        return agent

    def delete_agent(self, sess: Session, name: str) -> dict:
        if name not in self._agents:
            raise UIError(404, f"unknown agent: {name}")
        self._agents.pop(name, None)
        self._save_state_file()
        self._audit_admin_action(
            sess, "delete_agent", {"name": name}, True)
        return {"ok": True, "name": name}

    def run_agent(self, sess: Session, *,
                   name: str | None,
                   prompt: str,
                   system: str | None = None,
                   tools: t.Iterable[str] | None = None,
                   max_steps: int = 6,
                   force_tools: bool = False) -> dict:
        """Run a saved agent OR an ad-hoc one. Just give a name (saved)
        or system+tools (ad-hoc). Returns the full step trace."""
        from shabd_agent import Agent
        if name and name in self._agents:
            a = self._agents[name]
            system = a["system"]
            tools = a["tools"]
            max_steps = int(a.get("max_steps", max_steps))
            force_tools = bool(a.get("force_tools", force_tools))
        tools_list = list(tools or [])
        for tool_name in tools_list:
            if tool_name not in self.app._spells:
                raise UIError(404, f"unknown spell: {tool_name}")
        from shabd_agent import ToolRegistry
        llm = self.build_llm_backend(force_tools=force_tools)
        registry = ToolRegistry()
        registry.bind_shabd(self.app)
        # If the user picked a subset, prune the registry.
        if tools_list:
            registry._tools = {
                n: registry._tools[n] for n in tools_list
                if n in registry._tools}
        ag = Agent(
            llm=llm,
            system=system or "You are a helpful assistant.",
            tools=registry,
            max_steps=int(max_steps),
        )
        try:
            result = ag.run(prompt)
        except Exception as e:
            return {"ok": False, "error": str(e),
                    "type": type(e).__name__}
        return {
            "ok": True,
            "answer": result.answer,
            "stopped": result.stopped_reason,
            "steps": [
                {"n": s.n, "text": s.assistant.text,
                 "tool_calls": [{"name": tc.name,
                                   "arguments": tc.arguments}
                                 for tc in s.assistant.tool_calls],
                 "tool_results": s.tool_results,
                 "elapsed_ms": round(s.elapsed_ms, 2)}
                for s in result.steps
            ],
        }

    def update_spell_source(self, sess: Session, *,
                             name: str, source: str,
                             description: str = "",
                             scopes: t.Iterable[str] = (),
                             tags: t.Iterable[str] = ()) -> dict:
        """Re-register an existing UI-built spell with new source. The
        old version's source hash is captured in the audit page."""
        if name not in self.app._spells:
            raise UIError(404, f"unknown spell: {name}")
        if name not in self._dynamic_spells:
            raise UIError(
                403, "this spell is declared in code; edit the file")
        old = self._dynamic_spells[name]
        fn = _compile_spell_source(name, source)
        # Remove the old spell THEN re-register so app.spell()'s
        # "already registered" guard doesn't fire.
        del self.app._spells[name]
        try:
            self.app.spell(
                name=name,
                description=description or (fn.__doc__ or "").strip(),
                scopes=list(scopes), tags=list(tags),
            )(fn)
        except Exception as e:
            # Restore the old registration if the new one failed.
            old_fn = _compile_spell_source(name, old["source"])
            self.app.spell(
                name=name,
                description=old.get("description", ""),
                scopes=old.get("scopes", []),
                tags=old.get("tags", []),
            )(old_fn)
            raise UIError(400, str(e)) from None
        import hashlib as _h
        new_hash = _h.sha256(source.encode()).hexdigest()
        # Versioning: keep the last 10 versions of each spell's source.
        prev_versions = list(old.get("versions") or [])
        prev_versions.append({
            "source": old.get("source", ""),
            "hash": old.get("hash", ""),
            "by": old.get("updated_by") or old.get("created_by", ""),
            "ts": old.get("updated_at") or old.get("created_at", 0),
        })
        prev_versions = prev_versions[-10:]
        self._dynamic_spells[name] = {
            **old, "source": source, "hash": new_hash,
            "description": description or old.get("description", ""),
            "scopes": list(scopes), "tags": list(tags),
            "updated_by": sess.username,
            "updated_at": time.time(),
            "versions": prev_versions,
        }
        self._save_state_file()
        self._audit_admin_action(sess, "update_spell_source", {
            "name": name,
            "old_hash": old["hash"][:16],
            "new_hash": new_hash[:16],
            "versions_stored": len(prev_versions),
        }, True)
        return {"name": name, "source_hash": new_hash[:16],
                "versions": len(prev_versions)}

    def list_spell_versions(self, name: str) -> list[dict]:
        info = self._dynamic_spells.get(name)
        if not info:
            return []
        out = [{
            "source": info.get("source", ""),
            "hash": info.get("hash", ""),
            "by": info.get("updated_by") or info.get("created_by", ""),
            "ts": info.get("updated_at") or info.get("created_at", 0),
            "current": True,
        }]
        for v in reversed(info.get("versions") or []):
            out.append({**v, "current": False})
        return out

    def rollback_spell(self, sess: Session, *,
                        name: str, target_hash: str) -> dict:
        info = self._dynamic_spells.get(name)
        if not info:
            raise UIError(404, f"unknown UI-built spell: {name}")
        versions = info.get("versions") or []
        for v in versions:
            if v.get("hash") == target_hash:
                return self.update_spell_source(
                    sess, name=name, source=v["source"],
                    description=info.get("description", ""),
                    scopes=info.get("scopes", []),
                    tags=info.get("tags", []))
        raise UIError(404, "no version with that hash found")

    def get_spell_source(self, name: str) -> dict | None:
        """Return source code for a UI-built spell. None for code-built."""
        return self._dynamic_spells.get(name)

    SHARE_PREFIX = "shabd-spell-v1:"

    def export_project_zip(self, sess: Session) -> bytes:
        """Bundle every UI-built spell + saved agents + LLM config +
        audit chain into a single zip. The recipient can extract it
        anywhere and run `python -m shabd_ui --spells my_spells.py`
        to reproduce the same server state."""
        import io
        import zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w",
                              zipfile.ZIP_DEFLATED) as zf:
            # 1) Every UI-built spell becomes a function in my_spells.py
            lines = [
                '"""SHABD spells exported from the UI.',
                "",
                "Run with:",
                "    python -m shabd_ui --spells my_spells.py",
                '"""',
                "",
                "def register_spells(app):",
                "    # SHABD calls this if it finds the function.",
                "    pass",
                "",
            ]
            for name, info in self._dynamic_spells.items():
                spell = self.app._spells.get(name)
                scopes = list(spell.scopes or []) if spell else []
                tags = list(spell.tags or []) if spell else []
                lines.append(
                    f"@app.spell(name={name!r}, "
                    f"scopes={scopes!r}, tags={tags!r})")
                lines.append(info.get("source", "").rstrip("\n"))
                lines.append("")
            zf.writestr("my_spells.py", "\n".join(lines))

            # 2) Saved agents
            zf.writestr("agents.json",
                         json.dumps(self._agents, indent=2,
                                     default=str))

            # 3) LLM config (api_key redacted by default!)
            cfg = dict(self._llm_config)
            cfg["api_key"] = "***SET-VIA-ENV-OR-RE-ENTER***"
            zf.writestr("llm_config.json",
                         json.dumps(cfg, indent=2))

            # 4) Audit chain (if persisted)
            try:
                audit_path = (
                    getattr(self.app, "_grimoire_log", None)
                    and self.app._grimoire_log.path)
                if audit_path and os.path.exists(audit_path):
                    with open(audit_path, "rb") as f:
                        zf.writestr("audit.jsonl", f.read())
            except Exception:
                log.exception("audit export failed")

            # 5) State sidecar
            if self._state_path and os.path.exists(self._state_path):
                with open(self._state_path, "rb") as f:
                    zf.writestr("state.json", f.read())

            # 6) A run.sh recipe
            zf.writestr("run.sh", (
                "#!/usr/bin/env bash\n"
                "# Reproduce this SHABD project on any machine.\n"
                "# Requires Python 3.10+ and the shabd package.\n"
                "set -e\n"
                "pip install shabd\n"
                "python -m shabd_ui \\\n"
                "    --spells my_spells.py \\\n"
                "    --audit  audit.jsonl  \\\n"
                "    --port   8080\n"
            ))

            # 7) README
            zf.writestr("README.txt", (
                "SHABD project export\n"
                "====================\n\n"
                "Contents:\n"
                "  my_spells.py     — every UI-built spell, re-decorated\n"
                "  agents.json      — saved named agents\n"
                "  llm_config.json  — backend / model (api key redacted)\n"
                "  audit.jsonl      — your Grimoire audit chain\n"
                "  state.json       — sidecar state\n"
                "  run.sh           — one-line launch script\n\n"
                "How to use:\n"
                "  bash run.sh\n"
                "  # then open http://localhost:8080/\n\n"
                "Notes:\n"
                "  * The api_key is intentionally redacted. Re-enter "
                "it at /settings after launch.\n"
                "  * If audit.jsonl is present, the original user "
                "store, spells, and agents are replayed on boot.\n"
            ))
        self._audit_admin_action(sess, "export_project", {
            "spells": len(self._dynamic_spells),
            "agents": len(self._agents),
        }, True)
        return buf.getvalue()

    def import_project_zip(self, sess: Session,
                            data: bytes,
                            overwrite: bool = False) -> dict:
        """Reverse of export — register every UI-built spell from
        the my_spells.py inside the zip, load agents.json + state.json
        (audit chain isn't merged — that would break tamper-evidence;
        users must replace the audit log manually if they want)."""
        import io
        import zipfile
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            raise UIError(400, "not a valid zip file") from None
        members = set(zf.namelist())
        created = []
        # Saved agents
        if "agents.json" in members:
            try:
                ags = json.loads(zf.read("agents.json"))
                for n, a in (ags or {}).items():
                    a["name"] = n
                    self._agents[n] = a
            except Exception:
                log.exception("agents.json malformed")
        # State sidecar
        if "state.json" in members:
            try:
                d = json.loads(zf.read("state.json"))
                ds = d.get("dynamic_spells") or {}
                for n, meta in ds.items():
                    if n in self.app._spells and not overwrite:
                        continue
                    if n in self.app._spells:
                        del self.app._spells[n]
                    fn = _compile_spell_source(
                        n, meta.get("source", ""))
                    self.app.spell(
                        name=n,
                        description=meta.get("description", ""),
                        scopes=meta.get("scopes", []),
                        tags=meta.get("tags", []),
                    )(fn)
                    self._dynamic_spells[n] = meta
                    created.append(n)
            except Exception as e:
                log.exception("state.json import failed: %s", e)
        self._save_state_file()
        self._audit_admin_action(sess, "import_project", {
            "imported_spells": created,
            "agents_loaded": list(self._agents.keys()),
        }, True)
        return {"ok": True, "imported": created,
                "agents": list(self._agents.keys())}

    def share_spell(self, sess: Session, name: str) -> dict:
        """Bundle a UI-built spell into a short string the recipient
        can paste into their own Spell Builder."""
        if name not in self.app._spells:
            raise UIError(404, f"unknown spell: {name}")
        src = self._dynamic_spells.get(name)
        if not src:
            raise UIError(
                403, "only UI-built spells can be shared "
                     "(code-built spells live in your repo)")
        spell = self.app._spells[name]
        bundle = {
            "v": 1,
            "name": name,
            "description": (spell.description or "")[:300],
            "scopes": list(spell.scopes or []),
            "tags": list(spell.tags or []),
            "source": src.get("source", ""),
            "from": sess.username,
        }
        import base64
        token = base64.urlsafe_b64encode(
            json.dumps(bundle).encode()).decode().rstrip("=")
        return {"share": self.SHARE_PREFIX + token,
                "name": name}

    def import_shared_spell(self, sess: Session, *,
                             share: str,
                             overwrite: bool = False) -> dict:
        """Decode a share string and register the spell locally."""
        share = (share or "").strip()
        if not share.startswith(self.SHARE_PREFIX):
            raise UIError(400,
                          f"share string must start with "
                          f"{self.SHARE_PREFIX!r}")
        import base64
        body = share[len(self.SHARE_PREFIX):]
        pad = "=" * (-len(body) % 4)
        try:
            decoded = base64.urlsafe_b64decode(body + pad)
            bundle = json.loads(decoded)
        except Exception:
            raise UIError(400, "invalid share string") from None
        name = bundle.get("name")
        source = bundle.get("source")
        if not name or not source:
            raise UIError(400, "incomplete share bundle")
        if name in self.app._spells:
            if not overwrite:
                raise UIError(
                    409,
                    f"spell '{name}' already exists locally; "
                    "pass overwrite=true to replace")
            if name not in self._dynamic_spells:
                raise UIError(
                    403,
                    f"a code-built spell '{name}' exists; "
                    "rename your import to avoid clobber")
            self.update_spell_source(
                sess, name=name, source=source,
                description=bundle.get("description", ""),
                scopes=bundle.get("scopes", []),
                tags=bundle.get("tags", []))
            return {"ok": True, "imported": name, "mode": "updated"}
        self.create_spell(
            sess, name=name, source=source,
            description=bundle.get("description", ""),
            scopes=bundle.get("scopes", []),
            tags=bundle.get("tags", []))
        return {"ok": True, "imported": name, "mode": "created",
                "from": bundle.get("from", "")}

    def suggest_spell_source(self, sess: Session, *,
                              requirement: str,
                              name_hint: str = "") -> dict:
        """Ask the configured LLM to draft a SHABD spell from a
        natural-language requirement. Falls back to a deterministic
        skeleton if no LLM is configured."""
        cfg = self._llm_config
        backend = (cfg.get("backend") or "none").lower()
        requirement = (requirement or "").strip()
        if not requirement:
            raise UIError(400, "requirement is required")
        # Always available: a deterministic skeleton that the user can
        # refine. This is what you get when backend == "none".
        fallback_name = (name_hint or "my_spell").strip()
        fallback = (
            f"def {fallback_name}(...) -> dict:\n"
            f"    '''{requirement}'''\n"
            f"    # TODO: implement.\n"
            f"    return {{'ok': True}}\n"
        )
        if backend == "none":
            return {
                "source": fallback,
                "via": "fallback",
                "warning": ("No LLM configured. Set one in /settings "
                            "for real suggestions."),
            }
        # Build the prompt and call the LLM directly (without the
        # full Agent loop — we want one short generation, not a
        # tool-use round trip).
        prompt = (
            "Write ONE Python function that fits inside SHABD's "
            "Spell Builder sandbox.\n"
            "Rules:\n"
            "1. Use ONLY these stdlib modules and primitive types "
            "(int, float, str, bool, list, dict, tuple, set). "
            "No file I/O, no os, no subprocess.\n"
            "2. Type-annotate every parameter and the return "
            "type. SHABD generates the JSON schema from these "
            "annotations.\n"
            "3. The function name should be a valid Python "
            "identifier (snake_case).\n"
            "4. Include a one-line docstring describing what it "
            "does.\n"
            "5. Reply with ONLY the function source code, no "
            "markdown fences, no commentary.\n\n"
            f"Requirement: {requirement}\n"
        )
        if name_hint:
            prompt += f"Suggested name: {name_hint}\n"
        try:
            be = self.build_llm_backend()
            from shabd_agent import ToolRegistry
            turn = be.chat(
                messages=[
                    {"role": "system",
                     "content": "You are a Python coding assistant "
                                "for the SHABD framework."},
                    {"role": "user", "content": prompt},
                ],
                tools=list(ToolRegistry()._tools.values()),
            )
        except Exception as e:
            self._audit_admin_action(sess, "suggest_spell",
                                      {"backend": backend,
                                       "ok": False,
                                       "error": str(e)}, False)
            return {
                "source": fallback,
                "via": "fallback",
                "warning": f"LLM call failed: {e}",
            }
        text = (turn.text or "").strip()
        # Strip markdown fences if the LLM ignored rule #5.
        if text.startswith("```"):
            lines = text.split("\n")
            # drop first fence
            lines = lines[1:]
            # drop last fence
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        self._audit_admin_action(sess, "suggest_spell", {
            "backend": backend,
            "model": cfg.get("model", ""),
            "requirement": requirement[:140],
        }, True)
        return {"source": text, "via": backend}

    # ---- remote-SHABD / MCP client proxy ----

    def client_call(self, sess: Session, *, base_url: str,
                     token: str | None, action: str, **kw) -> dict:
        """Server-side bridge to any other SHABD HTTP server (the same
        wire format MCP clients use). Doing the call from the UI server
        avoids browser CORS and keeps the bearer token off the
        front-end. NOT meant for arbitrary URLs — we still validate
        scheme and length."""
        try:
            from shabd_client import (
                SHABDClient,
                SHABDClientError,
            )
        except ImportError:
            raise UIError(500, "shabd_client module unavailable") from None
        if not base_url or len(base_url) > 1024:
            raise UIError(400, "base_url is required (max 1 KiB)")
        if not (base_url.startswith("http://")
                or base_url.startswith("https://")):
            raise UIError(400, "base_url must be http(s)://")
        client = SHABDClient(
            base_url, token=token or None,
            timeout=float(kw.get("timeout", 10.0)), retries=1)
        try:
            if action == "ping":
                return {"ok": True, "health": client.health()}
            if action == "manifest":
                return {"ok": True, "manifest": client.manifest()}
            if action == "invoke":
                spell = (kw.get("spell") or "").strip()
                if not spell:
                    raise UIError(400, "spell name required")
                args = kw.get("args") or {}
                ik = kw.get("idempotency_key") or None
                res = client.cast(
                    spell, args, idempotency_key=ik)
                return {"ok": True, "result": res}
            if action == "grimoire":
                return {
                    "ok": True,
                    "verify": client.grimoire_verify(),
                    "head": client.grimoire_head(),
                }
        except SHABDClientError as e:
            return {
                "ok": False, "error": e.message, "code": e.code,
                "hint": e.hint, "did_you_mean": e.did_you_mean,
                "example": e.example, "status": e.status,
            }
        except Exception as e:
            return {"ok": False, "error": str(e),
                    "type": type(e).__name__}
        raise UIError(400, f"unknown client action: {action}")

    # ---- HTTP server bootstrap ----

    def serve(self) -> None:
        srv = _build_server(self)
        log.info("SHABD UI listening on %s:%s", self.bind, self.port)
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            srv.shutdown()


# ============================================================================
# HTTP server
# ============================================================================

def _build_server(ui: UIServer) -> socketserver.TCPServer:
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    handler_cls = _make_handler(ui)
    srv = socketserver.ThreadingTCPServer((ui.bind, ui.port), handler_cls)
    srv.daemon_threads = True
    return srv


def _make_handler(ui: UIServer):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args, **kwargs):
            pass

        # ---- helpers ----

        def _read_body(self) -> bytes:
            length = int(self.headers.get("content-length") or 0)
            return self.rfile.read(length) if length else b""

        def _origin(self) -> str:
            host = self.headers.get("host", f"{ui.bind}:{ui.port}")
            scheme = ("https" if ui.force_secure_cookies else "http")
            return f"{scheme}://{host}"

        def _session(self) -> Session | None:
            cookie = SimpleCookie(self.headers.get("cookie", ""))
            sid_morsel = cookie.get("shabd_sid")
            if not sid_morsel:
                return None
            return ui.sessions.get(sid_morsel.value)

        def _require_session(self) -> Session:
            sess = self._session()
            if not sess:
                raise UIError(401, "not signed in")
            return sess

        def _require_admin(self) -> Session:
            s = self._require_session()
            if not s.is_admin():
                raise UIError(403, "admin role required")
            return s

        def _require_super(self) -> Session:
            s = self._require_session()
            if not s.is_superuser():
                raise UIError(403, "superuser role required")
            return s

        def _check_csrf(self, sess: Session) -> None:
            tok = self.headers.get("x-csrf", "")
            if not tok or tok != sess.csrf:
                raise UIError(403, "csrf token mismatch")

        def _send(self, status: int, body: bytes,
                  content_type: str = "text/html; charset=utf-8",
                  cookies: list[tuple[str, str, dict]] = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "same-origin")
            if getattr(ui, "security_headers", True):
                if ui.hsts:
                    self.send_header("Strict-Transport-Security", ui.hsts)
                if ui.csp:
                    self.send_header("Content-Security-Policy", ui.csp)
                if ui.permissions_policy:
                    self.send_header("Permissions-Policy", ui.permissions_policy)
            for name, value, attrs in (cookies or []):
                ck = SimpleCookie()
                ck[name] = value
                m = ck[name]
                for k, v in attrs.items():
                    if v is True:
                        m[k] = True
                    elif v is False:
                        pass
                    else:
                        m[k] = v
                self.send_header("Set-Cookie", ck.output(header="").strip())
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, status: int, payload: t.Any) -> None:
            self._send(status, json.dumps(payload, default=str).encode(),
                       "application/json")

        def _send_html(self, status: int, page: str, body: str,
                       sess: Session, page_script: str = "") -> None:
            badge = (f"{_html.escape(sess.username)} · "
                     + " · ".join(_html.escape(r) for r in sess.roles))
            admin_nav = ""
            if sess.is_admin():
                admin_nav = (
                    '<a href="/users" data-page="users">'
                    '<span class="icon">👥</span> Users</a>'
                    '<a href="/tokens" data-page="tokens">'
                    '<span class="icon">🔑</span> Issue Token</a>'
                    '<a href="/scopes" data-page="scopes">'
                    '<span class="icon">🛡️</span> Scopes</a>'
                )
            super_nav = ""
            if sess.is_superuser():
                super_nav = (
                    '<a href="/builder" data-page="builder">'
                    '<span class="icon">🛠️</span> Spell Builder</a>'
                )
            html_page = (
                _APP_HTML
                .replace("__BODY__", body)
                .replace("__USER_BADGE__", badge)
                .replace("__ADMIN_NAV__", admin_nav)
                .replace("__SUPER_NAV__", super_nav)
                .replace("__CSRF__", sess.csrf)
                .replace("__PAGE_SCRIPT__", page_script)
            )
            self._send(status, html_page.encode())

        def _redirect(self, to: str,
                       cookies: list[tuple[str, str, dict]] = None) -> None:
            self.send_response(303)
            self.send_header("Location", to)
            self.send_header("Content-Length", "0")
            for name, value, attrs in (cookies or []):
                ck = SimpleCookie()
                ck[name] = value
                m = ck[name]
                for k, v in attrs.items():
                    if v is True:
                        m[k] = True
                    elif v is not False:
                        m[k] = v
                self.send_header("Set-Cookie", ck.output(header="").strip())
            self.send_header("Connection", "close")
            self.end_headers()

        # ---- routing ----

        def do_GET(self):  # noqa: N802
            try:
                self._route("GET")
            except UIError as e:
                self._handle_ui_error(e)
            except Exception:
                log.exception("unhandled UI error")
                self._send(500, b"internal error", "text/plain")

        def do_POST(self):  # noqa: N802
            try:
                self._route("POST")
            except UIError as e:
                self._handle_ui_error(e)
            except Exception:
                log.exception("unhandled UI error")
                self._send(500, b"internal error", "text/plain")

        def _handle_ui_error(self, e: UIError) -> None:
            if self.path.startswith("/api/"):
                self._send_json(e.status, {"error": e.message})
            elif e.status == 401:
                self._redirect("/login")
            else:
                self._send(e.status,
                           f"<h1>{e.status}</h1><p>{_html.escape(e.message)}</p>"
                           .encode())

        def _route(self, method: str) -> None:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            qs = urllib.parse.parse_qs(parsed.query)
            # Public routes
            if path == "/login":
                if method == "GET":
                    return self._page_login(error=None)
                if method == "POST":
                    return self._handle_login()
            if path == "/register":
                if method == "GET":
                    return self._page_register(error=None)
                if method == "POST":
                    return self._handle_register()
            if path == "/logout":
                return self._handle_logout()
            if path == "/healthz":
                return self._send_json(200, {"ok": True})

            # ----- Native SHABD endpoints (the MCP-compatible wire
            # format). These are token-authenticated (NOT session) so
            # external clients, LLMs and other SHABD servers can use
            # them directly. This is what you share to integrate. -----
            if path == "/manifest" and method == "GET":
                return self._native_manifest()
            if path == "/openapi.json" and method == "GET":
                origin = self._origin()
                return self._send_json(
                    200, ui.openapi_spec(base_url=origin))
            if path == "/grimoire/verify" and method == "GET":
                return self._send_json(200, ui.app.grimoire.verify())
            if path == "/grimoire/head" and method == "GET":
                return self._send_json(
                    200, {"head": ui.app.grimoire.head()})
            if path.startswith("/spells/") and method == "POST":
                spell = urllib.parse.unquote(path[len("/spells/"):])
                return self._native_invoke(spell)
            # The "ask anywhere" endpoint: POST /query/<agent> with
            # {"question": "..."} → {"answer": "..."}. Token-auth.
            if path.startswith("/query/") and method == "POST":
                agent_name = urllib.parse.unquote(
                    path[len("/query/"):])
                return self._native_query(agent_name)
            # The orchestrator "just ask" endpoint: POST /ask with
            # {"question": "..."} → orchestrator picks the agent,
            # runs it, returns the answer. Token-auth.
            if path == "/ask" and method == "POST":
                return self._native_ask()
            # Run a saved multi-agent flow: POST /flow/<name>.
            if path.startswith("/flow/") and method == "POST":
                flow_name = urllib.parse.unquote(
                    path[len("/flow/"):])
                return self._native_flow(flow_name)

            # Everything else needs a session
            sess = self._require_session()

            # API endpoints (JSON)
            if path == "/api/dashboard" and method == "GET":
                return self._api_dashboard(sess)
            if path == "/api/spells" and method == "GET":
                return self._api_spells(sess)
            if path.startswith("/api/invoke/") and method == "POST":
                self._check_csrf(sess)
                spell = urllib.parse.unquote(path[len("/api/invoke/"):])
                return self._api_invoke(sess, spell)
            if path == "/api/grimoire" and method == "GET":
                return self._api_grimoire(sess, qs)
            if path == "/api/audit" and method == "GET":
                return self._api_audit(sess, qs)
            if path == "/api/users" and method == "GET":
                self._require_admin()
                return self._api_users(sess)
            if path == "/api/agent/run" and method == "POST":
                self._check_csrf(sess)
                return self._api_agent_run(sess)
            if path == "/api/orchestrator/classify" and method == "POST":
                self._check_csrf(sess)
                return self._api_orch_classify(sess)
            if path == "/api/orchestrator/run" and method == "POST":
                self._check_csrf(sess)
                return self._api_orch_run(sess)
            if path == "/api/chains" and method == "GET":
                return self._api_chains_list(sess)
            if path == "/api/chains/create" and method == "POST":
                self._require_super()
                self._check_csrf(sess)
                return self._api_chains_create(sess)
            if (path.startswith("/api/chains/")
                    and path.endswith("/delete")
                    and method == "POST"):
                self._require_super()
                self._check_csrf(sess)
                nm = urllib.parse.unquote(
                    path[len("/api/chains/"):-len("/delete")])
                return self._api_chains_delete(sess, nm)
            if path == "/api/sources" and method == "GET":
                return self._api_sources_list(sess)
            if path == "/api/sources/connect" and method == "POST":
                self._require_admin()
                self._check_csrf(sess)
                return self._api_sources_connect(sess)
            if (path.startswith("/api/sources/")
                    and path.endswith("/disconnect")
                    and method == "POST"):
                self._require_admin()
                self._check_csrf(sess)
                nm = urllib.parse.unquote(
                    path[len("/api/sources/"):-len("/disconnect")])
                return self._api_sources_disconnect(sess, nm)
            if path == "/api/flows" and method == "GET":
                return self._api_flows_list(sess)
            if path == "/api/flows/save" and method == "POST":
                self._require_admin()
                self._check_csrf(sess)
                return self._api_flows_save(sess)
            if path == "/api/flows/run" and method == "POST":
                self._check_csrf(sess)
                return self._api_flows_run(sess)
            if (path.startswith("/api/flows/")
                    and path.endswith("/delete")
                    and method == "POST"):
                self._require_admin()
                self._check_csrf(sess)
                nm = urllib.parse.unquote(
                    path[len("/api/flows/"):-len("/delete")])
                return self._api_flows_delete(sess, nm)
            # v2.22 — knowledge bases (RAG)
            if path == "/api/kb" and method == "GET":
                return self._api_kb_list(sess)
            if path == "/api/kb/create" and method == "POST":
                self._require_admin()
                self._check_csrf(sess)
                return self._api_kb_create(sess)
            if path == "/api/kb/add" and method == "POST":
                self._require_admin()
                self._check_csrf(sess)
                return self._api_kb_add(sess)
            if path == "/api/kb/query" and method == "POST":
                self._check_csrf(sess)
                return self._api_kb_query(sess)
            if (path.startswith("/api/kb/") and method == "POST"
                    and path.endswith("/expose")):
                self._require_admin()
                self._check_csrf(sess)
                nm = urllib.parse.unquote(
                    path[len("/api/kb/"):-len("/expose")])
                return self._api_kb_expose(sess, nm)
            if (path.startswith("/api/kb/") and method == "POST"
                    and path.endswith("/delete")):
                self._require_admin()
                self._check_csrf(sess)
                nm = urllib.parse.unquote(
                    path[len("/api/kb/"):-len("/delete")])
                return self._send_json(200, ui.delete_kb(sess, nm))
            # v2.23 — SQL Intelligence connectors
            if path == "/api/sqlsvc" and method == "GET":
                return self._send_json(
                    200, {"services": ui.list_sql_services()})
            if path == "/api/sqlsvc/create" and method == "POST":
                self._require_admin()
                self._check_csrf(sess)
                return self._api_sqlsvc_create(sess)
            if path == "/api/sqlsvc/test" and method == "POST":
                self._check_csrf(sess)
                return self._api_sqlsvc_test(sess)
            if (path.startswith("/api/sqlsvc/") and method == "POST"
                    and path.endswith("/expose")):
                self._require_admin()
                self._check_csrf(sess)
                nm = urllib.parse.unquote(
                    path[len("/api/sqlsvc/"):-len("/expose")])
                return self._send_json(
                    200, ui.expose_sql_service(sess, nm))
            if (path.startswith("/api/sqlsvc/") and method == "POST"
                    and path.endswith("/delete")):
                self._require_admin()
                self._check_csrf(sess)
                nm = urllib.parse.unquote(
                    path[len("/api/sqlsvc/"):-len("/delete")])
                return self._send_json(
                    200, ui.delete_sql_service(sess, nm))
            # v2.25 — Nova RAG pipeline service
            if path == "/api/nova/config" and method == "GET":
                return self._send_json(200, ui.nova_get_config())
            if path == "/api/nova/config" and method == "POST":
                self._require_admin()
                self._check_csrf(sess)
                return self._api_nova_config(sess)
            if path == "/api/nova/tenants" and method == "GET":
                return self._api_nova_tenants(sess)
            if path == "/api/nova/tenants" and method == "POST":
                self._require_admin()
                self._check_csrf(sess)
                return self._api_nova_tenant_create(sess)
            if path == "/api/nova/pipelines" and method == "GET":
                return self._api_nova_pipelines(sess, qs)
            if path == "/api/nova/pipelines" and method == "POST":
                self._require_admin()
                self._check_csrf(sess)
                return self._api_nova_pipeline_create(sess)
            if path == "/api/nova/ingest" and method == "POST":
                self._require_admin()
                self._check_csrf(sess)
                return self._api_nova_ingest(sess)
            if path == "/api/nova/query" and method == "POST":
                self._check_csrf(sess)
                return self._api_nova_query(sess)
            if path == "/api/nova/expose" and method == "POST":
                self._require_admin()
                self._check_csrf(sess)
                return self._api_nova_expose(sess)
            if (path.startswith("/api/nova/pipelines/")
                    and path.endswith("/delete") and method == "POST"):
                self._require_admin()
                self._check_csrf(sess)
                pid = urllib.parse.unquote(
                    path[len("/api/nova/pipelines/"):-len("/delete")])
                return self._send_json(
                    200, ui.nova_delete_pipeline(sess, pid))
            if path == "/api/intents" and method == "GET":
                return self._api_intents_list(sess)
            if path == "/api/intents/save" and method == "POST":
                self._require_admin()
                self._check_csrf(sess)
                return self._api_intents_save(sess)
            if (path.startswith("/api/intents/")
                    and path.endswith("/delete")
                    and method == "POST"):
                self._require_admin()
                self._check_csrf(sess)
                name = urllib.parse.unquote(
                    path[len("/api/intents/"):-len("/delete")])
                return self._api_intents_delete(sess, name)
            if path == "/api/notary/publish" and method == "POST":
                self._require_admin()
                self._check_csrf(sess)
                return self._api_notary_publish(sess)
            if path == "/api/notary/state" and method == "GET":
                return self._api_notary_state(sess)
            if path == "/api/settings" and method == "GET":
                return self._api_settings(sess)

            # -- v2.9: builder / tokens / scopes / client proxy --
            if path == "/api/spells/create" and method == "POST":
                self._require_super()
                self._check_csrf(sess)
                return self._api_spells_create(sess)
            if (path.startswith("/api/spells/")
                    and path.endswith("/delete") and method == "POST"):
                self._require_super()
                self._check_csrf(sess)
                name = urllib.parse.unquote(
                    path[len("/api/spells/"):-len("/delete")])
                return self._api_spells_delete(sess, name)
            if path == "/api/scopes" and method == "GET":
                self._require_admin()
                return self._api_scopes(sess)
            if (path.startswith("/api/scopes/")
                    and method == "POST"):
                self._require_admin()
                self._check_csrf(sess)
                name = urllib.parse.unquote(
                    path[len("/api/scopes/"):])
                return self._api_scopes_update(sess, name)
            if path == "/api/tokens/issue" and method == "POST":
                self._require_admin()
                self._check_csrf(sess)
                return self._api_tokens_issue(sess)
            if path == "/api/tokens" and method == "GET":
                self._require_admin()
                return self._api_tokens_list(sess)
            if (path.startswith("/api/tokens/")
                    and path.endswith("/revoke")
                    and method == "POST"):
                self._require_admin()
                self._check_csrf(sess)
                jti = urllib.parse.unquote(
                    path[len("/api/tokens/"):-len("/revoke")])
                return self._api_tokens_revoke(sess, jti)
            if path == "/api/client/call" and method == "POST":
                self._check_csrf(sess)
                return self._api_client_call(sess)

            # v2.9.1: built-in user mgmt
            if path == "/api/users/create" and method == "POST":
                self._require_admin()
                self._check_csrf(sess)
                return self._api_users_create(sess)
            if (path.startswith("/api/users/") and method == "POST"
                    and path.endswith("/roles")):
                self._require_admin()
                self._check_csrf(sess)
                uname = urllib.parse.unquote(
                    path[len("/api/users/"):-len("/roles")])
                return self._api_users_set_roles(sess, uname)
            if (path.startswith("/api/users/") and method == "POST"
                    and path.endswith("/password")):
                self._require_admin()
                self._check_csrf(sess)
                uname = urllib.parse.unquote(
                    path[len("/api/users/"):-len("/password")])
                return self._api_users_reset_pw(sess, uname)
            if (path.startswith("/api/users/") and method == "POST"
                    and path.endswith("/delete")):
                self._require_super()
                self._check_csrf(sess)
                uname = urllib.parse.unquote(
                    path[len("/api/users/"):-len("/delete")])
                return self._api_users_delete(sess, uname)

            # v2.10: agent registry, spell editor, LLM config
            if path == "/api/agents" and method == "GET":
                return self._api_agents_list(sess)
            if path == "/api/agents/save" and method == "POST":
                self._require_admin()
                self._check_csrf(sess)
                return self._api_agents_save(sess)
            if (path.startswith("/api/agents/")
                    and path.endswith("/delete")
                    and method == "POST"):
                self._require_admin()
                self._check_csrf(sess)
                name = urllib.parse.unquote(
                    path[len("/api/agents/"):-len("/delete")])
                return self._api_agents_delete(sess, name)
            if path == "/api/agents/run" and method == "POST":
                self._check_csrf(sess)
                return self._api_agents_run(sess)
            if (path.startswith("/api/spells/")
                    and path.endswith("/source")
                    and method == "GET"):
                self._require_super()
                name = urllib.parse.unquote(
                    path[len("/api/spells/"):-len("/source")])
                return self._api_spell_source_get(sess, name)
            if (path.startswith("/api/spells/")
                    and path.endswith("/update")
                    and method == "POST"):
                self._require_super()
                self._check_csrf(sess)
                name = urllib.parse.unquote(
                    path[len("/api/spells/"):-len("/update")])
                return self._api_spell_update(sess, name)
            if (path.startswith("/api/spells/")
                    and path.endswith("/versions")
                    and method == "GET"):
                self._require_super()
                name = urllib.parse.unquote(
                    path[len("/api/spells/"):-len("/versions")])
                return self._api_spell_versions(sess, name)
            if (path.startswith("/api/spells/")
                    and path.endswith("/rollback")
                    and method == "POST"):
                self._require_super()
                self._check_csrf(sess)
                name = urllib.parse.unquote(
                    path[len("/api/spells/"):-len("/rollback")])
                return self._api_spell_rollback(sess, name)
            if path == "/api/llm_config" and method == "GET":
                return self._api_llm_config_get(sess)
            if path == "/api/llm_config" and method == "POST":
                self._require_admin()
                self._check_csrf(sess)
                return self._api_llm_config_set(sess)
            if path == "/api/spells/suggest" and method == "POST":
                self._require_super()
                self._check_csrf(sess)
                return self._api_spells_suggest(sess)
            if (path.startswith("/api/spells/")
                    and path.endswith("/share")
                    and method == "GET"):
                name = urllib.parse.unquote(
                    path[len("/api/spells/"):-len("/share")])
                return self._api_spells_share(sess, name)
            if path == "/api/spells/import" and method == "POST":
                self._require_super()
                self._check_csrf(sess)
                return self._api_spells_import(sess)
            if path == "/api/project/export" and method == "GET":
                self._require_super()
                return self._api_project_export(sess)
            if path == "/api/project/import" and method == "POST":
                self._require_super()
                self._check_csrf(sess)
                return self._api_project_import(sess)

            # HTML pages
            if path == "/" or path == "/dashboard":
                return self._page_dashboard(sess)
            if path == "/spells":
                return self._page_spells(sess)
            if path == "/grimoire":
                return self._page_grimoire(sess)
            if path == "/audit":
                return self._page_audit(sess)
            if path == "/agent":
                return self._page_agent(sess)
            if path == "/orchestrator":
                return self._page_orch(sess)
            if path == "/notary":
                return self._page_notary(sess)
            if path == "/users":
                self._require_admin()
                return self._page_users(sess)
            if path == "/builder":
                self._require_super()
                return self._page_builder(sess)
            if path == "/tokens":
                self._require_admin()
                return self._page_tokens(sess)
            if path == "/scopes":
                self._require_admin()
                return self._page_scopes(sess)
            if path == "/client":
                return self._page_client(sess)
            if path == "/sources":
                return self._page_sources(sess)
            if path == "/chains":
                return self._page_chains(sess)
            if path == "/knowledge":
                return self._page_knowledge(sess)
            if path == "/sql-intelligence":
                return self._page_sqlsvc(sess)
            if path == "/nova":
                return self._page_nova(sess)
            if path == "/api-docs":
                return self._page_apidocs(sess)
            if path == "/settings":
                return self._page_settings(sess)

            self._send(404, b"<h1>404</h1>")

        # =========================================================
        # Auth handlers
        # =========================================================

        def _page_login(self, error: str | None) -> None:
            err_html = (f'<div class="err">{_html.escape(error)}</div>'
                        if error else "")
            if ui.keycloak:
                auth_line = f"Authenticated by Keycloak · {_html.escape(ui.keycloak.realm)}"
            elif ui.users is not None:
                auth_line = "Built-in account"
            else:
                auth_line = "Bootstrap mode"
            page = (_LOGIN_HTML
                    .replace("__ERROR__", err_html)
                    .replace("__AUTH_LINE__", auth_line))
            self._send(200, page.encode())

        def _page_register(self, error: str | None) -> None:
            if ui.users is None:
                self._redirect("/login")
                return
            if (not ui.allow_self_register
                    and not ui.users.is_first_run()):
                self._redirect("/login")
                return
            first = ui.users.is_first_run()
            badge = ("First run — you will be created as the "
                     "<strong>superuser</strong>." if first else
                     "Create a new account")
            err_html = (f'<div class="err">{_html.escape(error)}</div>'
                        if error else "")
            page = (_REGISTER_HTML
                    .replace("__ERROR__", err_html)
                    .replace("__BADGE__", badge))
            self._send(200, page.encode())

        def _handle_register(self) -> None:
            body = self._read_body()
            form = urllib.parse.parse_qs(body.decode("utf-8"))
            user = form.get("username", [""])[0].strip()
            pw = form.get("password", [""])[0]
            pw2 = form.get("password2", [""])[0]
            if pw != pw2:
                return self._page_register(error="Passwords do not match")
            try:
                sess = ui._register(user, pw)
            except UIError as e:
                return self._page_register(error=e.message)
            attrs = {"Path": "/", "HttpOnly": True,
                     "SameSite": "Lax", "Max-Age": "28800"}
            if ui.force_secure_cookies:
                attrs["Secure"] = True
            self._redirect(
                "/", cookies=[("shabd_sid", sess.sid, attrs)])

        def _handle_login(self) -> None:
            body = self._read_body()
            form = urllib.parse.parse_qs(body.decode("utf-8"))
            user = form.get("username", [""])[0].strip()
            pw = form.get("password", [""])[0]
            if not user or not pw:
                return self._page_login(error="Missing credentials")
            try:
                sess = ui._login(user, pw)
            except UIError as e:
                return self._page_login(error=e.message)
            attrs = {"Path": "/", "HttpOnly": True, "SameSite": "Lax",
                     "Max-Age": "28800"}
            if ui.force_secure_cookies:
                attrs["Secure"] = True
            self._redirect("/", cookies=[("shabd_sid", sess.sid, attrs)])

        def _handle_logout(self) -> None:
            sess = self._session()
            if sess:
                ui.sessions.drop(sess.sid)
            self._redirect("/login", cookies=[
                ("shabd_sid", "", {"Path": "/", "Max-Age": "0"}),
            ])

        # =========================================================
        # JSON API
        # =========================================================

        def _api_dashboard(self, sess: Session) -> None:
            g = ui.app.grimoire
            verify = g.verify()
            recent = list(ui.app._recent_calls)[-10:]
            self._send_json(200, {
                "spells": len(ui.app._spells),
                "audit_pages": verify.get("pages", 0),
                "chain_ok": verify.get("ok"),
                "head": verify.get("head", "")[:16],
                "recent_calls": [
                    {"trace_id": r.trace_id[:8],
                     "spell": r.spell,
                     "subject": r.subject,
                     "ok": r.ok,
                     "elapsed_ms": round(r.elapsed_ms, 2),
                     "ts": r.ts}
                    for r in recent[::-1]
                ],
                "session": {"user": sess.username, "roles": sess.roles},
            })

        def _api_spells(self, sess: Session) -> None:
            out = []
            for name, spell in ui.app._spells.items():
                out.append({
                    "name": name,
                    "description": spell.description,
                    "schema": spell.schema,
                    "tags": list(spell.tags or []),
                    "scopes": list(spell.scopes or []),
                })
            self._send_json(200, {"spells": out})

        # ----- Native SHABD wire-format handlers (token auth) -----

        def _native_manifest(self) -> None:
            """The MCP-compatible manifest. Same shape SHABD's own HTTP
            server returns — OpenAI / Anthropic / Gemini tool format."""
            self._send_json(200, ui.app.manifest())

        def _native_invoke(self, spell_name: str) -> None:
            """POST /spells/<name> with `Authorization: Bearer <token>`.
            Goes through SHABD's real auth + scope + audit pipeline —
            identical to what an external client or another SHABD
            server would hit."""
            try:
                body = json.loads(self._read_body() or b"{}")
            except json.JSONDecodeError:
                self._send_json(400, {"error": {
                    "code": "bad_json",
                    "message": "request body must be JSON"}})
                return
            auth = self.headers.get("authorization", "")
            token = (auth.removeprefix("Bearer ").strip()
                     if auth else None)
            if spell_name not in ui.app._spells:
                self._send_json(404, {"error": {
                    "code": "spell_not_found",
                    "message": f"no such spell: {spell_name}",
                    "hint": "GET /manifest for the full list"}})
                return
            try:
                result = ui.app.invoke(spell_name, body, token=token)
                self._send_json(200, {"result": result})
            except Exception as e:
                code = type(e).__name__
                status = 500
                if code in ("AuthError",):
                    status = 401
                elif code in ("ForbiddenError",):
                    status = 403
                elif code in ("SpellNotFoundError",):
                    status = 404
                elif code in ("ValidationError", "SchemaError"):
                    status = 400
                self._send_json(status, {"error": {
                    "code": code,
                    "message": str(e),
                    "hint": getattr(e, "hint", None),
                }})

        def _native_query(self, agent_name: str) -> None:
            """POST /query/<agent>  body: {"question": "..."}.
            Optionally token-authenticated; the token's subject is
            recorded in the audit trail. This is the endpoint you embed
            anywhere — a website, a script, another service."""
            try:
                body = json.loads(self._read_body() or b"{}")
            except json.JSONDecodeError:
                self._send_json(400, {"error": {
                    "code": "bad_json",
                    "message": "body must be JSON"}})
                return
            # Identify the caller from the token if present (for audit).
            subject = "api"
            auth = self.headers.get("authorization", "")
            if auth:
                tok = auth.removeprefix("Bearer ").strip()
                try:
                    payload = ui.app.tokens.verify(tok)
                    subject = payload.get("sub", "api")
                except Exception:
                    self._send_json(401, {"error": {
                        "code": "unauthorized",
                        "message": "invalid token"}})
                    return
            question = body.get("question") or body.get("query") or ""
            try:
                res = ui.ask_agent(
                    agent_name=agent_name, question=question,
                    subject=subject)
                self._send_json(200, res)
            except UIError as e:
                self._send_json(e.status, {"error": {
                    "code": "agent_error", "message": e.message}})

        def _native_ask(self) -> None:
            """POST /ask  body: {"question": "..."}. The orchestrator
            picks which agent should answer, runs it, returns the
            answer. This is the endpoint where the caller does NOT
            know (or care) which agent handles it."""
            try:
                body = json.loads(self._read_body() or b"{}")
            except json.JSONDecodeError:
                self._send_json(400, {"error": {
                    "code": "bad_json",
                    "message": "body must be JSON"}})
                return
            subject = "api"
            auth = self.headers.get("authorization", "")
            if auth:
                tok = auth.removeprefix("Bearer ").strip()
                try:
                    payload = ui.app.tokens.verify(tok)
                    subject = payload.get("sub", "api")
                except Exception:
                    self._send_json(401, {"error": {
                        "code": "unauthorized",
                        "message": "invalid token"}})
                    return
            question = body.get("question") or body.get("query") or ""
            try:
                res = ui.ask_orchestrator(
                    question=question, subject=subject)
                self._send_json(200, res)
            except UIError as e:
                self._send_json(e.status, {"error": {
                    "code": "ask_error", "message": e.message}})

        def _native_flow(self, flow_name: str) -> None:
            """POST /flow/<name>  body: {"question": "..."}. Runs a
            saved multi-agent flow (sequential or parallel). Token-auth;
            the token subject is recorded for audit."""
            try:
                body = json.loads(self._read_body() or b"{}")
            except json.JSONDecodeError:
                self._send_json(400, {"error": {
                    "code": "bad_json",
                    "message": "body must be JSON"}})
                return
            subject = "api"
            auth = self.headers.get("authorization", "")
            if auth:
                tok = auth.removeprefix("Bearer ").strip()
                try:
                    payload = ui.app.tokens.verify(tok)
                    subject = payload.get("sub", "api")
                except Exception:
                    self._send_json(401, {"error": {
                        "code": "unauthorized",
                        "message": "invalid token"}})
                    return
            pseudo = Session(sid="api", username=subject,
                              roles=["user"], access_token="api")
            question = body.get("question") or body.get("query") or ""
            try:
                res = ui.run_flow(
                    pseudo, name=flow_name, question=question)
                self._send_json(200, res)
            except UIError as e:
                self._send_json(e.status, {"error": {
                    "code": "flow_error", "message": e.message}})

        def _api_invoke(self, sess: Session, spell_name: str) -> None:
            try:
                body = json.loads(self._read_body() or b"{}")
            except json.JSONDecodeError:
                raise UIError(400, "bad json body") from None
            if spell_name not in ui.app._spells:
                raise UIError(404, f"unknown spell: {spell_name}")
            spell = ui.app._spells[spell_name]
            # Enforce scope: if the spell declares scopes, only admin can run
            # (the bootstrap UI doesn't issue per-user scoped tokens — that
            # plumbing lives one level down).
            if spell.scopes and not sess.is_admin():
                raise UIError(403,
                              f"spell '{spell_name}' requires admin role")
            try:
                result = ui.app.invoke(spell_name, body)
                self._send_json(200, {"ok": True, "result": result})
            except Exception as e:
                self._send_json(200, {"ok": False,
                                       "error": str(e),
                                       "type": type(e).__name__})

        def _api_grimoire(self, sess: Session, qs: dict) -> None:
            since = int(qs.get("since", ["0"])[0])
            limit = int(qs.get("limit", ["100"])[0])
            pages = ui.app.grimoire.pages(since_seq=since, limit=limit)
            if not sess.is_admin():
                # Non-admin sees only own pages
                pages = [p for p in pages if p.get("subject") == sess.username]
            verify = ui.app.grimoire.verify()
            self._send_json(200, {
                "pages": pages,
                "verify": verify,
                "head": ui.app.grimoire.head(),
            })

        def _api_audit(self, sess: Session, qs: dict) -> None:
            calls = list(ui.app._recent_calls)
            spell_filter = qs.get("spell", [""])[0]
            subj_filter = qs.get("subject", [""])[0]
            only_err = qs.get("errors", [""])[0] in ("1", "true")
            if not sess.is_admin():
                calls = [c for c in calls if c.subject == sess.username]
            if spell_filter:
                calls = [c for c in calls if c.spell == spell_filter]
            if subj_filter:
                calls = [c for c in calls if c.subject == subj_filter]
            if only_err:
                calls = [c for c in calls if not c.ok]
            calls = calls[-200:]
            self._send_json(200, {
                "calls": [
                    {"trace_id": c.trace_id, "spell": c.spell,
                     "subject": c.subject, "ok": c.ok,
                     "error_code": c.error_code,
                     "elapsed_ms": round(c.elapsed_ms, 2), "ts": c.ts}
                    for c in calls[::-1]
                ],
            })

        def _api_users(self, sess: Session) -> None:
            live = ui.sessions.all()
            store = (ui.users.list_users()
                     if ui.users is not None else [])
            self._send_json(200, {
                "users": [
                    {"username": u.username, "roles": u.roles,
                     "created_at": u.created_at,
                     "last_active": u.last_active,
                     "is_me": u.sid == sess.sid}
                    for u in live
                ],
                "store": [u.to_public() for u in store],
                "superusers": sorted(ui._superusers),
                "admins": sorted(ui._admins),
                "store_enabled": ui.users is not None,
            })

        def _api_users_create(self, sess: Session) -> None:
            try:
                body = json.loads(self._read_body() or b"{}")
            except json.JSONDecodeError:
                raise UIError(400, "bad json") from None
            roles = body.get("roles") or ["user"]
            if not isinstance(roles, list) or not all(
                    isinstance(r, str) for r in roles):
                raise UIError(400, "roles must be a list of strings")
            res = ui.admin_create_user(
                sess,
                username=(body.get("username") or "").strip(),
                password=body.get("password") or "",
                roles=roles)
            self._send_json(200, {"ok": True, **res})

        def _api_users_set_roles(self, sess: Session,
                                  uname: str) -> None:
            try:
                body = json.loads(self._read_body() or b"{}")
            except json.JSONDecodeError:
                raise UIError(400, "bad json") from None
            roles = body.get("roles") or []
            if not isinstance(roles, list) or not all(
                    isinstance(r, str) for r in roles):
                raise UIError(400, "roles must be a list of strings")
            res = ui.admin_set_roles(
                sess, username=uname, roles=roles)
            self._send_json(200, {"ok": True, **res})

        def _api_users_reset_pw(self, sess: Session,
                                 uname: str) -> None:
            try:
                body = json.loads(self._read_body() or b"{}")
            except json.JSONDecodeError:
                raise UIError(400, "bad json") from None
            new_pw = body.get("password") or ""
            res = ui.admin_reset_password(
                sess, username=uname, new_password=new_pw)
            self._send_json(200, res)

        def _api_users_delete(self, sess: Session,
                               uname: str) -> None:
            res = ui.admin_delete_user(sess, username=uname)
            self._send_json(200, res)

        def _api_agent_run(self, sess: Session) -> None:
            try:
                body = json.loads(self._read_body() or b"{}")
            except json.JSONDecodeError:
                raise UIError(400, "bad json") from None
            try:
                from shabd_agent import Agent, MockBackend
            except ImportError:
                raise UIError(500, "shabd_agent not installed") from None
            prompt = (body.get("prompt") or "").strip()
            if not prompt:
                raise UIError(400, "prompt is required")
            system = body.get("system", "")
            # Mock plan: optionally invoke first selected tool, then a
            # canned final answer. For real LLM, the caller wires the
            # backend in code.
            tool_calls = body.get("plan", [])
            plan = []
            for step in tool_calls:
                if isinstance(step, dict) and step.get("tool"):
                    plan.append({"tool": step["tool"],
                                  "args": step.get("args", {})})
                elif isinstance(step, str):
                    plan.append(step)
            if not plan:
                plan = [f"(no plan supplied — echoing user prompt) {prompt}"]
            agent = Agent.from_shabd(
                ui.app, llm=MockBackend(plan=plan),
                system=system or "You are a helpful assistant.",
                max_steps=int(body.get("max_steps", 5)),
                track_provenance=bool(body.get("track_provenance", True)),
            )
            try:
                result = agent.run(prompt)
                self._send_json(200, {
                    "ok": True,
                    "answer": result.answer,
                    "stopped": result.stopped_reason,
                    "steps": [
                        {"n": s.n, "text": s.assistant.text,
                         "tool_calls": [
                             {"name": tc.name, "arguments": tc.arguments}
                             for tc in s.assistant.tool_calls
                         ],
                         "elapsed_ms": round(s.elapsed_ms, 2),
                         "tool_results": s.tool_results}
                        for s in result.steps
                    ],
                })
            except Exception as e:
                self._send_json(200, {"ok": False, "error": str(e)})

        def _api_orch_classify(self, sess: Session) -> None:
            try:
                body = json.loads(self._read_body() or b"{}")
            except json.JSONDecodeError:
                raise UIError(400, "bad json") from None
            res = ui.classify_query((body.get("query") or "").strip())
            self._send_json(200, res)

        def _api_orch_run(self, sess: Session) -> None:
            try:
                body = json.loads(self._read_body() or b"{}")
            except json.JSONDecodeError:
                raise UIError(400, "bad json") from None
            res = ui.route_and_run(
                sess, query=(body.get("query") or "").strip())
            self._send_json(200, res)

        def _api_chains_list(self, sess: Session) -> None:
            # Offer the spells that can be steps (exclude chains
            # themselves to avoid trivial recursion in the picker).
            steppable = [
                n for n, s in ui.app._spells.items()
                if "chain" not in (s.tags or [])]
            self._send_json(200, {
                "chains": ui.list_chains(),
                "spells": steppable,
            })

        def _api_chains_create(self, sess: Session) -> None:
            try:
                body = json.loads(self._read_body() or b"{}")
            except json.JSONDecodeError:
                raise UIError(400, "bad json") from None
            steps = body.get("steps") or []
            if not isinstance(steps, list):
                raise UIError(400, "steps must be a list")
            res = ui.create_chain(
                sess,
                name=(body.get("name") or "").strip(),
                steps=steps,
                description=(body.get("description") or "").strip(),
                scopes=body.get("scopes") or [])
            self._send_json(200, {"ok": True, **res})

        def _api_chains_delete(self, sess: Session,
                                name: str) -> None:
            res = ui.delete_chain(sess, name)
            self._send_json(200, res)

        def _api_sources_list(self, sess: Session) -> None:
            self._send_json(200, {
                "sources": ui.list_tool_sources(),
            })

        def _api_sources_connect(self, sess: Session) -> None:
            try:
                body = json.loads(self._read_body() or b"{}")
            except json.JSONDecodeError:
                raise UIError(400, "bad json") from None
            res = ui.connect_tool_source(
                sess,
                name=(body.get("name") or "").strip(),
                kind=(body.get("kind") or "").strip(),
                url=(body.get("url") or "").strip(),
                token=body.get("token") or "",
                transport=(body.get("transport") or "http").strip())
            self._send_json(200, {"ok": True, **res})

        def _api_sources_disconnect(self, sess: Session,
                                     name: str) -> None:
            res = ui.disconnect_tool_source(sess, name)
            self._send_json(200, res)

        def _api_flows_list(self, sess: Session) -> None:
            self._send_json(200, {
                "flows": ui.list_flows(),
                "agents": list(ui._agents.keys()),
            })

        # ---- Knowledge base (RAG) ----
        def _api_kb_list(self, sess: Session) -> None:
            self._send_json(200, {"kbs": ui.list_kbs()})

        def _api_kb_create(self, sess: Session) -> None:
            try:
                body = json.loads(self._read_body() or b"{}")
            except json.JSONDecodeError:
                raise UIError(400, "bad json") from None
            res = ui.create_kb(
                sess, name=(body.get("name") or "").strip(),
                description=(body.get("description") or "").strip())
            self._send_json(200, {"ok": True, **res})

        def _api_kb_add(self, sess: Session) -> None:
            try:
                body = json.loads(self._read_body() or b"{}")
            except json.JSONDecodeError:
                raise UIError(400, "bad json") from None
            res = ui.add_kb_text(
                sess, name=(body.get("name") or "").strip(),
                text=body.get("text") or "",
                source=(body.get("source") or "pasted").strip())
            self._send_json(200, res)

        def _api_kb_query(self, sess: Session) -> None:
            try:
                body = json.loads(self._read_body() or b"{}")
            except json.JSONDecodeError:
                raise UIError(400, "bad json") from None
            name = (body.get("name") or "").strip()
            hits = ui.query_kb(
                name, body.get("question") or "",
                top_k=int(body.get("top_k", 4)))
            self._send_json(200, {"hits": hits})

        def _api_kb_expose(self, sess: Session, name: str) -> None:
            res = ui.expose_kb(sess, name)
            self._send_json(200, res)

        # ---- SQL Intelligence connectors ----
        def _api_sqlsvc_create(self, sess: Session) -> None:
            try:
                body = json.loads(self._read_body() or b"{}")
            except json.JSONDecodeError:
                raise UIError(400, "bad json") from None
            res = ui.create_sql_service(
                sess,
                name=(body.get("name") or "").strip(),
                base_url=(body.get("base_url") or "").strip(),
                api_key=body.get("api_key") or "",
                auth_style=(body.get("auth_style") or "bearer").strip(),
                ask_path=(body.get("ask_path") or "/query/ask").strip(),
                query_field=(body.get("query_field") or "query").strip(),
                answer_field=(
                    body.get("answer_field") or "answer").strip(),
                description=(body.get("description") or "").strip(),
                extra={
                    "top_k": body.get("top_k"),
                    "collection": body.get("collection"),
                    "table": body.get("table"),
                    "platform": body.get("platform"),
                })
            self._send_json(200, {"ok": True, **res})

        def _api_sqlsvc_test(self, sess: Session) -> None:
            try:
                body = json.loads(self._read_body() or b"{}")
            except json.JSONDecodeError:
                raise UIError(400, "bad json") from None
            res = ui.test_sql_service(
                (body.get("name") or "").strip(),
                body.get("question") or "",
                thread_id=body.get("thread_id") or None)
            self._send_json(200, {"ok": True, **res})

        # ---- Nova RAG pipeline service ----
        def _api_nova_config(self, sess: Session) -> None:
            try:
                body = json.loads(self._read_body() or b"{}")
            except json.JSONDecodeError:
                raise UIError(400, "bad json") from None
            res = ui.nova_set_config(
                sess,
                base_url=(body.get("base_url") or "").strip(),
                api_key=body.get("api_key") or "",
                auth_style=(body.get("auth_style") or "bearer").strip())
            self._send_json(200, {"ok": True, **res})

        def _api_nova_tenants(self, sess: Session) -> None:
            try:
                self._send_json(200, {"tenants": ui.nova_tenants()})
            except UIError as e:
                self._send_json(e.status, {"error": e.message})

        def _api_nova_tenant_create(self, sess: Session) -> None:
            try:
                body = json.loads(self._read_body() or b"{}")
            except json.JSONDecodeError:
                raise UIError(400, "bad json") from None
            res = ui.nova_create_tenant(
                sess, name=(body.get("name") or "").strip(),
                description=(body.get("description") or "").strip())
            self._send_json(200, {"ok": True, "tenant": res})

        def _api_nova_pipelines(self, sess: Session, qs: dict) -> None:
            tid = qs.get("tenant_id", [""])[0]
            try:
                pls = ui.nova_pipelines(tid)
            except UIError as e:
                return self._send_json(e.status, {"error": e.message})
            self._send_json(200, {
                "pipelines": pls,
                "exposed": ui.nova_exposed_map(),
            })

        def _api_nova_pipeline_create(self, sess: Session) -> None:
            try:
                body = json.loads(self._read_body() or b"{}")
            except json.JSONDecodeError:
                raise UIError(400, "bad json") from None
            res = ui.nova_create_pipeline(
                sess,
                tenant_id=(body.get("tenant_id") or "").strip(),
                name=(body.get("name") or "").strip(),
                description=(body.get("description") or "").strip(),
                config=body.get("config") or None,
                collection_name=(body.get("collection_name") or "").strip(),
                table_name=(body.get("table_name") or "").strip())
            self._send_json(200, {"ok": True, "pipeline": res})

        def _api_nova_ingest(self, sess: Session) -> None:
            try:
                body = json.loads(self._read_body() or b"{}")
            except json.JSONDecodeError:
                raise UIError(400, "bad json") from None
            pid = (body.get("pipeline_id") or "").strip()
            filename = (body.get("filename") or "").strip()
            b64 = body.get("content_b64")
            if b64:
                import base64 as _b64
                try:
                    raw = _b64.b64decode(b64)
                except Exception:
                    raise UIError(400, "bad base64") from None
                res = ui.nova_ingest_file(
                    sess, pid=pid, filename=filename or "document",
                    content=raw,
                    content_type=(body.get("content_type")
                                  or "application/octet-stream"))
            else:
                res = ui.nova_ingest_text(
                    sess, pid=pid, filename=filename,
                    text=body.get("text") or "")
            self._send_json(200, {"ok": True, **res})

        def _api_nova_query(self, sess: Session) -> None:
            try:
                body = json.loads(self._read_body() or b"{}")
            except json.JSONDecodeError:
                raise UIError(400, "bad json") from None
            try:
                res = ui.nova_query(
                    (body.get("pipeline_id") or "").strip(),
                    body.get("question") or "",
                    top_k=body.get("top_k"),
                    retriever=(body.get("retriever") or "").strip())
                self._send_json(200, {"ok": True, **res})
            except UIError as e:
                self._send_json(e.status, {"error": e.message})

        def _api_nova_expose(self, sess: Session) -> None:
            try:
                body = json.loads(self._read_body() or b"{}")
            except json.JSONDecodeError:
                raise UIError(400, "bad json") from None
            res = ui.nova_expose_pipeline(
                sess, pid=(body.get("pipeline_id") or "").strip(),
                name=(body.get("name") or "").strip())
            self._send_json(200, {"ok": True, **res})

        def _api_flows_save(self, sess: Session) -> None:
            try:
                body = json.loads(self._read_body() or b"{}")
            except json.JSONDecodeError:
                raise UIError(400, "bad json") from None
            agents = body.get("agents") or []
            if not isinstance(agents, list):
                raise UIError(400, "agents must be a list")
            flow = ui.save_flow(
                sess,
                name=(body.get("name") or "").strip(),
                kind=(body.get("kind") or "").strip(),
                agents=agents,
                description=(body.get("description") or "").strip(),
                synthesizer_system=(
                    body.get("synthesizer_system") or "").strip())
            self._send_json(200, {"ok": True, **flow})

        def _api_flows_run(self, sess: Session) -> None:
            try:
                body = json.loads(self._read_body() or b"{}")
            except json.JSONDecodeError:
                raise UIError(400, "bad json") from None
            res = ui.run_flow(
                sess,
                name=(body.get("name") or "").strip(),
                question=(body.get("question")
                          or body.get("query") or ""))
            self._send_json(200, res)

        def _api_flows_delete(self, sess: Session,
                               name: str) -> None:
            res = ui.delete_flow(sess, name)
            self._send_json(200, res)

        def _api_intents_list(self, sess: Session) -> None:
            self._send_json(200, {
                "intents": list(ui._intents.values()),
                "agents": list(ui._agents.keys()),
            })

        def _api_intents_save(self, sess: Session) -> None:
            try:
                body = json.loads(self._read_body() or b"{}")
            except json.JSONDecodeError:
                raise UIError(400, "bad json") from None
            intent = ui.save_intent(
                sess,
                name=(body.get("name") or "").strip(),
                keywords=body.get("keywords") or [],
                description=body.get("description") or "",
                route_to=(body.get("route_to") or "").strip())
            self._send_json(200, {"ok": True, **intent})

        def _api_intents_delete(self, sess: Session,
                                 name: str) -> None:
            res = ui.delete_intent(sess, name)
            self._send_json(200, res)

        def _api_notary_publish(self, sess: Session) -> None:
            if ui.notary is None:
                raise UIError(404, "no notary configured")
            root = ui.notary.publish_root()
            self._send_json(200, {
                "ok": True,
                "root": root.to_dict(),
            })

        def _api_notary_state(self, sess: Session) -> None:
            if ui.notary is None:
                self._send_json(200, {
                    "configured": False,
                    "entity": None, "roots": [], "peer_roots": [],
                    "countersignatures": [],
                })
                return
            self._send_json(200, {
                "configured": True,
                "entity": ui.notary.entity,
                "roots": [r.to_dict() for r in ui.notary.roots()],
                "peer_roots": [r.to_dict()
                                for r in ui.notary.held_peer_roots()],
                "countersignatures": [
                    c.to_dict()
                    for c in ui.notary.countersignatures_received()
                ],
            })

        def _api_settings(self, sess: Session) -> None:
            kc = ui.keycloak
            self._send_json(200, {
                "app_name": ui.app.name,
                "require_auth": ui.app.require_auth,
                "bind": f"{ui.bind}:{ui.port}",
                "session_count": len(ui.sessions.all()),
                "secure_cookies": ui.force_secure_cookies,
                "superusers": sorted(ui._superusers),
                "admins": sorted(ui._admins),
                "keycloak": {
                    "configured": kc is not None,
                    "server_url": kc.server_url if kc else "",
                    "realm": kc.realm if kc else "",
                    "client_id": kc.client_id if kc else "",
                },
            })

        # =========================================================
        # HTML pages
        # =========================================================

        def _page_dashboard(self, sess: Session) -> None:
            body = """
            <div class="head"><h2>Dashboard</h2>
              <div><span class="kbd">Ctrl+R</span> to refresh</div></div>
            <div class="cards" id="cards"></div>
            <div class="panel"><h3>Audit Chain</h3>
              <div id="chain"></div></div>
            <div class="panel"><h3>Recent Calls</h3>
              <table id="recent"><thead><tr><th>Trace</th><th>Spell</th>
              <th>Subject</th><th>Status</th><th>Latency</th></tr></thead>
              <tbody></tbody></table></div>
            """
            script = """
            async function load() {
              const r = await api('/api/dashboard');
              const d = r.body;
              document.getElementById('cards').innerHTML = `
                <div class="card"><div class="label">Spells</div>
                  <div class="value">${d.spells}</div></div>
                <div class="card"><div class="label">Audit Pages</div>
                  <div class="value">${d.audit_pages}</div></div>
                <div class="card"><div class="label">Chain</div>
                  <div class="value">${d.chain_ok ? '✓' : '✗'}</div>
                  <div class="delta">${d.head}…</div></div>
                <div class="card"><div class="label">Recent Calls</div>
                  <div class="value">${d.recent_calls.length}</div></div>`;
              document.getElementById('chain').innerHTML = d.chain_ok
                ? '<span class="tag ok">Verified</span> head ' + d.head + '…'
                : '<span class="tag err">Tamper detected</span>';
              const tb = document.querySelector('#recent tbody');
              tb.innerHTML = d.recent_calls.map(c => `<tr>
                <td><code>${c.trace_id}</code></td>
                <td>${c.spell}</td><td>${c.subject}</td>
                <td>${c.ok ? '<span class="tag ok">OK</span>'
                           : '<span class="tag err">ERR</span>'}</td>
                <td>${c.elapsed_ms} ms</td></tr>`).join('')
                || '<tr><td colspan="5" class="empty">No calls yet</td></tr>';
            }
            load(); setInterval(load, 5000);
            """
            self._send_html(200, "dashboard", body, sess, script)

        def _page_spells(self, sess: Session) -> None:
            body = """
            <div class="head"><h2>Spells</h2></div>
            <div class="panel">
              <h3>Test mode</h3>
              <div class="row">
                <div style="flex:1">
                  <label>Mode</label>
                  <select id="t-mode" class="full">
                    <option value="session">Browser session (no scope check)</option>
                    <option value="token">Bearer token (real scope/auth check)</option>
                  </select>
                </div>
                <div style="flex:3" id="t-tok-wrap" style="display:none">
                  <label>Bearer token (paste from <a href="/tokens" style="color:var(--accent)">/tokens</a>)</label>
                  <input id="t-tok" class="full" type="password"
                         placeholder="ey…">
                </div>
              </div>
              <p style="color:var(--dim);font-size:12px;margin:0">
                Session mode bypasses scope/auth (browser already
                authenticated). Token mode goes through the real SHABD
                HTTP route so scoped spells are enforced — same path
                an external client would use.
              </p>
            </div>
            <div class="panel" id="list"></div>
            """
            script = """
            const HTTP_BASE = location.origin;
            function _mode() { return document.getElementById('t-mode').value; }
            function _tok()  { return document.getElementById('t-tok').value.trim(); }

            document.getElementById('t-mode').addEventListener('change', e => {
              document.getElementById('t-tok-wrap').style.display =
                e.target.value === 'token' ? 'block' : 'none';
            });

            async function invokeViaToken(name, args, tok) {
              const r = await fetch(HTTP_BASE + '/spells/' + encodeURIComponent(name), {
                method: 'POST',
                headers: {
                  'Content-Type': 'application/json',
                  'Authorization': 'Bearer ' + tok,
                  'Idempotency-Key': crypto.randomUUID(),
                },
                body: JSON.stringify(args),
              });
              let body; try { body = await r.json(); } catch { body = {raw: await r.text()}; }
              return { status: r.status, body };
            }

            async function load() {
              const r = await api('/api/spells');
              const list = document.getElementById('list');
              if (!r.body.spells.length) {
                list.innerHTML='<div class="empty">No spells registered. Build one at /builder.</div>';
                return;
              }
              // Decide whether a property should render as a numeric input.
              // Handles plain {type:integer/number} AND unions like
              // int|float -> {anyOf:[{integer},{number}]}. Only treat as
              // numeric when a number branch exists and NO string branch does
              // (so str|int still renders as text and sends a string).
              function _numeric(p) {
                if (!p) return false;
                if (p.type === 'integer' || p.type === 'number') return true;
                const alts = p.anyOf || p.oneOf;
                if (Array.isArray(alts)) {
                  const hasStr = alts.some(x => x && x.type === 'string');
                  const hasNum = alts.some(x => x &&
                    (x.type === 'integer' || x.type === 'number'));
                  return hasNum && !hasStr;
                }
                return false;
              }
              // Does the field permit a non-integer (float)? If so the number
              // input needs step="any" — the HTML default step=1 makes the
              // browser reject "2.5" as a step mismatch and silently block the
              // whole form submit (handler never fires, no output shown).
              function _allowsFloat(p) {
                if (!p) return false;
                if (p.type === 'number') return true;
                const alts = p.anyOf || p.oneOf;
                if (Array.isArray(alts))
                  return alts.some(x => x && x.type === 'number');
                return false;
              }
              // Object/array params can't fit a one-line text box AND must be
              // sent as real JSON (not a string), else the server rejects them
              // e.g. "request must be object". Render a JSON textarea for these
              // and JSON.parse the value on submit.
              function _objSchema(p) {
                if (!p) return null;
                if (p.type === 'object') return p;
                const alts = p.anyOf || p.oneOf;
                if (Array.isArray(alts))
                  return alts.find(x => x && x.type === 'object') || null;
                return null;
              }
              function _isArray(p) {
                if (!p) return false;
                if (p.type === 'array') return true;
                const alts = p.anyOf || p.oneOf;
                return Array.isArray(alts) &&
                  alts.some(x => x && x.type === 'array');
              }
              function _complex(p) { return !!_objSchema(p) || _isArray(p); }
              // Build a fill-in-the-blanks JSON skeleton so the user sees the
              // shape the tool expects instead of a blank box.
              function _tmpl(p) {
                const o = _objSchema(p);
                if (o && o.properties) {
                  const t = {};
                  for (const [k, v] of Object.entries(o.properties)) {
                    t[k] = (v.type === 'integer' || v.type === 'number') ? 0
                         : v.type === 'boolean' ? false
                         : v.type === 'array' ? []
                         : v.type === 'object' ? {} : '';
                  }
                  return JSON.stringify(t, null, 2);
                }
                if (_isArray(p)) return '[]';
                return '{}';
              }
              list.innerHTML = r.body.spells.map(s => {
                const props = Object.entries(s.schema.properties || {});
                const required = new Set(s.schema.required || []);
                const fields = props.map(([k, p]) => {
                  const star = required.has(k) ? ' *' : '';
                  if (_complex(p)) {
                    const tmpl = _tmpl(p).replace(/"/g, '&quot;');
                    const desc = p.description
                      ? `<div style="color:var(--dim);font-size:12px;margin:2px 0">${p.description}</div>`
                      : '';
                    return `<div style="flex:1 1 100%">
                      <label>${k}${star} <span style="color:var(--dim);font-weight:400">(JSON)</span></label>
                      ${desc}
                      <textarea class="full" name="${k}" data-json="1"
                        style="min-height:90px;font-family:ui-monospace,monospace"
                        placeholder="${tmpl}"></textarea></div>`;
                  }
                  const inputType = _numeric(p) ? 'number' : 'text';
                  const step = _allowsFloat(p) ? ' step="any"' : '';
                  const hint = p.description || p.example || '';
                  return `<div><label>${k}${star}</label>
                    <input class="full" name="${k}" type="${inputType}"${step}
                           placeholder="${hint}"></div>`;
                }).join('');
                const scopes = (s.scopes || []).map(x=>'<span class="tag info">'+x+'</span>').join(' ')
                  || '<span style="color:var(--dim);font-size:12px">no scopes</span>';
                return `<div class="panel">
                  <h3>${s.name}</h3>
                  <p style="color:var(--dim);font-size:13px">${s.description||''}</p>
                  <div style="margin-bottom:10px">${scopes}</div>
                  <form data-spell="${s.name}">
                    <div class="row">${fields || '<i style="color:var(--dim)">no arguments</i>'}</div>
                    <button type="submit">Invoke</button>
                    <pre style="margin-top:12px" data-out></pre>
                  </form></div>`;
              }).join('');
              list.querySelectorAll('form').forEach(f => {
                f.addEventListener('submit', async e => {
                  e.preventDefault();
                  const name = f.dataset.spell;
                  const args = {};
                  let inputErr = null;
                  f.querySelectorAll('[name]').forEach(i => {
                    if (i.value === '') return;
                    if (i.dataset.json) {
                      try { args[i.name] = JSON.parse(i.value); }
                      catch (err) {
                        inputErr = i.name + ': invalid JSON — ' + err.message;
                      }
                    } else if (i.type === 'number') {
                      args[i.name] = Number(i.value);
                    } else {
                      args[i.name] = i.value;
                    }
                  });
                  if (inputErr) {
                    f.querySelector('[data-out]').textContent =
                      'Input error\\n\\n' + inputErr;
                    return;
                  }
                  let r;
                  if (_mode() === 'token') {
                    const tok = _tok();
                    if (!tok) { alert('Paste a token first.'); return; }
                    r = await invokeViaToken(name, args, tok);
                  } else {
                    r = await api('/api/invoke/' + encodeURIComponent(name),
                                   {method:'POST', body: args});
                  }
                  f.querySelector('[data-out]').textContent =
                    'HTTP ' + r.status + '\\n\\n' +
                    JSON.stringify(r.body, null, 2);
                });
              });
            }
            load();
            """
            self._send_html(200, "spells", body, sess, script)

        def _page_grimoire(self, sess: Session) -> None:
            body = """
            <div class="head"><h2>Grimoire — Audit Chain</h2>
              <button onclick="load()" class="ghost">Refresh</button></div>
            <div class="panel"><div id="status"></div></div>
            <div class="panel"><h3>Pages</h3>
              <table><thead><tr><th>Seq</th><th>Spell</th><th>Subject</th>
              <th>OK</th><th>Hash</th><th>TS</th></tr></thead>
              <tbody id="pages"></tbody></table></div>
            """
            script = """
            async function load() {
              const r = await api('/api/grimoire?limit=200');
              const v = r.body.verify;
              document.getElementById('status').innerHTML = v.ok
                ? `<span class="tag ok">Chain intact</span>
                   <strong>${v.pages}</strong> pages,
                   head <code>${(r.body.head||'').slice(0,32)}…</code>`
                : `<span class="tag err">TAMPER at seq ${v.at_seq}</span>
                   ${v.reason}`;
              const tb = document.getElementById('pages');
              tb.innerHTML = (r.body.pages || []).map(p => `<tr>
                <td>${p.seq}</td><td>${p.spell}</td><td>${p.subject}</td>
                <td>${p.ok ? '✓' : '✗'}</td>
                <td><code>${(p.hash||'').slice(0,20)}…</code></td>
                <td>${new Date(p.ts*1000).toLocaleTimeString()}</td></tr>`).join('')
                || '<tr><td colspan="6" class="empty">Empty chain</td></tr>';
            }
            load();
            """
            self._send_html(200, "grimoire", body, sess, script)

        def _page_audit(self, sess: Session) -> None:
            body = """
            <div class="head"><h2>Audit Log</h2></div>
            <div class="panel">
              <div class="row">
                <div><label>Spell</label>
                  <input id="f-spell" class="full"></div>
                <div><label>Subject</label>
                  <input id="f-subj" class="full"></div>
                <div><label>Only errors?</label>
                  <select id="f-err" class="full">
                    <option value="0">All</option>
                    <option value="1">Errors only</option></select></div>
                <div><button onclick="load()">Filter</button></div>
              </div>
              <table><thead><tr><th>TS</th><th>Trace</th><th>Spell</th>
              <th>Subject</th><th>Status</th><th>Latency</th></tr></thead>
              <tbody id="rows"></tbody></table>
            </div>
            """
            script = """
            async function load() {
              const qs = new URLSearchParams();
              const sp = document.getElementById('f-spell').value;
              const su = document.getElementById('f-subj').value;
              const er = document.getElementById('f-err').value;
              if (sp) qs.set('spell', sp);
              if (su) qs.set('subject', su);
              if (er === '1') qs.set('errors', '1');
              const r = await api('/api/audit?' + qs.toString());
              document.getElementById('rows').innerHTML =
                (r.body.calls||[]).map(c => `<tr>
                <td>${new Date(c.ts*1000).toLocaleString()}</td>
                <td><code>${c.trace_id.slice(0,12)}</code></td>
                <td>${c.spell}</td><td>${c.subject}</td>
                <td>${c.ok ? '<span class="tag ok">OK</span>'
                           : '<span class="tag err">'+ (c.error_code||'ERR')+'</span>'}</td>
                <td>${c.elapsed_ms} ms</td></tr>`).join('')
                || '<tr><td colspan="6" class="empty">No calls match</td></tr>';
            }
            load();
            """
            self._send_html(200, "audit", body, sess, script)

        def _page_agent(self, sess: Session) -> None:
            body = """
            <div class="head"><h2>Agent Lab</h2>
              <span id="llm-tag"></span></div>
            <div class="split">
              <div class="panel"><h3>1 · Saved agents</h3>
                <div id="agents-list" class="empty">Loading…</div>
                <button onclick="newAgent()" style="margin-top:10px">+ New agent</button>
              </div>
              <div class="panel"><h3>2 · Run</h3>
                <label>Agent</label>
                <select id="ag-pick" class="full"></select>
                <label style="margin-top:12px">Ask the agent</label>
                <textarea id="prompt" class="full" style="min-height:80px"
                  placeholder="What is 7 plus 5?"></textarea>
                <div style="margin-top:14px">
                  <button onclick="runAgent()">Run agent</button>
                  <span id="run-status" style="margin-left:14px"></span>
                </div>
                <pre id="out" style="margin-top:14px; min-height:220px">Output appears here.</pre>
              </div>
            </div>

            <div class="panel" id="editor-panel" style="display:none">
              <h3 id="editor-title">New agent</h3>
              <label>Name</label>
              <input id="ag-name" class="full" placeholder="loan-helper">
              <label style="margin-top:10px">Short description</label>
              <input id="ag-desc" class="full" placeholder="Helps customers apply for loans">
              <label style="margin-top:10px">System prompt (tells the LLM how to behave)</label>
              <textarea id="ag-sys" class="full" style="min-height:80px"
                placeholder="You are a banking assistant. Be concise."></textarea>
              <label style="margin-top:10px">Tools the agent may use</label>
              <div id="ag-tools" class="row" style="margin:0"></div>
              <label style="margin-top:10px">Max LLM steps</label>
              <input id="ag-steps" class="full" type="number" value="6" min="1" max="20">
              <label style="display:block;margin-top:12px;font-size:13px;color:var(--text)">
                <input type="checkbox" id="ag-force">
                Force tool use — the model MUST call a tool, it cannot
                answer from its own knowledge (good for arithmetic /
                lookups where you never want a guessed answer).
              </label>
              <div style="margin-top:14px">
                <button onclick="saveAgent()">Save agent</button>
                <button class="ghost" onclick="cancelEdit()">Cancel</button>
                <span id="save-status" style="margin-left:14px"></span>
              </div>
              <div id="ag-api" style="margin-top:16px;display:none">
                <label>API for this agent (use anywhere — just a question)</label>
                <pre id="ag-api-snippet"></pre>
              </div>
            </div>

            <div class="panel">
              <h3>Help — how the agent decides what to do</h3>
              <ol style="color:var(--dim); font-size:13px; line-height:1.7">
                <li>It reads your <strong>system prompt</strong> + your question.</li>
                <li>It is told about each <strong>tool</strong> you ticked,
                    along with its parameters (auto-generated from the
                    spell's Python signature).</li>
                <li>The LLM picks a tool (by name), fills in the
                    arguments, and SHABD invokes that spell.</li>
                <li>The result goes back to the LLM, which decides if it
                    needs another tool or has the final answer.</li>
                <li>Loops up to <strong>Max steps</strong> times then
                    stops.</li>
              </ol>
              <p style="color:var(--dim); font-size:13px">
                If no LLM is set in <a href="/settings">Settings</a>,
                a mock backend runs that just echoes — you'll see a
                placeholder reply. Set Ollama or OpenAI in
                <strong>Settings</strong> for real answers.
              </p>
            </div>
            """
            script = """
            let AGENTS = [];
            let SPELLS = [];

            async function load() {
              const r = await api('/api/agents');
              AGENTS = r.body.agents || [];
              SPELLS = r.body.spells || [];
              const llm = r.body.llm || {};
              const tag = llm.backend && llm.backend !== 'none'
                ? '<span class="tag ok">' + llm.backend
                    + ' · ' + (llm.model||'') + '</span>'
                : '<span class="tag warn">no LLM — set one in Settings</span>';
              document.getElementById('llm-tag').innerHTML = tag;

              const list = document.getElementById('agents-list');
              if (!AGENTS.length) {
                list.innerHTML = '<div class="empty">No saved agents yet — click "New agent" below.</div>';
              } else {
                list.innerHTML = AGENTS.map(a => `<div class="card" style="margin-bottom:8px">
                  <div><strong>${a.name}</strong>
                    <span style="color:var(--dim);font-size:12px">${a.description||''}</span></div>
                  <div style="margin-top:6px">${(a.tools||[]).map(t=>'<span class="tag info">'+t+'</span>').join(' ')||'<i style="color:var(--dim)">no tools</i>'}</div>
                  <div style="margin-top:8px">
                    <button class="ghost" onclick="editAgent('${a.name}')">Edit</button>
                    <button class="danger" onclick="delAgent('${a.name}')">Delete</button>
                  </div>
                </div>`).join('');
              }
              const sel = document.getElementById('ag-pick');
              sel.innerHTML = '<option value="">(ad-hoc — all tools)</option>' +
                AGENTS.map(a => '<option value="'+a.name+'">'+a.name+'</option>').join('');
            }

            function newAgent() {
              document.getElementById('editor-panel').style.display = 'block';
              document.getElementById('editor-title').textContent = 'New agent';
              document.getElementById('ag-name').value='';
              document.getElementById('ag-desc').value='';
              document.getElementById('ag-sys').value='You are a helpful assistant.';
              document.getElementById('ag-steps').value='6';
              document.getElementById('ag-force').checked = false;
              document.getElementById('ag-api').style.display = 'none';
              renderToolPicks([]);
            }

            function editAgent(name) {
              const a = AGENTS.find(x => x.name === name);
              if (!a) return;
              document.getElementById('editor-panel').style.display = 'block';
              document.getElementById('editor-title').textContent = 'Edit ' + name;
              document.getElementById('ag-name').value = name;
              document.getElementById('ag-desc').value = a.description || '';
              document.getElementById('ag-sys').value = a.system || '';
              document.getElementById('ag-steps').value = a.max_steps || 6;
              document.getElementById('ag-force').checked = !!a.force_tools;
              renderToolPicks(a.tools || []);
              showApi(name);
            }

            function showApi(name) {
              const origin = location.origin;
              const snip =
                '# Use this agent from anywhere — just a question:\\n' +
                'curl -X POST ' + origin + '/query/' + name + ' \\\\\\n' +
                '     -H "Content-Type: application/json" \\\\\\n' +
                '     -H "Authorization: Bearer <TOKEN>" \\\\\\n' +
                '     -d \\'{"question": "your question here"}\\'\\n\\n' +
                '# Response: {"ok": true, "answer": "...", "agent": "' + name + '"}';
              document.getElementById('ag-api-snippet').textContent = snip;
              document.getElementById('ag-api').style.display = 'block';
            }

            function renderToolPicks(selected) {
              const sel = new Set(selected || []);
              document.getElementById('ag-tools').innerHTML =
                SPELLS.map(s =>
                  '<label style="flex:0 0 auto; padding-right:14px">'
                  + '<input type="checkbox" name="tool" value="' + s + '"'
                  + (sel.has(s) ? ' checked' : '') + '> ' + s + '</label>'
                ).join('') || '<i style="color:var(--dim)">No spells available — create one in the Builder.</i>';
            }

            function cancelEdit() {
              document.getElementById('editor-panel').style.display = 'none';
            }

            async function saveAgent() {
              const tools = Array.from(
                document.querySelectorAll('#ag-tools input[name=tool]:checked')
              ).map(i => i.value);
              const body = {
                name: document.getElementById('ag-name').value.trim(),
                description: document.getElementById('ag-desc').value.trim(),
                system: document.getElementById('ag-sys').value,
                tools,
                max_steps: Number(document.getElementById('ag-steps').value),
                force_tools: document.getElementById('ag-force').checked,
              };
              const r = await api('/api/agents/save',
                                   {method:'POST', body});
              const msg = document.getElementById('save-status');
              if (r.status>=400 || !r.body.ok)
                msg.innerHTML = '<span class="tag err">'+(r.body.error||'failed')+'</span>';
              else {
                msg.innerHTML = '<span class="tag ok">Saved</span>';
                showApi(body.name);
                load();
              }
            }

            async function delAgent(name) {
              if (!confirm('Delete agent '+name+'?')) return;
              const r = await api('/api/agents/'+encodeURIComponent(name)+'/delete',
                                   {method:'POST'});
              if (r.body.ok) load(); else alert(r.body.error||'failed');
            }

            async function runAgent() {
              const name = document.getElementById('ag-pick').value;
              const prompt = document.getElementById('prompt').value;
              if (!prompt.trim()) { alert('Enter a question'); return; }
              document.getElementById('run-status').innerHTML =
                '<span class="tag info">Running…</span>';
              document.getElementById('out').textContent = '…';
              const body = name ? {name, prompt} : {prompt, tools: SPELLS};
              const r = await api('/api/agents/run',
                                   {method:'POST', body});
              document.getElementById('run-status').innerHTML = r.body.ok
                ? '<span class="tag ok">'+ (r.body.steps||[]).length +' steps</span>'
                : '<span class="tag err">'+ (r.body.error||'error') +'</span>';
              if (r.body.ok) {
                const trace = (r.body.steps||[]).map(s => {
                  const lines = ['Step ' + s.n + ':'];
                  if (s.text && s.text.trim()) lines.push('  ' + s.text.trim());
                  (s.tool_calls||[]).forEach(tc => {
                    lines.push('  → called ' + tc.name +
                               '(' + JSON.stringify(tc.arguments) + ')');
                  });
                  // Only show tool results when there were tool calls.
                  if ((s.tool_calls||[]).length && s.tool_results &&
                      s.tool_results.length) {
                    s.tool_results.forEach(tr => {
                      let c = tr.content || tr;
                      try { c = JSON.parse(tr.content); } catch {}
                      if (c && c.error) {
                        lines.push('  ⚠ ' + (c.error.code || 'error') +
                                   ': ' + (c.error.message || ''));
                      } else {
                        lines.push('  ← ' + JSON.stringify(c));
                      }
                    });
                  }
                  if (!s.text && !(s.tool_calls||[]).length)
                    lines.push('  (model produced the final answer)');
                  return lines.join('\\n');
                }).join('\\n\\n');
                document.getElementById('out').textContent =
                  '✅ Answer: ' + (r.body.answer || '(no answer)')
                  + '\\n\\nStopped: ' + r.body.stopped
                  + '   ·   ' + (r.body.steps||[]).length + ' steps'
                  + '\\n\\n--- how it got there ---\\n' + trace;
              } else {
                document.getElementById('out').textContent =
                  JSON.stringify(r.body, null, 2);
              }
            }

            load();
            """
            self._send_html(200, "agent", body, sess, script)

        def _page_orch(self, sess: Session) -> None:
            body = """
            <div class="head"><h2>Orchestrator</h2></div>
            <div class="panel" style="padding:6px">
              <button id="tab-btn-routing" onclick="showTab('routing')">🎯 Intent Routing</button>
              <button id="tab-btn-flows" class="ghost" onclick="showTab('flows')">🧩 Multi-Agent Flows</button>
            </div>

            <div id="tab-routing">
            <div class="panel"><h3>Route a query to the right agent</h3>
              <p style="color:var(--dim);font-size:13px">
                Type what a user would say. The orchestrator picks the
                best matching intent (by keyword → synonym → meaning → AI),
                then runs the agent that intent points at.
              </p>
              <p style="color:var(--dim);font-size:13px">
                Type what a user would say. The orchestrator picks the
                best matching intent (by keyword → synonym → meaning),
                then runs the agent that intent points at.
              </p>
              <div class="row">
                <div style="flex:4"><label>User query</label>
                  <input id="q" class="full" placeholder="kal ki chuti chahiye"></div>
                <div><button onclick="classify()">Classify only</button></div>
                <div><button onclick="routeRun()">Classify + run agent</button></div>
              </div>
              <pre id="out" style="min-height:120px">Result appears here.</pre>
              <div style="margin-top:14px">
                <label>Public API — let the orchestrator pick the agent (use anywhere)</label>
                <pre id="ask-api">curl -X POST __ORIGIN__/ask \
     -H "Content-Type: application/json" \
     -H "Authorization: Bearer &lt;TOKEN&gt;" \
     -d '{"question": "your question here"}'

# Response: {"ok": true, "intent": "...", "agent": "...", "answer": "..."}</pre>
              </div>
            </div>

            <div class="split">
              <div class="panel"><h3>Add / edit an intent</h3>
                <label>Intent name</label>
                <input id="i-name" class="full" placeholder="hr">
                <label style="margin-top:10px">Keywords (comma-sep — what the user might say)</label>
                <input id="i-kw" class="full" placeholder="leave, holiday, chuti, rest">
                <label style="margin-top:10px">Description (helps the smarter stages)</label>
                <input id="i-desc" class="full" placeholder="HR questions — leaves, attendance, policy">
                <label style="margin-top:10px">Route to agent</label>
                <select id="i-route" class="full"></select>
                <div style="margin-top:14px">
                  <button onclick="saveIntent()">Save intent</button>
                  <span id="i-msg" style="margin-left:12px"></span>
                </div>
              </div>
              <div class="panel"><h3>Registered intents</h3>
                <div id="intents" class="empty">None yet.</div>
              </div>
            </div>

            <div class="panel">
              <h3>How routing decides (cheap → smart)</h3>
              <ol style="color:var(--dim);font-size:13px;line-height:1.7">
                <li><strong>keyword</strong> — exact word match (instant)</li>
                <li><strong>synonym</strong> — chuti→leave, kharab→broken</li>
                <li><strong>n-gram</strong> — phrase overlap with the description</li>
                <li><strong>embedding</strong> — semantic similarity (if available)</li>
                <li><strong>llm</strong> — last resort, asks the configured model</li>
              </ol>
              <p style="color:var(--dim);font-size:12px">
                The <code>via</code> field in the result tells you which
                stage decided.
              </p>
            </div>
            </div><!-- /tab-routing -->

            <div id="tab-flows" style="display:none">
            <div class="panel"><h3>Run a flow</h3>
              <p style="color:var(--dim);font-size:13px">
                A flow orchestrates <strong>several agents</strong> on one
                query. <strong>Sequential</strong>: agents run in order,
                each agent's answer feeds the next. <strong>Parallel</strong>:
                agents run independently, then an LLM combines their
                answers into one.
              </p>
              <div class="row">
                <div style="flex:3"><label>Flow</label>
                  <select id="f-pick" class="full"></select></div>
                <div style="flex:5"><label>Question</label>
                  <input id="f-q" class="full" placeholder="add 5 and 2, then subtract that from 6"></div>
                <div><button onclick="runFlow()">Run flow</button></div>
              </div>
              <pre id="f-out" style="min-height:160px">Result appears here.</pre>
            </div>

            <div class="split">
              <div class="panel"><h3>Build / edit a flow</h3>
                <label>Flow name</label>
                <input id="fb-name" class="full" placeholder="loan_pipeline">
                <label style="margin-top:10px">Type</label>
                <select id="fb-kind" class="full">
                  <option value="sequential">Sequential — output of one feeds the next</option>
                  <option value="parallel">Parallel — run independently, LLM combines</option>
                </select>
                <label style="margin-top:10px">Description</label>
                <input id="fb-desc" class="full" placeholder="what this flow does">
                <label style="margin-top:10px">Agents (order matters for sequential)</label>
                <div id="fb-agents"></div>
                <button class="ghost" onclick="addAgentRow()" style="margin-top:8px">+ Add agent</button>
                <div id="fb-synth-wrap" style="margin-top:10px;display:none">
                  <label>Synthesizer prompt (parallel only — how the LLM combines results)</label>
                  <textarea id="fb-synth" class="full" style="min-height:60px"
                    placeholder="Combine the agent answers into one clear answer."></textarea>
                </div>
                <div style="margin-top:14px">
                  <button onclick="saveFlow()">Save flow</button>
                  <span id="fb-msg" style="margin-left:12px"></span>
                </div>
                <div id="fb-preview" style="margin-top:10px;color:var(--dim);font-size:13px"></div>
              </div>
              <div class="panel"><h3>Saved flows</h3>
                <div id="f-list" class="empty">None yet.</div>
              </div>
            </div>

            <div class="panel">
              <h3>Example</h3>
              <ul style="color:var(--dim);font-size:13px;line-height:1.7">
                <li><strong>Sequential</strong> "add then subtract": agent
                    <code>adder</code> → its answer (7) feeds agent
                    <code>subtractor</code> with the original query, which
                    computes the final number.</li>
                <li><strong>Parallel</strong> "should I go out?": a weather
                    agent and a news agent answer independently; the LLM
                    reads both + your question and decides.</li>
                <li>Each flow is callable as an API:
                    <code>POST /flow/&lt;name&gt;</code> with
                    <code>{"question": "..."}</code>.</li>
              </ul>
            </div>
            </div><!-- /tab-flows -->
            """
            script = """
            let AGENTS = [];

            // Fill the live origin into the /ask snippet.
            document.getElementById('ask-api').textContent =
              document.getElementById('ask-api').textContent
                .replace('__ORIGIN__', location.origin);

            async function load() {
              const r = await api('/api/intents');
              AGENTS = r.body.agents || [];
              const sel = document.getElementById('i-route');
              sel.innerHTML = '<option value="">(no agent — classify only)</option>' +
                AGENTS.map(a => '<option value="'+a+'">'+a+'</option>').join('');
              renderIntents(r.body.intents || []);
            }

            function renderIntents(list) {
              const el = document.getElementById('intents');
              if (!list.length) { el.innerHTML = '<div class="empty">No intents yet — add one on the left.</div>'; return; }
              el.innerHTML = list.map(i => `
                <div class="card" style="margin-bottom:10px">
                  <div><strong>${i.name}</strong>
                    ${i.route_to ? '<span class="tag ok">→ '+i.route_to+'</span>' : '<span class="tag warn">no agent</span>'}</div>
                  <div style="color:var(--dim);font-size:12px;margin:4px 0">${i.description||''}</div>
                  <div>${(i.keywords||[]).map(k=>'<span class="tag info">'+k+'</span>').join(' ')}</div>
                  <div style="margin-top:8px">
                    <button class="ghost" onclick="editIntent('${i.name}')">Edit</button>
                    <button class="danger" onclick="delIntent('${i.name}')">Delete</button>
                  </div>
                </div>`).join('');
            }

            function editIntent(name) {
              api('/api/intents').then(r => {
                const i = (r.body.intents||[]).find(x => x.name === name);
                if (!i) return;
                document.getElementById('i-name').value = i.name;
                document.getElementById('i-kw').value = (i.keywords||[]).join(', ');
                document.getElementById('i-desc').value = i.description || '';
                document.getElementById('i-route').value = i.route_to || '';
              });
            }

            async function saveIntent() {
              const kw = document.getElementById('i-kw').value
                .split(',').map(s=>s.trim()).filter(Boolean);
              const r = await api('/api/intents/save', {method:'POST', body:{
                name: document.getElementById('i-name').value.trim(),
                keywords: kw,
                description: document.getElementById('i-desc').value.trim(),
                route_to: document.getElementById('i-route').value,
              }});
              const msg = document.getElementById('i-msg');
              if (r.status>=400 || !r.body.ok)
                msg.innerHTML = '<span class="tag err">'+(r.body.error||'failed')+'</span>';
              else { msg.innerHTML = '<span class="tag ok">Saved</span>'; load(); }
            }

            async function delIntent(name) {
              if (!confirm('Delete intent '+name+'?')) return;
              const r = await api('/api/intents/'+encodeURIComponent(name)+'/delete',
                                   {method:'POST'});
              if (r.body.ok) load(); else alert(r.body.error||'failed');
            }

            async function classify() {
              const r = await api('/api/orchestrator/classify', {method:'POST',
                body: { query: document.getElementById('q').value }});
              const b = r.body;
              if (b.message && !b.intent) {
                document.getElementById('out').textContent = b.message;
                return;
              }
              document.getElementById('out').textContent =
                'Matched intent: ' + b.intent + '\\n'
                + 'Confidence:     ' + b.confidence + '\\n'
                + 'Decided by:     ' + b.via + ' stage\\n'
                + 'Routes to agent:' + (b.route_to || '(none assigned)');
            }

            async function routeRun() {
              document.getElementById('out').textContent = 'Routing…';
              const r = await api('/api/orchestrator/run', {method:'POST',
                body: { query: document.getElementById('q').value }});
              const b = r.body;
              if (!b.intent) {
                document.getElementById('out').textContent = b.message || JSON.stringify(b,null,2);
                return;
              }
              let txt = 'Matched intent: ' + b.intent + ' (via ' + b.via + ', conf ' + b.confidence + ')\\n';
              if (!b.ran) {
                txt += '\\n' + (b.message || 'No agent ran.');
              } else if (b.result && b.result.ok) {
                txt += 'Ran agent:      ' + b.route_to + '\\n\\n';
                txt += '✅ Answer: ' + (b.result.answer || '(no answer)');
              } else {
                txt += '\\nAgent error: ' + JSON.stringify(b.result, null, 2);
              }
              document.getElementById('out').textContent = txt;
            }

            // ---------- Tab switching ----------
            function showTab(which) {
              document.getElementById('tab-routing').style.display =
                which === 'routing' ? 'block' : 'none';
              document.getElementById('tab-flows').style.display =
                which === 'flows' ? 'block' : 'none';
              document.getElementById('tab-btn-routing').className =
                which === 'routing' ? '' : 'ghost';
              document.getElementById('tab-btn-flows').className =
                which === 'flows' ? '' : 'ghost';
              if (which === 'flows') loadFlows();
            }

            // ---------- Flows ----------
            let FLOW_AGENTS = [];
            function agentOptions(sel) {
              return '<option value="">— pick an agent —</option>' +
                FLOW_AGENTS.map(a => '<option value="'+a+'"'+
                  (a===sel?' selected':'')+'>'+a+'</option>').join('');
            }
            function addAgentRow(val) {
              const div = document.createElement('div');
              div.className = 'row'; div.style.margin = '6px 0';
              div.innerHTML =
                '<div style="flex:5"><select class="full fagent">'+agentOptions(val)+'</select></div>'+
                '<div style="flex:0 0 auto"><button class="ghost" type="button" onclick="this.closest(\\'.row\\').remove();flowPreview()">✕</button></div>';
              document.getElementById('fb-agents').appendChild(div);
              div.querySelector('select').addEventListener('change', flowPreview);
              flowPreview();
            }
            function currentFlowAgents() {
              return Array.from(document.querySelectorAll('#fb-agents select.fagent'))
                .map(s => s.value).filter(Boolean);
            }
            function flowPreview() {
              const a = currentFlowAgents();
              const kind = document.getElementById('fb-kind').value;
              const sep = kind === 'sequential' ? '  →  ' : '  +  ';
              document.getElementById('fb-preview').textContent =
                a.length ? (kind+':  ' + a.join(sep)) : '';
            }
            async function loadFlows() {
              const r = await api('/api/flows');
              FLOW_AGENTS = r.body.agents || [];
              if (!document.querySelector('#fb-agents select')) {
                addAgentRow(); addAgentRow();
              }
              const sel = document.getElementById('f-pick');
              const flows = r.body.flows || [];
              sel.innerHTML = flows.map(f => '<option value="'+f.name+'">'+f.name+' ('+f.kind+')</option>').join('')
                || '<option value="">(no flows yet)</option>';
              renderFlows(flows);
            }
            function renderFlows(list) {
              const el = document.getElementById('f-list');
              if (!list.length) { el.innerHTML = '<div class="empty">No flows yet — build one on the left.</div>'; return; }
              el.innerHTML = list.map(f => `
                <div class="card" style="margin-bottom:10px">
                  <div><strong>${f.name}</strong>
                    <span class="tag info">${f.kind}</span>
                    ${f.live?'':'<span class="tag err">missing agent</span>'}</div>
                  <div style="color:var(--dim);font-size:12px;margin:4px 0">${f.description||''}</div>
                  <div style="font-family:ui-monospace,monospace;font-size:12px">
                    ${(f.agents||[]).join(f.kind==='sequential'?' → ':' + ')}</div>
                  <div style="margin-top:8px">
                    <button class="ghost" onclick="editFlow('${f.name}')">Edit</button>
                    <button class="danger" onclick="delFlow('${f.name}')">Delete</button>
                  </div>
                </div>`).join('');
            }
            async function editFlow(name) {
              const r = await api('/api/flows');
              const f = (r.body.flows||[]).find(x => x.name === name);
              if (!f) return;
              document.getElementById('fb-name').value = f.name;
              document.getElementById('fb-kind').value = f.kind;
              document.getElementById('fb-desc').value = f.description || '';
              document.getElementById('fb-agents').innerHTML = '';
              (f.agents||[]).forEach(a => addAgentRow(a));
              toggleSynth();
            }
            function toggleSynth() {
              document.getElementById('fb-synth-wrap').style.display =
                document.getElementById('fb-kind').value === 'parallel' ? 'block' : 'none';
              flowPreview();
            }
            async function saveFlow() {
              const msg = document.getElementById('fb-msg');
              const r = await api('/api/flows/save', {method:'POST', body:{
                name: document.getElementById('fb-name').value.trim(),
                kind: document.getElementById('fb-kind').value,
                description: document.getElementById('fb-desc').value.trim(),
                agents: currentFlowAgents(),
                synthesizer_system: document.getElementById('fb-synth').value,
              }});
              if (r.status>=400 || !r.body.ok)
                msg.innerHTML = '<span class="tag err">'+(r.body.error||'failed')+'</span>';
              else { msg.innerHTML = '<span class="tag ok">Saved</span>'; loadFlows(); }
            }
            async function delFlow(name) {
              if (!confirm('Delete flow '+name+'?')) return;
              const r = await api('/api/flows/'+encodeURIComponent(name)+'/delete',{method:'POST'});
              if (r.body.ok) loadFlows(); else alert(r.body.error||'failed');
            }
            async function runFlow() {
              const name = document.getElementById('f-pick').value;
              if (!name) { alert('Pick a flow'); return; }
              document.getElementById('f-out').textContent = 'Running…';
              const r = await api('/api/flows/run', {method:'POST', body:{
                name, question: document.getElementById('f-q').value }});
              const b = r.body;
              if (!b.ok) { document.getElementById('f-out').textContent = JSON.stringify(b,null,2); return; }
              let txt = '✅ Final answer ('+b.kind+'):\\n  ' + (b.answer||'(none)') + '\\n\\n--- per-agent ---\\n';
              txt += (b.trace||[]).map(t => '• '+t.agent+': '+t.answer).join('\\n');
              document.getElementById('f-out').textContent = txt;
            }
            document.getElementById('fb-kind').addEventListener('change', toggleSynth);

            load();
            """
            self._send_html(200, "orchestrator", body, sess, script)

        def _page_notary(self, sess: Session) -> None:
            body = """
            <div class="head"><h2>Agent Notary</h2>
              <button onclick="publishRoot()">Publish root</button></div>
            <div class="panel"><h3>Your published roots</h3>
              <div id="roots"></div></div>
            <div class="panel"><h3>Peer roots held</h3>
              <div id="peers"></div></div>
            <div class="panel"><h3>Countersignatures received</h3>
              <div id="cs"></div></div>
            """
            script = """
            async function load() {
              const r = await api('/api/notary/state');
              if (!r.body.configured) {
                document.getElementById('roots').innerHTML =
                  '<div class="empty">Notary is not configured on this server.</div>';
                return;
              }
              document.getElementById('roots').innerHTML = (r.body.roots||[]).map(x =>
                `<pre>${JSON.stringify(x, null, 2)}</pre>`).join('')
                || '<div class="empty">No roots yet — click Publish.</div>';
              document.getElementById('peers').innerHTML = (r.body.peer_roots||[]).map(x =>
                `<pre>${JSON.stringify(x, null, 2)}</pre>`).join('')
                || '<div class="empty">No peer roots received.</div>';
              document.getElementById('cs').innerHTML = (r.body.countersignatures||[]).map(x =>
                `<pre>${JSON.stringify(x, null, 2)}</pre>`).join('')
                || '<div class="empty">No countersignatures received.</div>';
            }
            async function publishRoot() {
              const r = await api('/api/notary/publish', {method:'POST'});
              if (!r.body.ok) alert(r.body.error || 'failed');
              load();
            }
            load();
            """
            self._send_html(200, "notary", body, sess, script)

        def _page_users(self, sess: Session) -> None:
            is_super = sess.is_superuser()
            body = """
            <div class="head"><h2>Users</h2></div>
            <div class="split">
              <div class="panel"><h3>Create user</h3>
                <label>Username</label>
                <input id="u-name" class="full" placeholder="amit">
                <label style="margin-top:10px">Password (min 8)</label>
                <input id="u-pw" class="full" type="password">
                <label style="margin-top:10px">Roles</label>
                <div class="row" style="margin:0">
                  <label style="flex:0 0 auto"><input type="checkbox" id="r-user" checked> user</label>
                  <label style="flex:0 0 auto"><input type="checkbox" id="r-admin"> admin</label>
                  <label style="flex:0 0 auto"><input type="checkbox" id="r-super"> superuser</label>
                </div>
                <div style="margin-top:14px">
                  <button onclick="createUser()">Create</button>
                  <span id="u-msg" style="margin-left:14px"></span></div>
              </div>
              <div class="panel"><h3>Active sessions</h3>
                <table><thead><tr><th>Username</th><th>Roles</th>
                  <th>Signed in</th><th>Last active</th></tr></thead>
                  <tbody id="rows"></tbody></table>
              </div>
            </div>
            <div class="panel"><h3>All accounts</h3>
              <table><thead><tr><th>Username</th><th>Roles</th>
                <th>Created</th><th>Last login</th><th>Actions</th></tr></thead>
                <tbody id="store"></tbody></table>
            </div>
            <div class="panel"><h3>Allow-lists (env)</h3>
              <div id="lists"></div></div>
            """
            script = """
            const IS_SUPER = __SUPER__;
            function _roles() {
              const r = [];
              if (document.getElementById('r-user').checked) r.push('user');
              if (document.getElementById('r-admin').checked) r.push('admin');
              if (document.getElementById('r-super').checked) r.push('superuser');
              return r;
            }
            async function createUser() {
              const msg = document.getElementById('u-msg');
              const r = await api('/api/users/create', {method:'POST', body:{
                username: document.getElementById('u-name').value.trim(),
                password: document.getElementById('u-pw').value,
                roles: _roles(),
              }});
              if (r.status>=400 || !r.body.ok) {
                msg.innerHTML = '<span class="tag err">'+(r.body.error||'failed')+'</span>';
              } else {
                msg.innerHTML = '<span class="tag ok">Created</span>';
                document.getElementById('u-name').value='';
                document.getElementById('u-pw').value='';
                load();
              }
            }
            async function setRoles(u) {
              const raw = prompt('Roles for '+u+' (comma-sep)', 'user');
              if (!raw) return;
              const roles = raw.split(',').map(s=>s.trim()).filter(Boolean);
              const r = await api('/api/users/'+encodeURIComponent(u)+'/roles',
                                   {method:'POST', body:{roles}});
              if (r.body.ok) load(); else alert(r.body.error||'failed');
            }
            async function resetPw(u) {
              const pw = prompt('New password for '+u+' (min 8)');
              if (!pw) return;
              const r = await api('/api/users/'+encodeURIComponent(u)+'/password',
                                   {method:'POST', body:{password:pw}});
              if (r.body.ok) alert('Password updated.');
              else alert(r.body.error||'failed');
            }
            async function delUser(u) {
              if (!confirm('Delete user '+u+'?')) return;
              const r = await api('/api/users/'+encodeURIComponent(u)+'/delete',
                                   {method:'POST'});
              if (r.body.ok) load(); else alert(r.body.error||'failed');
            }
            async function load() {
              const r = await api('/api/users');
              document.getElementById('rows').innerHTML = (r.body.users||[]).map(u => `<tr>
                <td><strong>${u.username}</strong>${u.is_me?' (you)':''}</td>
                <td>${u.roles.map(x=>'<span class="tag info">'+x+'</span>').join(' ')}</td>
                <td>${new Date(u.created_at*1000).toLocaleString()}</td>
                <td>${new Date(u.last_active*1000).toLocaleTimeString()}</td>
                </tr>`).join('') || '<tr><td colspan="4" class="empty">No active sessions</td></tr>';
              document.getElementById('store').innerHTML = (r.body.store||[]).map(u => {
                const del = IS_SUPER
                  ? `<button class="danger" onclick="delUser('${u.username}')">Delete</button>`
                  : '';
                return `<tr>
                  <td><strong>${u.username}</strong></td>
                  <td>${(u.roles||[]).map(x=>'<span class="tag info">'+x+'</span>').join(' ')}</td>
                  <td>${u.created_at?new Date(u.created_at*1000).toLocaleString():'—'}</td>
                  <td>${u.last_login_at?new Date(u.last_login_at*1000).toLocaleString():'never'}</td>
                  <td>
                    <button class="ghost" onclick="setRoles('${u.username}')">Roles</button>
                    <button class="ghost" onclick="resetPw('${u.username}')">Reset pw</button>
                    ${del}
                  </td>
                </tr>`;
              }).join('') || (r.body.store_enabled
                ? '<tr><td colspan="5" class="empty">No accounts yet</td></tr>'
                : '<tr><td colspan="5" class="empty">User store disabled — only Keycloak is active.</td></tr>');
              document.getElementById('lists').innerHTML = `
                <div>Superusers: ${(r.body.superusers||[]).map(x=>'<span class="tag ok">'+x+'</span>').join(' ')||'<i>none</i>'}</div>
                <div style="margin-top:8px">Admins: ${(r.body.admins||[]).map(x=>'<span class="tag info">'+x+'</span>').join(' ')||'<i>none</i>'}</div>`;
            }
            load();
            """.replace("__SUPER__", "true" if is_super else "false")
            self._send_html(200, "users", body, sess, script)

        # =========================================================
        # v2.9 — Spell builder / Tokens / Scopes / Client console
        # =========================================================

        def _api_spells_create(self, sess: Session) -> None:
            try:
                body = json.loads(self._read_body() or b"{}")
            except json.JSONDecodeError:
                raise UIError(400, "bad json") from None
            name = (body.get("name") or "").strip()
            source = body.get("source") or ""
            description = (body.get("description") or "").strip()
            scopes = body.get("scopes") or []
            tags = body.get("tags") or []
            if not isinstance(scopes, list):
                raise UIError(400, "scopes must be a list")
            if not isinstance(tags, list):
                raise UIError(400, "tags must be a list")
            meta = ui.create_spell(
                sess, name=name, source=source,
                description=description, scopes=scopes, tags=tags)
            self._send_json(200, {"ok": True, **meta})

        def _api_spells_delete(self, sess: Session, name: str) -> None:
            res = ui.delete_spell(sess, name)
            self._send_json(200, res)

        def _api_scopes(self, sess: Session) -> None:
            out = []
            for name, spell in ui.app._spells.items():
                out.append({
                    "name": name,
                    "scopes": list(spell.scopes or []),
                    "tags": list(spell.tags or []),
                    "description": (spell.description or "")[:140],
                    "managed_in_ui": name in ui._dynamic_spells,
                })
            self._send_json(200, {"spells": out})

        def _api_scopes_update(self, sess: Session, name: str) -> None:
            try:
                body = json.loads(self._read_body() or b"{}")
            except json.JSONDecodeError:
                raise UIError(400, "bad json") from None
            scopes = body.get("scopes")
            if not isinstance(scopes, list) or not all(
                    isinstance(x, str) for x in scopes):
                raise UIError(400, "scopes must be a list of strings")
            res = ui.update_scopes(sess, name, scopes)
            self._send_json(200, {"ok": True, **res})

        def _api_tokens_list(self, sess: Session) -> None:
            self._send_json(200, {
                "tokens": ui.list_issued_tokens(),
            })

        def _api_tokens_revoke(self, sess: Session,
                                jti: str) -> None:
            res = ui.revoke_token(sess, jti)
            self._send_json(200, res)

        def _api_tokens_issue(self, sess: Session) -> None:
            try:
                body = json.loads(self._read_body() or b"{}")
            except json.JSONDecodeError:
                raise UIError(400, "bad json") from None
            subject = (body.get("subject") or "").strip()
            scopes = body.get("scopes") or []
            ttl = body.get("ttl", 3600)
            if not isinstance(scopes, list) or not all(
                    isinstance(x, str) for x in scopes):
                raise UIError(400, "scopes must be a list of strings")
            res = ui.issue_token(
                sess, subject=subject, scopes=scopes, ttl=ttl)
            self._send_json(200, {"ok": True, **res})

        def _api_client_call(self, sess: Session) -> None:
            try:
                body = json.loads(self._read_body() or b"{}")
            except json.JSONDecodeError:
                raise UIError(400, "bad json") from None
            base_url = (body.get("base_url") or "").strip()
            token = body.get("token") or ""
            action = (body.get("action") or "").strip()
            kw = {k: v for k, v in body.items()
                  if k not in ("base_url", "token", "action")}
            res = ui.client_call(
                sess, base_url=base_url, token=token,
                action=action, **kw)
            self._send_json(200, res)

        # ----- v2.10: Agent registry + Spell editor + LLM config -----

        def _api_agents_list(self, sess: Session) -> None:
            self._send_json(200, {
                "agents": [
                    {**a, "is_mine": a.get("created_by") == sess.username}
                    for a in ui._agents.values()
                ],
                "spells": list(ui.app._spells.keys()),
                "llm": ui.get_llm_config(redact=True),
            })

        def _api_agents_save(self, sess: Session) -> None:
            try:
                body = json.loads(self._read_body() or b"{}")
            except json.JSONDecodeError:
                raise UIError(400, "bad json") from None
            agent = ui.save_agent(
                sess,
                name=(body.get("name") or "").strip(),
                system=body.get("system") or "",
                tools=body.get("tools") or [],
                description=body.get("description") or "",
                max_steps=int(body.get("max_steps", 6)),
                force_tools=bool(body.get("force_tools", False)),
            )
            self._send_json(200, {"ok": True, **agent})

        def _api_agents_delete(self, sess: Session,
                                name: str) -> None:
            res = ui.delete_agent(sess, name)
            self._send_json(200, res)

        def _api_agents_run(self, sess: Session) -> None:
            try:
                body = json.loads(self._read_body() or b"{}")
            except json.JSONDecodeError:
                raise UIError(400, "bad json") from None
            prompt = (body.get("prompt") or "").strip()
            if not prompt:
                raise UIError(400, "prompt is required")
            res = ui.run_agent(
                sess,
                name=body.get("name") or None,
                prompt=prompt,
                system=body.get("system"),
                tools=body.get("tools") or None,
                max_steps=int(body.get("max_steps", 6)),
            )
            self._send_json(200, res)

        def _api_spell_source_get(self, sess: Session,
                                    name: str) -> None:
            src = ui.get_spell_source(name)
            if not src:
                raise UIError(
                    404, f"no UI-managed source for spell '{name}'")
            spell = ui.app._spells.get(name)
            self._send_json(200, {
                "name": name,
                "source": src.get("source", ""),
                "description": (spell.description if spell else ""),
                "scopes": list(spell.scopes or []) if spell else [],
                "tags": list(spell.tags or []) if spell else [],
                "hash": src.get("hash", ""),
                "created_by": src.get("created_by", ""),
                "created_at": src.get("created_at", 0),
                "updated_at": src.get("updated_at", 0),
            })

        def _api_spell_update(self, sess: Session, name: str) -> None:
            try:
                body = json.loads(self._read_body() or b"{}")
            except json.JSONDecodeError:
                raise UIError(400, "bad json") from None
            source = body.get("source") or ""
            description = body.get("description") or ""
            scopes = body.get("scopes") or []
            tags = body.get("tags") or []
            if not isinstance(scopes, list):
                raise UIError(400, "scopes must be a list")
            if not isinstance(tags, list):
                raise UIError(400, "tags must be a list")
            res = ui.update_spell_source(
                sess, name=name, source=source,
                description=description,
                scopes=scopes, tags=tags)
            self._send_json(200, {"ok": True, **res})

        def _api_spell_versions(self, sess: Session,
                                  name: str) -> None:
            self._send_json(200, {
                "name": name,
                "versions": ui.list_spell_versions(name),
            })

        def _api_spell_rollback(self, sess: Session,
                                  name: str) -> None:
            try:
                body = json.loads(self._read_body() or b"{}")
            except json.JSONDecodeError:
                raise UIError(400, "bad json") from None
            target = (body.get("hash") or "").strip()
            if not target:
                raise UIError(400, "hash is required")
            res = ui.rollback_spell(
                sess, name=name, target_hash=target)
            self._send_json(200, {"ok": True, **res})

        def _api_project_export(self, sess: Session) -> None:
            zip_bytes = ui.export_project_zip(sess)
            self.send_response(200)
            self.send_header(
                "Content-Type", "application/zip")
            self.send_header(
                "Content-Length", str(len(zip_bytes)))
            self.send_header(
                "Content-Disposition",
                'attachment; filename="shabd-project.zip"')
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(zip_bytes)

        def _api_project_import(self, sess: Session) -> None:
            # We expect the zip in the raw body (not a multipart upload —
            # keeps the parser stdlib-friendly).
            data = self._read_body()
            if not data:
                raise UIError(400, "empty body — POST the zip raw")
            if len(data) > 32 * 1024 * 1024:
                raise UIError(413, "zip too large (32 MiB max)")
            overwrite = self.headers.get(
                "x-overwrite", "").lower() in ("1", "true", "yes")
            res = ui.import_project_zip(sess, data, overwrite)
            self._send_json(200, res)

        def _api_spells_share(self, sess: Session, name: str) -> None:
            res = ui.share_spell(sess, name)
            self._send_json(200, res)

        def _api_spells_import(self, sess: Session) -> None:
            try:
                body = json.loads(self._read_body() or b"{}")
            except json.JSONDecodeError:
                raise UIError(400, "bad json") from None
            res = ui.import_shared_spell(
                sess,
                share=body.get("share", ""),
                overwrite=bool(body.get("overwrite", False)))
            self._send_json(200, res)

        def _api_spells_suggest(self, sess: Session) -> None:
            try:
                body = json.loads(self._read_body() or b"{}")
            except json.JSONDecodeError:
                raise UIError(400, "bad json") from None
            res = ui.suggest_spell_source(
                sess,
                requirement=(body.get("requirement") or "").strip(),
                name_hint=(body.get("name_hint") or "").strip())
            self._send_json(200, res)

        def _api_llm_config_get(self, sess: Session) -> None:
            self._send_json(200, ui.get_llm_config(redact=True))

        def _api_llm_config_set(self, sess: Session) -> None:
            try:
                body = json.loads(self._read_body() or b"{}")
            except json.JSONDecodeError:
                raise UIError(400, "bad json") from None
            res = ui.set_llm_config(
                sess,
                backend=body.get("backend", "none"),
                base_url=body.get("base_url", ""),
                model=body.get("model", ""),
                api_key=body.get("api_key", ""))
            self._send_json(200, {"ok": True, **res})

        def _page_builder(self, sess: Session) -> None:
            body = """
            <div class="head"><h2>Spell Builder</h2></div>
            <div class="split">
              <div class="panel"><h3 id="form-title">New spell</h3>
                <label>Name (function name)</label>
                <input id="b-name" class="full" placeholder="add_numbers">
                <label style="margin-top:10px">Description</label>
                <input id="b-desc" class="full" placeholder="Add two integers">
                <label style="margin-top:10px">Required scopes (comma-sep)</label>
                <input id="b-scopes" class="full" placeholder="trader, payments">
                <label style="margin-top:10px">Tags (comma-sep)</label>
                <input id="b-tags" class="full" placeholder="math, demo">
                <label style="margin-top:10px">Python source</label>
                <textarea id="b-src" class="full" style="min-height:240px;font-family:ui-monospace,monospace"
                  placeholder="def add_numbers(a: int, b: int) -> int:&#10;    '''Add two integers.'''&#10;    return a + b"></textarea>

                <div style="margin-top:14px;padding:12px;background:var(--bg);border-radius:8px;border:1px dashed var(--panel2)">
                  <div style="font-size:12px;color:var(--accent);text-transform:uppercase;letter-spacing:1px">
                    ✨ AI suggestion (optional)
                  </div>
                  <p style="color:var(--dim);font-size:12px;margin:8px 0">
                    Don't know how to format the Python? Describe what
                    you want and the LLM (set in <a href="/settings" style="color:var(--accent)">Settings</a>)
                    will draft a spell that matches what the Builder expects.
                  </p>
                  <textarea id="b-req" class="full" style="min-height:60px"
                    placeholder="Example: a function that takes a price (float) and pct (float) and returns the discounted price as a dict."></textarea>
                  <div style="margin-top:10px">
                    <button class="ghost" onclick="suggest()" type="button">Generate suggestion</button>
                    <button class="ghost" onclick="acceptSuggestion()" type="button" id="accept-btn" style="display:none">Use this code →</button>
                    <span id="sug-msg" style="margin-left:10px"></span>
                  </div>
                  <pre id="sug-out" style="margin-top:10px;display:none;max-height:240px"></pre>
                </div>

                <div style="margin-top:14px">
                  <button id="b-submit" onclick="submitSpell()">Register spell</button>
                  <button id="b-cancel" class="ghost" onclick="resetForm()" style="display:none">Cancel</button>
                  <span id="b-msg" style="margin-left:14px"></span></div>
              </div>
              <div class="panel"><h3>UI-managed spells</h3>
                <table><thead><tr><th>Name</th><th>Source hash</th>
                  <th>Created by</th><th>Actions</th></tr></thead>
                  <tbody id="b-rows"></tbody></table>
                <p style="color:var(--dim);font-size:12px;margin-top:14px">
                  Spells declared with <code>@app.spell</code> in your
                  Python file are read-only — they live in code.
                </p>

                <div style="margin-top:18px;padding:12px;background:var(--bg);border-radius:8px;border:1px dashed var(--panel2)">
                  <div style="font-size:12px;color:var(--accent);text-transform:uppercase;letter-spacing:1px">
                    📦 Import a shared spell
                  </div>
                  <p style="color:var(--dim);font-size:12px;margin:8px 0">
                    Paste a <code>shabd-spell-v1:</code> string a colleague
                    shared with you.
                  </p>
                  <textarea id="imp-str" class="full" style="min-height:60px"
                    placeholder="shabd-spell-v1:eyJ2IjoxLCJuY..."></textarea>
                  <div style="margin-top:8px">
                    <label style="font-size:12px;color:var(--dim)"><input type="checkbox" id="imp-over"> overwrite if exists</label>
                  </div>
                  <div style="margin-top:8px">
                    <button onclick="importShared()">Import</button>
                    <span id="imp-msg" style="margin-left:10px"></span>
                  </div>
                </div>
              </div>
            </div>
            """
            script = """
            let MODE = 'create';       // 'create' or 'edit:<name>'

            function _csv(x){ return x.split(',').map(s=>s.trim()).filter(Boolean); }

            async function refresh() {
              const r = await api('/api/spells');
              const dyn = new Set(__DYN__);
              document.getElementById('b-rows').innerHTML =
                (r.body.spells||[]).map(s => {
                  const managed = dyn.has(s.name);
                  const hash = managed ? '<code>live</code>' : '<code>in code</code>';
                  const who = managed ? '(UI)' : '@app.spell';
                  const actions = managed
                    ? `<button class="ghost" onclick="viewSpell('${s.name}')">View</button>
                       <button class="ghost" onclick="editSpell('${s.name}')">Edit</button>
                       <button class="ghost" onclick="shareSpell('${s.name}')">Share</button>
                       <button class="danger" onclick="del('${s.name}')">Delete</button>`
                    : '<span class="tag info">code</span>';
                  return `<tr><td><strong>${s.name}</strong></td>
                    <td>${hash}</td><td>${who}</td><td>${actions}</td></tr>`;
                }).join('') || '<tr><td colspan="4" class="empty">No spells</td></tr>';
            }

            function resetForm() {
              MODE = 'create';
              document.getElementById('form-title').textContent = 'New spell';
              document.getElementById('b-submit').textContent = 'Register spell';
              document.getElementById('b-cancel').style.display = 'none';
              document.getElementById('b-name').readOnly = false;
              document.getElementById('b-name').value = '';
              document.getElementById('b-desc').value = '';
              document.getElementById('b-scopes').value = '';
              document.getElementById('b-tags').value = '';
              document.getElementById('b-src').value = '';
              document.getElementById('b-msg').textContent = '';
            }

            async function viewSpell(name) {
              const r = await api('/api/spells/'+encodeURIComponent(name)+'/source');
              if (r.status >= 400) {
                alert(r.body.error||'failed'); return;
              }
              alert('Spell: ' + r.body.name + '\\n' +
                    'Hash: ' + r.body.hash.slice(0,16) + '\\n' +
                    'Created by: ' + r.body.created_by + '\\n\\n' +
                    '--- Python source ---\\n\\n' +
                    r.body.source);
            }

            async function editSpell(name) {
              const r = await api('/api/spells/'+encodeURIComponent(name)+'/source');
              if (r.status >= 400) {
                alert(r.body.error||'failed'); return;
              }
              MODE = 'edit:' + name;
              document.getElementById('form-title').textContent = 'Edit ' + name;
              document.getElementById('b-submit').textContent = 'Save changes';
              document.getElementById('b-cancel').style.display = 'inline-block';
              document.getElementById('b-name').value = name;
              document.getElementById('b-name').readOnly = true;
              document.getElementById('b-desc').value = r.body.description || '';
              document.getElementById('b-scopes').value = (r.body.scopes||[]).join(', ');
              document.getElementById('b-tags').value = (r.body.tags||[]).join(', ');
              document.getElementById('b-src').value = r.body.source || '';
              window.scrollTo({top:0, behavior:'smooth'});
            }

            async function submitSpell() {
              const msg = document.getElementById('b-msg');
              msg.textContent = '…';
              const payload = {
                name: document.getElementById('b-name').value.trim(),
                description: document.getElementById('b-desc').value.trim(),
                scopes: _csv(document.getElementById('b-scopes').value),
                tags: _csv(document.getElementById('b-tags').value),
                source: document.getElementById('b-src').value,
              };
              let endpoint, method='POST';
              if (MODE.startsWith('edit:')) {
                const name = MODE.split(':',2)[1];
                endpoint = '/api/spells/'+encodeURIComponent(name)+'/update';
              } else {
                endpoint = '/api/spells/create';
              }
              const r = await api(endpoint, {method, body: payload});
              if (r.status >= 400 || !r.body.ok) {
                msg.innerHTML = '<span class="tag err">'+(r.body.error||'failed')+'</span>';
              } else {
                msg.innerHTML = '<span class="tag ok">Saved · hash '+r.body.source_hash+'</span>';
                if (MODE === 'create') { resetForm(); }
                refresh();
              }
            }

            async function del(name) {
              if (!confirm('Delete spell '+name+'?')) return;
              const r = await api('/api/spells/'+encodeURIComponent(name)+'/delete',
                                   {method:'POST'});
              if (r.body.ok) refresh();
              else alert(r.body.error||'failed');
            }

            async function shareSpell(name) {
              const r = await api('/api/spells/'+encodeURIComponent(name)+'/share');
              if (r.status >= 400) { alert(r.body.error||'failed'); return; }
              const s = r.body.share;
              try {
                await navigator.clipboard.writeText(s);
                alert('Share string copied to clipboard.\\n\\nSend it to your colleague — they paste it under "Import a shared spell" on their /builder page.\\n\\n' + s.slice(0, 80) + '…');
              } catch {
                prompt('Copy this share string:', s);
              }
            }

            async function importShared() {
              const s = document.getElementById('imp-str').value.trim();
              if (!s) { alert('Paste a share string first.'); return; }
              const msg = document.getElementById('imp-msg');
              msg.textContent = '…';
              const r = await api('/api/spells/import', {method:'POST', body:{
                share: s,
                overwrite: document.getElementById('imp-over').checked,
              }});
              if (r.status >= 400 || !r.body.ok) {
                msg.innerHTML = '<span class="tag err">'+(r.body.error||'failed')+'</span>';
              } else {
                msg.innerHTML = '<span class="tag ok">Imported · '+r.body.imported+' ('+r.body.mode+')</span>';
                document.getElementById('imp-str').value = '';
                refresh();
              }
            }

            async function suggest() {
              const req = document.getElementById('b-req').value.trim();
              if (!req) { alert('Describe what you want first.'); return; }
              const msg = document.getElementById('sug-msg');
              msg.innerHTML = '<span class="tag info">Thinking…</span>';
              const r = await api('/api/spells/suggest', {method:'POST', body:{
                requirement: req,
                name_hint: document.getElementById('b-name').value.trim(),
              }});
              const out = document.getElementById('sug-out');
              if (r.status >= 400) {
                msg.innerHTML = '<span class="tag err">'+(r.body.error||'failed')+'</span>';
                return;
              }
              out.style.display = 'block';
              out.textContent = r.body.source;
              document.getElementById('accept-btn').style.display = 'inline-block';
              msg.innerHTML = r.body.warning
                ? '<span class="tag warn">'+r.body.warning+'</span>'
                : '<span class="tag ok">via '+r.body.via+'</span>';
            }

            function acceptSuggestion() {
              const src = document.getElementById('sug-out').textContent;
              if (src) {
                document.getElementById('b-src').value = src;
                document.getElementById('sug-msg').innerHTML =
                  '<span class="tag ok">Copied into source ↑</span>';
              }
            }

            refresh();
            """.replace("__DYN__",
                        json.dumps(list(ui._dynamic_spells.keys())))
            self._send_html(200, "builder", body, sess, script)

        def _page_tokens(self, sess: Session) -> None:
            body = """
            <div class="head"><h2>Issue a bearer token</h2></div>
            <div class="split">
              <div class="panel"><h3>New token</h3>
                <label>Subject (who the token represents)</label>
                <input id="t-sub" class="full" placeholder="agent-bob, ci-bot, llm-worker-1">
                <label style="margin-top:10px">Scopes (comma-sep)</label>
                <input id="t-sc" class="full" placeholder="trader, kyc, payments">
                <label style="margin-top:10px">TTL (seconds — 60 to 604800)</label>
                <input id="t-ttl" class="full" type="number" value="3600" min="60" max="604800">
                <div style="margin-top:14px">
                  <button onclick="mint()">Issue</button></div>
                <div style="margin-top:18px">
                  <label>Token</label>
                  <pre id="t-out" style="user-select:all">—</pre>
                  <button class="ghost" onclick="copy()">Copy</button>
                </div>
              </div>
              <div class="panel"><h3>How to use</h3>
                <p style="color:var(--dim);font-size:13px">
                  Paste the token into any SHABD client (Python, curl, MCP).
                </p>
                <pre>curl -H "Authorization: Bearer &lt;token&gt;" \\
     -H "Idempotency-Key: $(uuidgen)" \\
     -d '{"a":1,"b":2}' \\
     http://localhost:8765/spells/add</pre>
                <pre>from shabd_client import SHABDClient
c = SHABDClient("http://localhost:8765", token="&lt;token&gt;")
c.cast("add", {"a":1,"b":2})</pre>
              </div>
            </div>

            <div class="panel"><h3>Issued tokens</h3>
              <p style="color:var(--dim);font-size:12px">
                Every token minted from this UI is recorded here.
                Revoking a token rejects it immediately, even if it
                hasn't expired yet.
              </p>
              <table><thead><tr>
                <th>Subject</th><th>Scopes</th><th>Issued</th>
                <th>Expires</th><th>Status</th><th>Action</th>
              </tr></thead><tbody id="t-rows"></tbody></table>
            </div>
            """
            script = """
            async function mint() {
              const scopes = document.getElementById('t-sc').value
                .split(',').map(s=>s.trim()).filter(Boolean);
              const r = await api('/api/tokens/issue', {method:'POST', body: {
                subject: document.getElementById('t-sub').value.trim(),
                scopes: scopes,
                ttl: Number(document.getElementById('t-ttl').value),
              }});
              if (r.status>=400 || !r.body.ok)
                document.getElementById('t-out').textContent = r.body.error || 'failed';
              else
                document.getElementById('t-out').textContent = r.body.token;
              loadList();
            }
            async function copy() {
              const t = document.getElementById('t-out').textContent;
              if (t && t !== '—') {
                await navigator.clipboard.writeText(t);
                alert('Token copied');
              }
            }
            async function loadList() {
              const r = await api('/api/tokens');
              const rows = (r.body.tokens||[]).map(t => {
                const status = t.revoked
                  ? '<span class="tag err">revoked</span>'
                  : t.expired
                    ? '<span class="tag warn">expired</span>'
                    : '<span class="tag ok">active</span>';
                const btn = (t.revoked || t.expired)
                  ? '<span style="color:var(--dim)">—</span>'
                  : `<button class="danger" onclick="revoke('${t.jti}')">Revoke</button>`;
                return `<tr>
                  <td><strong>${t.subject}</strong></td>
                  <td>${(t.scopes||[]).map(x=>'<span class="tag info">'+x+'</span>').join(' ')||'<i style="color:var(--dim)">none</i>'}</td>
                  <td>${new Date(t.issued_at*1000).toLocaleString()}</td>
                  <td>${new Date(t.exp*1000).toLocaleString()}</td>
                  <td>${status}</td>
                  <td>${btn}</td>
                </tr>`;
              }).join('');
              document.getElementById('t-rows').innerHTML = rows ||
                '<tr><td colspan="6" class="empty">No tokens issued yet</td></tr>';
            }
            async function revoke(jti) {
              if (!confirm('Revoke this token?')) return;
              const r = await api('/api/tokens/'+encodeURIComponent(jti)+'/revoke',
                                   {method:'POST'});
              if (r.body.ok) loadList(); else alert(r.body.error||'failed');
            }
            loadList();
            """
            self._send_html(200, "tokens", body, sess, script)

        def _page_scopes(self, sess: Session) -> None:
            body = """
            <div class="head"><h2>Scope management</h2></div>
            <div class="panel"><h3>Spells and their required scopes</h3>
              <table><thead><tr><th>Spell</th><th>Description</th>
                <th>Scopes</th><th>Source</th><th>Action</th></tr></thead>
                <tbody id="rows"></tbody></table>
              <p style="color:var(--dim);font-size:12px;margin-top:12px">
                Editing a scope here takes effect immediately — calls
                from tokens that don't carry the new scope will be
                rejected with <code>403</code>.
              </p>
            </div>
            """
            script = """
            async function load() {
              const r = await api('/api/scopes');
              const rows = r.body.spells || [];
              document.getElementById('rows').innerHTML = rows.map(s => `<tr>
                <td><strong>${s.name}</strong></td>
                <td style="color:var(--dim)">${s.description}</td>
                <td>
                  <input class="full" id="sc-${s.name}"
                         value="${(s.scopes||[]).join(', ')}"
                         placeholder="comma-separated">
                </td>
                <td>${s.managed_in_ui
                    ? '<span class="tag info">UI</span>'
                    : '<span class="tag warn">code</span>'}</td>
                <td><button onclick="save('${s.name}')">Save</button></td>
              </tr>`).join('') || '<tr><td colspan="5" class="empty">No spells</td></tr>';
            }
            async function save(name) {
              const raw = document.getElementById('sc-'+name).value;
              const sc = raw.split(',').map(s=>s.trim()).filter(Boolean);
              const r = await api('/api/scopes/'+encodeURIComponent(name),
                                   {method:'POST', body:{scopes: sc}});
              if (r.body.ok) {
                alert(name + ' scopes updated → ' + (r.body.scopes.join(', ') || '(none)'));
              } else {
                alert(r.body.error||'failed');
              }
            }
            load();
            """
            self._send_html(200, "scopes", body, sess, script)

        def _page_apidocs(self, sess: Session) -> None:
            body = """
            <div class="head"><h2>API Docs — every endpoint of this server</h2>
              <a class="btn" href="/openapi.json" download="openapi.json">⬇ openapi.json</a>
            </div>
            <div class="panel">
              <p style="color:var(--dim);font-size:13px">
                Everything below is live, generated from THIS running
                server. Download <code>openapi.json</code> and import it
                into Postman or Swagger UI. All examples use this
                server's address and a token you can mint on
                <a href="/tokens" style="color:var(--accent)">Issue Token</a>.
              </p>
              <div class="row">
                <div style="flex:4"><label>Test token (optional — paste to make examples runnable)</label>
                  <input id="d-tok" class="full" type="password" placeholder="ey..."></div>
              </div>
            </div>
            <div id="d-body"></div>
            """
            script = """
            const ORIGIN = location.origin;
            function authH(tok){ return tok ? '\\n     -H "Authorization: Bearer '+tok+'"' : ''; }

            async function load() {
              const tok = document.getElementById('d-tok').value.trim();
              const spec = (await api('/openapi.json')).body;
              const spells = (await api('/api/spells')).body.spells || [];
              const agentsRaw = (await api('/api/agents')).body.agents || [];
              const agents = agentsRaw.map(a => a.name || a);
              const intents = (await api('/api/intents')).body.intents || [];

              let html = '';

              // Core
              html += section('Core', [
                row('GET', '/healthz', 'Is the server alive?',
                    'curl '+ORIGIN+'/healthz'),
                row('GET', '/manifest', 'All tools (MCP / OpenAI format)',
                    'curl '+ORIGIN+'/manifest'),
                row('GET', '/openapi.json', 'This API as OpenAPI 3.0',
                    'curl '+ORIGIN+'/openapi.json'),
                row('GET', '/grimoire/verify', 'Verify the audit chain',
                    'curl '+ORIGIN+'/grimoire/verify'),
              ]);

              // Spells
              html += section('Tools (spells)', spells.map(s => {
                const scoped = (s.scopes||[]).length;
                const ex = Object.fromEntries(Object.entries(s.schema.properties||{})
                  .map(([k,p]) => [k, p.type==='integer'||p.type==='number'?1:'...']));
                return row('POST', '/spells/'+s.name,
                  (s.description||'')+(scoped?' (needs scope: '+s.scopes.join(',')+')':''),
                  'curl -X POST '+ORIGIN+'/spells/'+s.name+' \\\\'+
                  '\\n     -H "Content-Type: application/json"'+authH(tok)+' \\\\'+
                  "\\n     -d '"+JSON.stringify(ex)+"'");
              }));

              // Agents
              html += section('Agents', agents.map(a =>
                row('POST', '/query/'+a,
                  "Ask the '"+a+"' agent directly",
                  'curl -X POST '+ORIGIN+'/query/'+a+' \\\\'+
                  '\\n     -H "Content-Type: application/json"'+authH(tok)+' \\\\'+
                  "\\n     -d '"+JSON.stringify({question:'your question'})+"'")
              ));

              // Orchestrator
              if (intents.length) {
                html += section('Orchestrator', [
                  row('POST', '/ask',
                    'Send a question — orchestrator routes to the right agent',
                    'curl -X POST '+ORIGIN+'/ask \\\\'+
                    '\\n     -H "Content-Type: application/json"'+authH(tok)+' \\\\'+
                    "\\n     -d '"+JSON.stringify({question:'kal ki chuti chahiye'})+"'")
                ]);
              }

              document.getElementById('d-body').innerHTML = html;
            }

            function section(title, rows) {
              return '<div class="panel"><h3>'+title+'</h3>'+
                (rows.join('') || '<div class="empty">none</div>')+'</div>';
            }
            function row(method, path, desc, curl) {
              const cls = method==='GET'?'ok':'info';
              return '<div style="margin-bottom:16px;padding-bottom:14px;border-bottom:1px solid var(--panel2)">'+
                '<div><span class="tag '+cls+'">'+method+'</span> '+
                '<code style="font-size:14px">'+path+'</code></div>'+
                '<div style="color:var(--dim);font-size:12px;margin:6px 0">'+desc+'</div>'+
                '<pre style="margin:0">'+curl.replace(/</g,'&lt;')+'</pre></div>';
            }

            document.getElementById('d-tok').addEventListener('input', load);
            load();
            """
            self._send_html(200, "api-docs", body, sess, script)

        def _page_knowledge(self, sess: Session) -> None:
            body = """
            <div class="head"><h2>Knowledge Base — teach a tool from your documents</h2></div>
            <div class="split">
              <div class="panel"><h3>Create a knowledge base</h3>
                <p style="color:var(--dim);font-size:13px">
                  Paste text (policy docs, FAQs, manuals). It is chunked
                  and indexed with pure-stdlib TF-IDF retrieval — no
                  external vector database. Then click <b>Expose as
                  tool</b> and it becomes a spell <code>kb_&lt;name&gt;</code>
                  usable in Agent Lab, the Studio, the Orchestrator and
                  the API.
                </p>
                <label>Name</label>
                <input id="k-name" class="full" placeholder="hr_policy">
                <label style="margin-top:10px">Description</label>
                <input id="k-desc" class="full" placeholder="HR leave &amp; attendance policy">
                <div style="margin-top:12px">
                  <button onclick="createKb()">Create</button>
                  <span id="k-msg" style="margin-left:12px"></span>
                </div>
                <hr style="border:none;border-top:1px solid var(--line);margin:16px 0">
                <label>Add text to a knowledge base</label>
                <select id="k-target" class="full"></select>
                <label style="margin-top:10px">Source label (e.g. filename)</label>
                <input id="k-src" class="full" placeholder="leave_policy.txt">
                <label style="margin-top:10px">Text</label>
                <textarea id="k-text" class="full" style="min-height:140px"
                  placeholder="Paste document text here…"></textarea>
                <div style="margin-top:12px">
                  <button onclick="addText()">Add to KB</button>
                  <span id="k-add-msg" style="margin-left:12px"></span>
                </div>
              </div>
              <div class="panel"><h3>Your knowledge bases</h3>
                <div id="k-list" class="empty">None yet.</div>
                <hr style="border:none;border-top:1px solid var(--line);margin:16px 0">
                <h3>Test retrieval</h3>
                <div class="row">
                  <div style="flex:2"><label>KB</label>
                    <select id="k-qtarget" class="full"></select></div>
                  <div style="flex:4"><label>Question</label>
                    <input id="k-q" class="full" placeholder="how many casual leaves?"></div>
                  <div><button onclick="queryKb()">Search</button></div>
                </div>
                <pre id="k-hits" style="min-height:120px">Top matches appear here.</pre>
              </div>
            </div>
            """
            script = """
            async function load() {
              const r = await api('/api/kb');
              const kbs = r.body.kbs || [];
              const opts = kbs.map(k => '<option value="'+k.name+'">'+k.name+'</option>').join('');
              document.getElementById('k-target').innerHTML = opts || '<option value="">(create one first)</option>';
              document.getElementById('k-qtarget').innerHTML = opts || '<option value="">(none)</option>';
              const list = document.getElementById('k-list');
              if (!kbs.length) { list.innerHTML = '<div class="empty">No knowledge bases yet.</div>'; return; }
              list.innerHTML = kbs.map(k => `
                <div class="card" style="margin-bottom:10px">
                  <div><strong>${k.name}</strong>
                    <span class="tag info">${k.chunks} chunks</span>
                    ${k.exposed?'<span class="tag ok">tool: '+k.spell+'</span>':'<span class="tag warn">not exposed</span>'}</div>
                  <div style="color:var(--dim);font-size:12px;margin:4px 0">${k.description||''}</div>
                  <div style="color:var(--dim);font-size:11px">sources: ${(k.sources||[]).join(', ')||'—'}</div>
                  <div style="margin-top:8px">
                    ${k.exposed?'':'<button onclick="expose(\\''+k.name+'\\')">Expose as tool</button>'}
                    <button class="danger" onclick="delKb('${k.name}')">Delete</button>
                  </div>
                </div>`).join('');
            }
            async function createKb() {
              const r = await api('/api/kb/create', {method:'POST', body:{
                name: document.getElementById('k-name').value.trim(),
                description: document.getElementById('k-desc').value.trim()}});
              const m = document.getElementById('k-msg');
              if (r.status>=400||!r.body.ok) m.innerHTML='<span class="tag err">'+(r.body.error||'failed')+'</span>';
              else { m.innerHTML='<span class="tag ok">Created</span>'; document.getElementById('k-name').value=''; load(); }
            }
            async function addText() {
              const r = await api('/api/kb/add', {method:'POST', body:{
                name: document.getElementById('k-target').value,
                source: document.getElementById('k-src').value.trim()||'pasted',
                text: document.getElementById('k-text').value}});
              const m = document.getElementById('k-add-msg');
              if (r.status>=400||!r.body.ok) m.innerHTML='<span class="tag err">'+(r.body.error||'failed')+'</span>';
              else { m.innerHTML='<span class="tag ok">+'+r.body.chunks_added+' chunks ('+r.body.total_chunks+' total)</span>';
                     document.getElementById('k-text').value=''; load(); }
            }
            async function expose(name) {
              const r = await api('/api/kb/'+encodeURIComponent(name)+'/expose', {method:'POST'});
              if (r.body.ok) { alert('Exposed as tool: '+r.body.spell+'\\nNow available in Agent Lab, Studio, /manifest and the API.'); load(); }
              else alert(r.body.error||'failed');
            }
            async function delKb(name) {
              if (!confirm('Delete knowledge base '+name+'?')) return;
              const r = await api('/api/kb/'+encodeURIComponent(name)+'/delete', {method:'POST'});
              if (r.body.ok) load(); else alert(r.body.error||'failed');
            }
            async function queryKb() {
              const r = await api('/api/kb/query', {method:'POST', body:{
                name: document.getElementById('k-qtarget').value,
                question: document.getElementById('k-q').value}});
              const hits = r.body.hits || [];
              document.getElementById('k-hits').textContent = hits.length
                ? hits.map((h,i)=>'#'+(i+1)+'  ('+h.score+')  ['+h.source+']\\n'+h.text).join('\\n\\n———\\n\\n')
                : 'No matches.';
            }
            load();
            """
            self._send_html(200, "knowledge", body, sess, script)

        def _page_nova(self, sess: Session) -> None:
            body = """
            <div class="head"><h2>Nova — external RAG pipelines, driven from here</h2></div>
            <div class="panel" style="background:#faf6ee;border-left:4px solid var(--accent)">
              <p style="margin:0;font-size:13px;color:var(--dim)">
                Nova is an <strong>external multi-tenant RAG service</strong>
                (Tenants → Pipelines → Ingest → Query). SHABD does not run
                it — it drives the service through its API. Create tenants
                and pipelines, ingest text, query, and <strong>Expose a
                pipeline as a tool</strong> so agents and chatbots can use
                it.
              </p>
            </div>

            <div class="panel"><h3>1 · Connect the Nova service</h3>
              <div class="row">
                <div style="flex:4"><label>Base URL</label>
                  <input id="n-url" class="full" placeholder="http://localhost:8000"></div>
                <div style="flex:3"><label>API key (optional)</label>
                  <input id="n-key" class="full" type="password"></div>
                <div><label>Auth</label>
                  <select id="n-auth" class="full">
                    <option value="bearer">Bearer</option>
                    <option value="x-api-key">X-API-Key</option>
                    <option value="none">None</option>
                  </select></div>
                <div><button onclick="saveCfg()">Save</button></div>
              </div>
              <div id="n-cfg" style="color:var(--dim);font-size:12px"></div>
            </div>

            <div class="split">
              <div class="panel"><h3>2 · Tenants</h3>
                <div class="row">
                  <div style="flex:3"><input id="n-tname" class="full" placeholder="Tenant name (e.g. TCS_CCIL)"></div>
                  <div><button onclick="createTenant()">Create</button></div>
                </div>
                <label>Active tenant</label>
                <select id="n-tenant" class="full" onchange="loadPipes()"></select>
                <div id="n-tmsg" style="margin-top:6px"></div>
              </div>
              <div class="panel"><h3>3 · Create a pipeline</h3>
                <label>Name</label>
                <input id="p-name" class="full" placeholder="Doc Pipeline">
                <details style="margin-top:8px">
                  <summary style="cursor:pointer;color:var(--dim);font-size:12px">Config (embedding · chunking · cleaning · retrieval · vector)</summary>
                  <div class="row" style="margin-top:6px">
                    <div><label>Embed base URL</label><input id="p-eburl" class="full" placeholder="http://localhost:11434"></div>
                    <div><label>Embed model</label><input id="p-emodel" class="full" value="nomic-embed-text:v1.5"></div>
                  </div>
                  <div class="row" style="margin:0">
                    <div><label>Embedding dim</label><input id="p-edim" class="full" type="number" value="768"></div>
                    <div><label>Embed API key</label><input id="p-ekey" class="full" type="password" placeholder="blank for Ollama"></div>
                  </div>
                  <div class="row" style="margin:0">
                    <div><label>Chunk strategy</label>
                      <select id="p-cstrat" class="full">
                        <option value="recursive">recursive</option>
                        <option value="paragraph">paragraph</option>
                        <option value="sentence">sentence</option>
                        <option value="fixed_size">fixed_size</option>
                        <option value="sliding_window">sliding_window</option>
                      </select></div>
                    <div><label>Chunk size</label><input id="p-csize" class="full" type="number" value="500"></div>
                    <div><label>Overlap</label><input id="p-cover" class="full" type="number" value="50"></div>
                  </div>
                  <div class="row" style="margin:0">
                    <div><label>Clean profile</label>
                      <select id="p-clean" class="full">
                        <option value="light">light</option>
                        <option value="aggressive">aggressive</option>
                        <option value="none">none</option>
                      </select></div>
                    <div><label>Retriever</label>
                      <select id="p-ret" class="full">
                        <option value="similarity">similarity</option>
                        <option value="mmr">mmr</option>
                        <option value="hybrid">hybrid</option>
                        <option value="threshold">threshold</option>
                        <option value="ensemble">ensemble</option>
                      </select></div>
                    <div><label>top_k</label><input id="p-topk" class="full" type="number" value="5"></div>
                  </div>
                  <label style="margin-top:6px">Vector backend</label>
                  <select id="p-vb" class="full">
                    <option value="pgvector">PostgreSQL + pgvector (default)</option>
                    <option value="qdrant">Qdrant</option>
                    <option value="chroma">Chroma</option>
                  </select>
                  <label style="margin-top:6px">Postgres DSN (optional)</label>
                  <input id="p-dsn" class="full" placeholder="postgresql://user:pass@host:5432/db">
                </details>
                <div style="margin-top:10px">
                  <button onclick="createPipe()">Create pipeline</button>
                  <span id="p-msg" style="margin-left:10px"></span>
                </div>
              </div>
            </div>

            <div class="panel"><h3>4 · Pipelines</h3>
              <div id="n-pipes" class="empty">Connect a service and pick a tenant.</div>
            </div>

            <div class="split">
              <div class="panel"><h3>5 · Ingest documents</h3>
                <label>Pipeline</label>
                <select id="i-pipe" class="full"></select>
                <label style="margin-top:8px">Upload a file (PDF / DOCX / TXT / MD)</label>
                <input id="i-file" type="file" class="full"
                       accept=".pdf,.docx,.doc,.txt,.md">
                <div style="margin-top:8px">
                  <button onclick="uploadFile()">Upload &amp; ingest</button>
                  <span id="i-fmsg" style="margin-left:10px"></span>
                </div>
                <div style="margin:14px 0;text-align:center;color:var(--dim);font-size:12px">— or paste text —</div>
                <label>Filename</label>
                <input id="i-fn" class="full" placeholder="policy.txt">
                <label style="margin-top:8px">Text</label>
                <textarea id="i-text" class="full" style="min-height:100px" placeholder="Paste document text…"></textarea>
                <div style="margin-top:10px">
                  <button onclick="ingest()">Ingest text</button>
                  <span id="i-msg" style="margin-left:10px"></span>
                </div>
              </div>
              <div class="panel"><h3>6 · Query Playground</h3>
                <div class="row">
                  <div style="flex:3"><label>Pipeline</label>
                    <select id="q-pipe" class="full"></select></div>
                  <div><label>top_k</label><input id="q-topk" class="full" type="number" value="5"></div>
                </div>
                <div id="q-chat" style="border:1px solid var(--line);border-radius:10px;
                     background:var(--panel);min-height:200px;max-height:340px;overflow-y:auto;
                     padding:12px;display:flex;flex-direction:column;gap:10px;margin-top:6px">
                  <div class="empty">Ask a question…</div>
                </div>
                <div class="row" style="margin-top:6px;align-items:center">
                  <div style="flex:6"><input id="q-in" class="full"
                    placeholder="What are you looking for?"
                    onkeydown="if(event.key==='Enter')runQuery()"></div>
                  <div><button onclick="runQuery()">Run Query</button></div>
                </div>
              </div>
            </div>
            """
            script = """
            function mdEsc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}

            async function loadCfg() {
              const r = await api('/api/nova/config');
              const c = r.body||{};
              document.getElementById('n-cfg').innerHTML = c.configured
                ? '<span class="tag ok">connected</span> '+c.base_url
                : '<span class="tag warn">not configured</span>';
              if (c.base_url) document.getElementById('n-url').value = c.base_url;
              if (c.auth_style) document.getElementById('n-auth').value = c.auth_style;
              if (c.configured) loadTenants();
            }
            async function saveCfg() {
              const r = await api('/api/nova/config', {method:'POST', body:{
                base_url: document.getElementById('n-url').value.trim(),
                api_key: document.getElementById('n-key').value,
                auth_style: document.getElementById('n-auth').value}});
              if (r.status>=400||!r.body.ok) alert(r.body.error||'failed');
              else loadCfg();
            }
            async function loadTenants() {
              const r = await api('/api/nova/tenants');
              const sel = document.getElementById('n-tenant');
              if (r.status>=400) { document.getElementById('n-tmsg').innerHTML='<span class="tag err">'+(r.body.error||'failed')+'</span>'; return; }
              const ts = r.body.tenants||[];
              sel.innerHTML = ts.map(t=>'<option value="'+t.id+'">'+t.name+'</option>').join('') || '<option value="">(no tenants)</option>';
              loadPipes();
            }
            async function createTenant() {
              const r = await api('/api/nova/tenants', {method:'POST', body:{name: document.getElementById('n-tname').value.trim()}});
              const m = document.getElementById('n-tmsg');
              if (r.status>=400||!r.body.ok) m.innerHTML='<span class="tag err">'+(r.body.error||'failed')+'</span>';
              else { m.innerHTML='<span class="tag ok">Created</span>'; document.getElementById('n-tname').value=''; loadTenants(); }
            }
            function tenantId(){ return document.getElementById('n-tenant').value; }
            async function createPipe() {
              const tid = tenantId();
              if (!tid) { alert('Create/pick a tenant first'); return; }
              const config = {
                embed_base_url: document.getElementById('p-eburl').value.trim(),
                embed_model: document.getElementById('p-emodel').value.trim(),
                embed_api_key: document.getElementById('p-ekey').value,
                embedding_dim: Number(document.getElementById('p-edim').value)||768,
                chunk_strategy: document.getElementById('p-cstrat').value,
                chunk_size: Number(document.getElementById('p-csize').value)||500,
                chunk_overlap: Number(document.getElementById('p-cover').value)||50,
                cleaning: { clean_profile: document.getElementById('p-clean').value },
                retriever: document.getElementById('p-ret').value,
                top_k: Number(document.getElementById('p-topk').value)||5,
                vector_backend: { backend: document.getElementById('p-vb').value }
              };
              const dsn = document.getElementById('p-dsn').value.trim();
              if (dsn) config.vector_backend.pg_dsn = dsn;
              const r = await api('/api/nova/pipelines', {method:'POST', body:{
                tenant_id: tid,
                name: document.getElementById('p-name').value.trim(),
                config}});
              const m = document.getElementById('p-msg');
              if (r.status>=400||!r.body.ok) m.innerHTML='<span class="tag err">'+(r.body.error||'failed')+'</span>';
              else { m.innerHTML='<span class="tag ok">Created</span>'; document.getElementById('p-name').value=''; loadPipes(); }
            }
            let PIPES = [];
            async function loadPipes() {
              const tid = tenantId();
              const r = await api('/api/nova/pipelines?tenant_id='+encodeURIComponent(tid||''));
              const box = document.getElementById('n-pipes');
              if (r.status>=400) { box.innerHTML='<div class="empty">'+(r.body.error||'failed')+'</div>'; return; }
              PIPES = r.body.pipelines||[];
              const exposed = r.body.exposed||{};
              // fill selects
              const opts = PIPES.map(p=>'<option value="'+p.id+'">'+p.name+'</option>').join('');
              document.getElementById('i-pipe').innerHTML = opts;
              document.getElementById('q-pipe').innerHTML = opts;
              if (!PIPES.length) { box.innerHTML='<div class="empty">No pipelines in this tenant.</div>'; return; }
              box.innerHTML = PIPES.map(p => `
                <div class="card" style="margin-bottom:10px">
                  <div><strong>${p.name}</strong>
                    ${exposed[p.id]?'<span class="tag ok">tool: '+exposed[p.id]+'</span>':''}
                    <span class="tag info">${(p.config&&p.config.embed_model)||''}</span></div>
                  <div style="color:var(--dim);font-size:12px">${p.description||''}
                    · docs ${p.document_count||0} · chunks ${p.chunk_count||0}</div>
                  <div style="margin-top:8px">
                    ${exposed[p.id]?'':'<button onclick="expose(\\''+p.id+'\\',\\''+p.name+'\\')">Expose as tool</button>'}
                    <button class="danger" onclick="delPipe('${p.id}')">Delete</button>
                  </div>
                </div>`).join('');
            }
            async function expose(pid, name) {
              const r = await api('/api/nova/expose', {method:'POST', body:{pipeline_id:pid, name}});
              if (r.body.ok) { alert('Exposed as tool: '+r.body.spell+'\\nUsable in Agent Lab, Studio, /manifest and the API.'); loadPipes(); }
              else alert(r.body.error||'failed');
            }
            async function delPipe(pid) {
              if (!confirm('Delete pipeline?')) return;
              const r = await api('/api/nova/pipelines/'+encodeURIComponent(pid)+'/delete', {method:'POST'});
              if (r.body.ok) loadPipes(); else alert(r.body.error||'failed');
            }
            async function ingest() {
              const m = document.getElementById('i-msg'); m.textContent='…';
              const r = await api('/api/nova/ingest', {method:'POST', body:{
                pipeline_id: document.getElementById('i-pipe').value,
                filename: document.getElementById('i-fn').value.trim(),
                text: document.getElementById('i-text').value}});
              if (r.status>=400||!r.body.ok) m.innerHTML='<span class="tag err">'+(r.body.error||'failed')+'</span>';
              else { m.innerHTML='<span class="tag ok">'+(r.body.status||'ok')+' · '+(r.body.chunks_indexed||0)+' chunks</span>'; document.getElementById('i-text').value=''; loadPipes(); }
            }
            async function uploadFile() {
              const f = document.getElementById('i-file').files[0];
              const m = document.getElementById('i-fmsg');
              if (!f) { alert('Pick a file first'); return; }
              if (f.size > 32*1024*1024) { m.innerHTML='<span class="tag err">file &gt; 32 MiB</span>'; return; }
              m.innerHTML='<span class="tag info">uploading '+f.name+'…</span>';
              const buf = await f.arrayBuffer();
              // base64 encode
              let bin=''; const bytes=new Uint8Array(buf);
              for (let i=0;i<bytes.length;i++) bin+=String.fromCharCode(bytes[i]);
              const b64 = btoa(bin);
              const r = await api('/api/nova/ingest', {method:'POST', body:{
                pipeline_id: document.getElementById('i-pipe').value,
                filename: f.name,
                content_type: f.type || 'application/octet-stream',
                content_b64: b64}});
              if (r.status>=400||!r.body.ok) m.innerHTML='<span class="tag err">'+(r.body.error||'failed')+'</span>';
              else { m.innerHTML='<span class="tag ok">'+(r.body.status||'ok')+' · '+(r.body.chunks_indexed||0)+' chunks</span>'; document.getElementById('i-file').value=''; loadPipes(); }
            }
            function qb(role, html) {
              const box=document.getElementById('q-chat'); const e=box.querySelector('.empty'); if(e)e.remove();
              const d=document.createElement('div');
              const mine=role==='user';
              d.style.cssText='max-width:85%;padding:9px 13px;border-radius:12px;font-size:13px;'+(mine?'align-self:flex-end;background:var(--accent);color:#fff':'align-self:flex-start;background:var(--panel2);border:1px solid var(--line)');
              if (mine) d.textContent=html; else d.innerHTML=html;
              box.appendChild(d); box.scrollTop=box.scrollHeight; return d;
            }
            async function runQuery() {
              const pid = document.getElementById('q-pipe').value;
              const q = document.getElementById('q-in').value.trim();
              if (!pid) { alert('Pick a pipeline'); return; }
              if (!q) return;
              document.getElementById('q-in').value='';
              qb('user', q);
              const think = qb('bot', '<span style="color:var(--dim)">…searching</span>');
              const r = await api('/api/nova/query', {method:'POST', body:{
                pipeline_id: pid, question: q, top_k: Number(document.getElementById('q-topk').value)||5}});
              if (r.status>=400||!r.body.ok) { think.innerHTML='<span style="color:var(--err)">'+(r.body.error||'failed')+'</span>'; return; }
              const results = r.body.results||[];
              if (!results.length) { think.innerHTML='<span style="color:var(--dim)">No results.</span>'; return; }
              think.innerHTML = results.map((x,i)=>'<div style="margin-bottom:8px"><b>#'+(i+1)+'</b> ('+(x.score||0).toFixed(3)+') '+(x.filename?'['+mdEsc(x.filename)+']':'')+'<br>'+mdEsc((x.text||'').slice(0,300))+'…</div>').join('');
            }
            loadCfg();
            """
            self._send_html(200, "nova", body, sess, script)

        def _page_sqlsvc(self, sess: Session) -> None:
            body = """
            <div class="head"><h2>SQL Intelligence — connect an external service, expose it as a tool</h2></div>
            <div class="panel" style="background:#faf6ee;border-left:4px solid var(--accent)">
              <p style="margin:0;font-size:13px;color:var(--dim)">
                SHABD does <strong>not</strong> run the SQL / text-to-SQL
                engine. You point it at an <strong>external service</strong>
                (its own database, schemas, models) through that service's
                HTTP API. Fill in the URL + key below, test it, then
                <strong>Expose as tool</strong> — exactly like a Knowledge
                Base. The tool then works in Agent Lab, the Studio, the
                Orchestrator and the API.
              </p>
            </div>
            <div class="split">
              <div class="panel"><h3>Connect a service</h3>
                <label>Name</label>
                <input id="s-name" class="full" placeholder="ccil_sql">
                <label style="margin-top:10px">Description</label>
                <input id="s-desc" class="full" placeholder="CCIL text-to-SQL over collateral data">
                <label style="margin-top:10px">Base URL</label>
                <input id="s-url" class="full" placeholder="http://172.19.204.25:8002">
                <label style="margin-top:10px">API key (optional)</label>
                <input id="s-key" class="full" type="password" placeholder="leave blank if the service is open">
                <label style="margin-top:10px">Auth style</label>
                <select id="s-auth" class="full">
                  <option value="bearer">Authorization: Bearer &lt;key&gt;</option>
                  <option value="x-api-key">X-API-Key: &lt;key&gt;</option>
                  <option value="x-user-id">X-User-Id: &lt;key&gt;</option>
                  <option value="none">No auth</option>
                </select>
                <details style="margin-top:10px">
                  <summary style="cursor:pointer;color:var(--dim);font-size:12px">Advanced (endpoint, field mapping &amp; optional query params)</summary>
                  <label style="margin-top:8px">Ask endpoint path</label>
                  <input id="s-path" class="full" value="/query/ask">
                  <label style="margin-top:8px">Request query field</label>
                  <input id="s-qf" class="full" value="query">
                  <label style="margin-top:8px">Response answer field</label>
                  <input id="s-af" class="full" value="answer">
                  <div style="margin-top:10px;font-size:11px;color:var(--accent);text-transform:uppercase;letter-spacing:1px">
                    Optional /query/ask body params
                  </div>
                  <div class="row" style="margin-top:6px">
                    <div><label>top_k</label>
                      <input id="s-topk" class="full" type="number" placeholder="e.g. 10"></div>
                    <div><label>platform</label>
                      <input id="s-plat" class="full" placeholder="web"></div>
                  </div>
                  <div class="row" style="margin:0">
                    <div><label>collection</label>
                      <input id="s-coll" class="full" placeholder="optional"></div>
                    <div><label>table</label>
                      <input id="s-tab" class="full" placeholder="optional"></div>
                  </div>
                  <p style="color:var(--dim);font-size:11px;margin-top:6px">
                    These are sent in every /query/ask body if filled.
                    <code>thread_id</code> is managed automatically per
                    chat so the service keeps conversation context.
                  </p>
                </details>
                <div style="margin-top:12px">
                  <button onclick="createSvc()">Save connection</button>
                  <span id="s-msg" style="margin-left:12px"></span>
                </div>
                <p style="color:var(--dim);font-size:12px;margin-top:10px">
                  Defaults match the standard <code>POST /query/ask</code>
                  shape: body <code>{"query": "..."}</code> → response
                  <code>{"answer": "..."}</code>. Change them in Advanced
                  if your service differs.
                </p>
              </div>
              <div class="panel"><h3>Connected services</h3>
                <div id="s-list" class="empty">None yet.</div>
              </div>
            </div>
            <div class="panel"><h3>Live Chat — test the service (keeps history)</h3>
              <div class="row" style="align-items:center">
                <div style="flex:2"><label>Service</label>
                  <select id="s-qtarget" class="full"></select></div>
                <div style="flex:0 0 auto;padding-bottom:2px">
                  <button class="ghost" onclick="clearChat()">Clear chat</button></div>
              </div>
              <div id="s-chat" style="border:1px solid var(--line);border-radius:10px;
                   background:var(--panel);min-height:260px;max-height:420px;overflow-y:auto;
                   padding:12px;display:flex;flex-direction:column;gap:10px;margin-top:8px">
                <div class="empty">Ask a question about your data…</div>
              </div>
              <div class="row" style="margin-top:8px;align-items:center">
                <div style="flex:6"><input id="s-q" class="full"
                     placeholder="Show all transactions settled by CCIL"
                     onkeydown="if(event.key==='Enter')sendChat()"></div>
                <div><button onclick="sendChat()">Send</button></div>
              </div>
            </div>
            """
            script = """
            async function load() {
              const r = await api('/api/sqlsvc');
              const svcs = r.body.services || [];
              document.getElementById('s-qtarget').innerHTML =
                svcs.map(s=>'<option value="'+s.name+'">'+s.name+'</option>').join('')
                || '<option value="">(connect one first)</option>';
              const list = document.getElementById('s-list');
              if (!svcs.length) { list.innerHTML='<div class="empty">No services connected yet.</div>'; return; }
              list.innerHTML = svcs.map(s => `
                <div class="card" style="margin-bottom:10px">
                  <div><strong>${s.name}</strong>
                    ${s.exposed?'<span class="tag ok">tool: '+s.spell+'</span>':'<span class="tag warn">not exposed</span>'}</div>
                  <div style="color:var(--dim);font-size:12px;margin:4px 0">${s.description||''}</div>
                  <div style="color:var(--dim);font-size:11px;font-family:ui-monospace,monospace">
                    ${s.base_url}${s.ask_path} · ${s.auth_style}${s.has_key?' (key set)':''}</div>
                  <div style="margin-top:8px">
                    ${s.exposed?'':'<button onclick="expose(\\''+s.name+'\\')">Expose as tool</button>'}
                    <button class="danger" onclick="delSvc('${s.name}')">Delete</button>
                  </div>
                </div>`).join('');
            }
            async function createSvc() {
              const r = await api('/api/sqlsvc/create', {method:'POST', body:{
                name: document.getElementById('s-name').value.trim(),
                description: document.getElementById('s-desc').value.trim(),
                base_url: document.getElementById('s-url').value.trim(),
                api_key: document.getElementById('s-key').value,
                auth_style: document.getElementById('s-auth').value,
                ask_path: document.getElementById('s-path').value.trim(),
                query_field: document.getElementById('s-qf').value.trim(),
                answer_field: document.getElementById('s-af').value.trim(),
                top_k: document.getElementById('s-topk').value,
                platform: document.getElementById('s-plat').value.trim(),
                collection: document.getElementById('s-coll').value.trim(),
                table: document.getElementById('s-tab').value.trim(),
              }});
              const m = document.getElementById('s-msg');
              if (r.status>=400||!r.body.ok) m.innerHTML='<span class="tag err">'+(r.body.error||'failed')+'</span>';
              else { m.innerHTML='<span class="tag ok">Saved</span>'; document.getElementById('s-name').value=''; document.getElementById('s-url').value=''; load(); }
            }
            async function expose(name) {
              const r = await api('/api/sqlsvc/'+encodeURIComponent(name)+'/expose', {method:'POST'});
              if (r.body.ok) { alert('Exposed as tool: '+r.body.spell+'\\nUsable in Agent Lab, Studio, /manifest and the API.'); load(); }
              else alert(r.body.error||'failed');
            }
            async function delSvc(name) {
              if (!confirm('Delete service '+name+'?')) return;
              const r = await api('/api/sqlsvc/'+encodeURIComponent(name)+'/delete', {method:'POST'});
              if (r.body.ok) load(); else alert(r.body.error||'failed');
            }

            // ---- markdown (safe) ----
            function mdEsc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
            function md(s){var B=[];s=String(s);
              s=s.replace(/```([\\s\\S]*?)```/g,function(m,c){B.push(c.replace(/^\\n/,''));return 'CBLK'+(B.length-1)+'KBLC';});
              s=mdEsc(s);
              s=s.replace(/`([^`]+)`/g,'<code>$1</code>');
              s=s.replace(/\\*\\*([^*]+)\\*\\*/g,'<strong>$1</strong>');
              s=s.replace(/^#{1,3} (.*)$/gm,'<b>$1</b>');
              s=s.replace(/^\\s*[-*] (.*)$/gm,'<li>$1</li>');
              s=s.replace(/(?:<li>.*?<\\/li>\\n?)+/g,function(m){return '<ul>'+m.replace(/\\n/g,'')+'</ul>';});
              s=s.replace(/\\n/g,'<br>');
              s=s.replace(/CBLK(\\d+)KBLC/g,function(m,i){return '<pre style="background:#2d2b26;color:#f0ede4;padding:8px 10px;border-radius:8px;overflow-x:auto;white-space:pre-wrap;margin:6px 0">'+mdEsc(B[i])+'</pre>';});
              return s;}

            // ---- chat ----
            let threadId = null;
            function chatBubble(role, html) {
              const box = document.getElementById('s-chat');
              const e = box.querySelector('.empty'); if (e) e.remove();
              const d = document.createElement('div');
              const mine = role==='user';
              d.style.cssText = 'max-width:82%;padding:9px 13px;border-radius:12px;font-size:13px;'+
                (mine?'align-self:flex-end;background:var(--accent);color:#fff':'align-self:flex-start;background:var(--panel2);border:1px solid var(--line)');
              if (mine) d.textContent = html; else d.innerHTML = html;
              box.appendChild(d); box.scrollTop = box.scrollHeight;
              return d;
            }
            function clearChat() {
              threadId = null;
              document.getElementById('s-chat').innerHTML = '<div class="empty">Ask a question about your data…</div>';
            }
            async function sendChat() {
              const svc = document.getElementById('s-qtarget').value;
              const inp = document.getElementById('s-q');
              const q = inp.value.trim();
              if (!svc) { alert('Connect a service first'); return; }
              if (!q) return;
              inp.value = '';
              chatBubble('user', q);
              const thinking = chatBubble('bot', '<span style="color:var(--dim)">…thinking</span>');
              const r = await api('/api/sqlsvc/test', {method:'POST', body:{
                name: svc, question: q, thread_id: threadId }});
              if (r.status>=400 || !r.body.ok) {
                thinking.innerHTML = '<span style="color:var(--err)">'+(r.body.error||'failed')+'</span>';
                return;
              }
              if (r.body.thread_id) threadId = r.body.thread_id;
              const ans = typeof r.body.answer==='string' ? r.body.answer : JSON.stringify(r.body.answer, null, 2);
              let html = md(ans);
              if (r.body.sources) html += '<div style="margin-top:6px;color:var(--dim);font-size:11px">'+(Array.isArray(r.body.sources)?r.body.sources.length+' sources':'')+'</div>';
              thinking.innerHTML = html;
            }
            load();
            """
            self._send_html(200, "sql-intelligence", body, sess, script)

        def _page_chains(self, sess: Session) -> None:
            body = """
            <div class="head"><h2>Spell Chains — pipe spells together (no LLM)</h2></div>
            <div class="split">
              <div class="panel"><h3>Build a chain</h3>
                <p style="color:var(--dim);font-size:13px">
                  A chain runs spells in a fixed order: the output of one
                  becomes the input of the next. Deterministic, fast, no
                  LLM. The chain itself becomes a callable spell — you can
                  invoke it, give it to an agent, or expose it as an API.
                </p>
                <label>Chain name</label>
                <input id="c-name" class="full" placeholder="kyc_then_score">
                <label style="margin-top:10px">Description</label>
                <input id="c-desc" class="full" placeholder="Run KYC, then risk-score the result">
                <label style="margin-top:10px">Steps (in order)</label>
                <div id="c-steps"></div>
                <button class="ghost" onclick="addStep()" style="margin-top:8px">+ Add step</button>
                <div style="margin-top:14px">
                  <button onclick="createChain()">Create chain</button>
                  <span id="c-msg" style="margin-left:12px"></span>
                </div>
                <div id="c-preview" style="margin-top:10px;color:var(--dim);font-size:13px"></div>
              </div>
              <div class="panel"><h3>Existing chains</h3>
                <div id="c-list" class="empty">None yet.</div>
              </div>
            </div>
            <div class="panel">
              <h3>How a chain passes data</h3>
              <ul style="color:var(--dim);font-size:13px;line-height:1.7">
                <li>If a step returns a <strong>dict</strong>, its keys
                    become the next step's named arguments.</li>
                <li>If it returns anything else, that value goes into the
                    next step's first parameter.</li>
                <li>The chain's input schema is the <strong>first
                    step's</strong> schema — call it like any spell.</li>
              </ul>
            </div>
            """
            script = """
            let SPELLS = [];

            async function load() {
              const r = await api('/api/chains');
              SPELLS = r.body.spells || [];
              if (!document.querySelector('#c-steps select')) {
                addStep(); addStep();   // start with two step rows
              }
              renderChains(r.body.chains || []);
            }

            function stepOptions(sel) {
              return '<option value="">— pick a spell —</option>' +
                SPELLS.map(s => '<option value="'+s+'"'+
                  (s===sel?' selected':'')+'>'+s+'</option>').join('');
            }
            function addStep(val) {
              const div = document.createElement('div');
              div.className = 'row';
              div.style.margin = '6px 0';
              div.innerHTML =
                '<div style="flex:5"><select class="full step">'+stepOptions(val)+'</select></div>'+
                '<div style="flex:0 0 auto"><button class="ghost" type="button" onclick="this.closest(\\'.row\\').remove();preview()">✕</button></div>';
              document.getElementById('c-steps').appendChild(div);
              div.querySelector('select').addEventListener('change', preview);
              preview();
            }
            function currentSteps() {
              return Array.from(document.querySelectorAll('#c-steps select.step'))
                .map(s => s.value).filter(Boolean);
            }
            function preview() {
              const s = currentSteps();
              document.getElementById('c-preview').textContent =
                s.length ? 'Pipeline:  ' + s.join('  →  ') : '';
            }
            async function createChain() {
              const steps = currentSteps();
              const msg = document.getElementById('c-msg');
              const r = await api('/api/chains/create', {method:'POST', body:{
                name: document.getElementById('c-name').value.trim(),
                description: document.getElementById('c-desc').value.trim(),
                steps: steps,
              }});
              if (r.status>=400 || !r.body.ok)
                msg.innerHTML = '<span class="tag err">'+(r.body.error||'failed')+'</span>';
              else { msg.innerHTML = '<span class="tag ok">Created</span>'; load(); }
            }
            function renderChains(list) {
              const el = document.getElementById('c-list');
              if (!list.length) { el.innerHTML = '<div class="empty">No chains yet.</div>'; return; }
              el.innerHTML = list.map(c => `
                <div class="card" style="margin-bottom:10px">
                  <div><strong>${c.name}</strong>
                    ${c.live?'<span class="tag ok">live</span>':'<span class="tag err">broken</span>'}</div>
                  <div style="color:var(--dim);font-size:12px;margin:4px 0">${c.description||''}</div>
                  <div style="font-family:ui-monospace,monospace;font-size:12px">
                    ${(c.steps||[]).join(' &rarr; ')}</div>
                  <div style="margin-top:8px">
                    <button class="ghost" onclick="location.href='/spells'">Test in Spells</button>
                    <button class="danger" onclick="delChain('${c.name}')">Delete</button>
                  </div>
                </div>`).join('');
            }
            async function delChain(name) {
              if (!confirm('Delete chain '+name+'?')) return;
              const r = await api('/api/chains/'+encodeURIComponent(name)+'/delete',
                                   {method:'POST'});
              if (r.body.ok) load(); else alert(r.body.error||'failed');
            }
            load();
            """
            self._send_html(200, "chains", body, sess, script)

        def _page_sources(self, sess: Session) -> None:
            body = """
            <div class="head"><h2>Tool Sources — bring in tools from other servers</h2></div>
            <div class="split">
              <div class="panel"><h3>Connect a source</h3>
                <p style="color:var(--dim);font-size:13px">
                  Import tools from an external <strong>MCP server</strong>
                  (Anthropic protocol) or another <strong>SHABD server</strong>.
                  Imported tools become local spells named
                  <code>&lt;source&gt;__&lt;tool&gt;</code> — they then show up
                  in the Agent Lab tool picker, the Spells page and your
                  /manifest automatically.
                </p>
                <label>Source name (short id)</label>
                <input id="s-name" class="full" placeholder="partner-bank">
                <label style="margin-top:10px">Kind</label>
                <select id="s-kind" class="full">
                  <option value="shabd">Another SHABD server</option>
                  <option value="mcp">External MCP server (Anthropic protocol)</option>
                </select>
                <label style="margin-top:10px">URL</label>
                <input id="s-url" class="full"
                       placeholder="http://172.19.18.204:9036/mcp  ·  http://partner:8080">
                <label style="margin-top:10px">Bearer token (optional)</label>
                <input id="s-token" class="full" type="password"
                       placeholder="if the remote needs auth">
                <label style="margin-top:10px" id="s-transport-wrap">Transport (MCP only)</label>
                <select id="s-transport" class="full">
                  <option value="http">http</option>
                  <option value="stdio">stdio (local process)</option>
                </select>
                <div style="margin-top:14px">
                  <button onclick="connectSrc()">Connect &amp; import tools</button>
                  <span id="s-msg" style="margin-left:12px"></span>
                </div>
              </div>
              <div class="panel"><h3>Connected sources</h3>
                <div id="s-list" class="empty">None yet.</div>
              </div>
            </div>
            <div class="panel">
              <h3>What happens after connecting</h3>
              <ol style="color:var(--dim);font-size:13px;line-height:1.7">
                <li>Every remote tool becomes a local spell
                    <code>source__tool</code>.</li>
                <li>Open <a href="/agent">Agent Lab</a> → the new tools
                    appear as tick-boxes alongside your own.</li>
                <li>Build an agent mixing local + remote tools, save it.</li>
                <li>Each agent gets a <code>/query/&lt;agent&gt;</code> API —
                    call it from anywhere with just a question.</li>
              </ol>
            </div>
            """
            script = """
            async function load() {
              const r = await api('/api/sources');
              const list = document.getElementById('s-list');
              const ss = r.body.sources || [];
              if (!ss.length) { list.innerHTML = '<div class="empty">No sources connected yet.</div>'; return; }
              list.innerHTML = ss.map(s => `
                <div class="card" style="margin-bottom:10px">
                  <div><strong>${s.name}</strong>
                    <span class="tag info">${s.type}</span>
                    ${s.connected ? '<span class="tag ok">'+(s.tools||[]).length+' tools</span>'
                                   : '<span class="tag err">'+(s.error||'down')+'</span>'}</div>
                  <div style="color:var(--dim);font-size:12px;margin:4px 0">${s.url}</div>
                  <div>${(s.tools||[]).map(t=>'<span class="tag info">'+t+'</span>').join(' ')}</div>
                  <div style="margin-top:8px">
                    <button class="danger" onclick="disc('${s.name}')">Disconnect</button>
                  </div>
                </div>`).join('');
            }
            async function connectSrc() {
              const msg = document.getElementById('s-msg');
              msg.innerHTML = '<span class="tag info">Connecting…</span>';
              const r = await api('/api/sources/connect', {method:'POST', body:{
                name: document.getElementById('s-name').value.trim(),
                kind: document.getElementById('s-kind').value,
                url: document.getElementById('s-url').value.trim(),
                token: document.getElementById('s-token').value,
                transport: document.getElementById('s-transport').value,
              }});
              if (r.status>=400 || !r.body.ok)
                msg.innerHTML = '<span class="tag err">'+(r.body.error||'failed')+'</span>';
              else { msg.innerHTML = '<span class="tag ok">Imported '+r.body.count+' tools</span>'; load(); }
            }
            async function disc(name) {
              if (!confirm('Disconnect '+name+'? Its tools will be removed.')) return;
              const r = await api('/api/sources/'+encodeURIComponent(name)+'/disconnect',
                                   {method:'POST'});
              if (r.body.ok) load(); else alert(r.body.error||'failed');
            }
            load();
            """
            self._send_html(200, "sources", body, sess, script)

        def _page_client(self, sess: Session) -> None:
            body = """
            <div class="head"><h2>Client Console — talk to any SHABD/MCP server</h2></div>
            <div class="panel"><h3>Target</h3>
              <div class="row">
                <div style="flex:3"><label>Base URL</label>
                  <input id="c-url" class="full"
                         placeholder="http://localhost:8765"></div>
                <div style="flex:3"><label>Bearer token (optional)</label>
                  <input id="c-tok" class="full" type="password"
                         placeholder="paste a token from /tokens"></div>
                <div><button onclick="ping()">Ping</button></div>
                <div><button onclick="manifest()" class="ghost">Manifest</button></div>
                <div><button onclick="grim()" class="ghost">Verify chain</button></div>
              </div>
              <pre id="c-status" style="margin-top:8px">No target yet.</pre>
            </div>
            <div class="panel"><h3>Available spells</h3>
              <div id="c-list"><div class="empty">Click <b>Manifest</b> to list tools.</div></div>
            </div>
            """
            script = """
            function target() {
              return {
                base_url: document.getElementById('c-url').value.trim(),
                token: document.getElementById('c-tok').value.trim(),
              };
            }
            async function call(action, extra) {
              const t = target();
              if (!t.base_url) { alert('base_url required'); return null; }
              const body = Object.assign({}, t, {action}, extra||{});
              const r = await api('/api/client/call', {method:'POST', body});
              return r.body;
            }
            async function ping() {
              const r = await call('ping');
              document.getElementById('c-status').textContent =
                JSON.stringify(r, null, 2);
            }
            async function grim() {
              const r = await call('grimoire');
              document.getElementById('c-status').textContent =
                JSON.stringify(r, null, 2);
            }
            async function manifest() {
              const r = await call('manifest');
              document.getElementById('c-status').textContent =
                r.ok ? 'Listed '+ (r.manifest.spells||[]).length +' spells.'
                     : JSON.stringify(r, null, 2);
              if (!r.ok) return;
              const list = document.getElementById('c-list');
              const spells = (r.manifest.spells||[]);
              if (!spells.length) {
                list.innerHTML = '<div class="empty">Remote has no spells.</div>'; return;
              }
              list.innerHTML = spells.map(s => {
                const sch = s.input_schema || {};
                const props = Object.entries(sch.properties || {});
                const required = new Set(sch.required||[]);
                const fields = props.map(([k,p]) => {
                  const it = p.type==='integer'||p.type==='number' ? 'number':'text';
                  return `<div><label>${k}${required.has(k)?' *':''}</label>
                    <input class="full" name="${k}" type="${it}"
                           placeholder="${p.description||''}"></div>`;
                }).join('');
                return `<div class="panel" style="margin-bottom:12px">
                  <h3>${s.name}</h3>
                  <p style="color:var(--dim);font-size:13px">${s.description||''}</p>
                  <form data-spell="${s.name}">
                    <div class="row">${fields}</div>
                    <button>Invoke remotely</button>
                    <pre style="margin-top:10px" data-out></pre>
                  </form></div>`;
              }).join('');
              list.querySelectorAll('form').forEach(f => {
                f.addEventListener('submit', async (e) => {
                  e.preventDefault();
                  const name = f.dataset.spell;
                  const args = {};
                  f.querySelectorAll('input[name]').forEach(i => {
                    if (i.value === '') return;
                    args[i.name] = i.type==='number' ? Number(i.value) : i.value;
                  });
                  const out = f.querySelector('[data-out]');
                  out.textContent = '…';
                  const r = await call('invoke', {spell: name, args});
                  out.textContent = JSON.stringify(r, null, 2);
                });
              });
            }
            """
            self._send_html(200, "client", body, sess, script)

        def _page_settings(self, sess: Session) -> None:
            is_admin = sess.is_admin()
            body = f"""
            <div class="head"><h2>Settings</h2></div>

            <div class="panel"><h3>LLM backend</h3>
              <p style="color:var(--dim);font-size:13px">
                Used by the Agent Lab. Picks how the agent thinks.
                When backend is <code>none</code>, a mock LLM runs that
                just echoes — useful for development.
              </p>
              <div class="row">
                <div><label>Backend</label>
                  <select id="lc-be" class="full">
                    <option value="none">none (mock)</option>
                    <option value="ollama">Ollama (local)</option>
                    <option value="openai">OpenAI / compatible</option>
                    <option value="anthropic">Anthropic Claude</option>
                  </select></div>
                <div style="flex:3"><label>Base URL</label>
                  <input id="lc-url" class="full"
                         placeholder="http://localhost:11434  (Ollama) · https://api.openai.com/v1"></div>
              </div>
              <div class="row">
                <div style="flex:2"><label>Model</label>
                  <input id="lc-model" class="full"
                         placeholder="llama3.1:8b  ·  gpt-4o-mini  ·  claude-opus-4-8"></div>
                <div style="flex:3"><label>API key (optional for Ollama)</label>
                  <input id="lc-key" class="full" type="password"
                         placeholder="leave blank if not needed"></div>
              </div>
              <div style="margin-top:14px">
                <button onclick="saveLlm()" {('' if is_admin else 'disabled')}>Save LLM config</button>
                <span id="lc-msg" style="margin-left:14px"></span>
              </div>
              <p style="color:var(--dim);font-size:12px;margin-top:10px">
                Tip: for <strong>Ollama</strong> running on the same
                machine, use <code>http://127.0.0.1:11434</code>. The UI
                appends <code>/v1</code> automatically.
              </p>
            </div>

            <div class="panel"><h3>Backup / Restore — full project</h3>
              <p style="color:var(--dim);font-size:13px">
                Export everything (UI-built spells, saved agents, LLM
                config, audit chain, state) as a single zip.
                Recipient extracts and runs <code>bash run.sh</code>
                to reproduce the same server.
              </p>
              <div style="margin-top:14px">
                <a class="btn" href="/api/project/export" download="shabd-project.zip">
                  ⬇ Download project zip
                </a>
              </div>
              <div style="margin-top:18px">
                <label>Import a project zip (drag the file in)</label>
                <input type="file" id="prj-file" accept=".zip,application/zip">
                <label style="display:block;margin-top:8px;font-size:12px;color:var(--dim)">
                  <input type="checkbox" id="prj-over"> overwrite existing spells
                </label>
                <button onclick="importProject()" style="margin-top:10px">Upload &amp; merge</button>
                <span id="prj-msg" style="margin-left:14px"></span>
              </div>
            </div>

            <div class="panel"><h3>System info</h3>
              <pre id="out"></pre>
            </div>
            """
            script = """
            const IS_ADMIN = __ADMIN__;

            async function loadAll() {
              const r = await api('/api/settings');
              document.getElementById('out').textContent =
                JSON.stringify(r.body, null, 2);
              const cfg = await api('/api/llm_config');
              const c = cfg.body || {};
              document.getElementById('lc-be').value = c.backend || 'none';
              document.getElementById('lc-url').value = c.base_url || '';
              document.getElementById('lc-model').value = c.model || '';
              document.getElementById('lc-key').value = c.api_key && c.api_key !== '***' ? c.api_key : '';
            }

            async function saveLlm() {
              if (!IS_ADMIN) { alert('Admin only.'); return; }
              const msg = document.getElementById('lc-msg');
              msg.textContent = '…';
              const r = await api('/api/llm_config', {method:'POST', body:{
                backend: document.getElementById('lc-be').value,
                base_url: document.getElementById('lc-url').value.trim(),
                model: document.getElementById('lc-model').value.trim(),
                api_key: document.getElementById('lc-key').value,
              }});
              if (r.status>=400 || !r.body.ok)
                msg.innerHTML = '<span class="tag err">'+(r.body.error||'failed')+'</span>';
              else
                msg.innerHTML = '<span class="tag ok">Saved · backend='+r.body.backend+'</span>';
            }

            async function importProject() {
              const f = document.getElementById('prj-file').files[0];
              if (!f) { alert('Pick a zip first'); return; }
              const msg = document.getElementById('prj-msg');
              msg.textContent = '…';
              const data = await f.arrayBuffer();
              const r = await fetch('/api/project/import', {
                method:'POST',
                headers: {
                  'Content-Type': 'application/zip',
                  'X-CSRF': csrf,
                  'X-Overwrite': document.getElementById('prj-over').checked ? '1' : '0',
                },
                body: data,
              });
              const body = await r.json().catch(() => ({}));
              if (r.status >= 400 || !body.ok) {
                msg.innerHTML = '<span class="tag err">'+(body.error||'failed')+'</span>';
              } else {
                msg.innerHTML = '<span class="tag ok">'
                  + (body.imported||[]).length + ' spells, '
                  + (body.agents||[]).length + ' agents loaded</span>';
              }
            }

            loadAll();
            """.replace("__ADMIN__", "true" if is_admin else "false")
            self._send_html(200, "settings", body, sess, script)

    return Handler


# ============================================================================
# `python -m shabd_ui` entry point
# ============================================================================

if __name__ == "__main__":
    raise SystemExit(main())
