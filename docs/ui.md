# SHABD Production UI

A single-file, stdlib-only **no-code web UI** that exposes every SHABD
module — spells, Grimoire, agents, orchestrator, notary, audit log,
user management — through a polished browser dashboard.

  * Keycloak OIDC password-grant authentication (TCS Ultimatix pattern).
  * Three roles enforced server-side: `superuser`, `admin`, `user`.
  * Cookie sessions with `HttpOnly` + per-session CSRF tokens.
  * One file (`shabd_ui.py`), zero runtime dependencies.

```
http://your-server:8080/
  ├─ /login         Login form
  ├─ /              Dashboard with chain status + recent calls
  ├─ /spells        Every registered spell, one-click invoke
  ├─ /grimoire      Audit chain explorer + tamper indicator
  ├─ /audit         Filterable call log
  ├─ /agent         No-code agent playground
  ├─ /orchestrator  Intent registry + classify-a-query
  ├─ /notary        Publish roots, view peer roots & countersigs
  ├─ /users         Admin only — active sessions + role lists
  └─ /settings      View live config (secrets masked)
```

---

## 1. Step-by-step setup

### A. Keycloak side (one-time)

In the Keycloak admin console for your realm (e.g. `Ultimatix`):

1. Open the client (e.g. `Tcs-nginx-manager`).
2. Under **Settings**, turn on **Direct Access Grants Enabled** (this
   is the OIDC name for the password grant).
3. *(Confidential clients only)* Copy the **Client Secret** from the
   Credentials tab — pass it as `KEYCLOAK_CLIENT_SECRET`.
4. Map roles you want SHABD to honour into a realm role on each user
   (e.g. `admin`, `compliance`).

### B. Environment (per server)

```bash
# Keycloak
export KEYCLOAK_URL="https://keycloak.bank.internal"
export KEYCLOAK_REALM="Ultimatix"
export KEYCLOAK_CLIENT_ID="Tcs-nginx-manager"
# export KEYCLOAK_CLIENT_SECRET="..."  # only for confidential clients

# SHABD secrets
export SHABD_SECRET="$(openssl rand -hex 32)"
export NOTARY_SECRET="$(openssl rand -hex 32)"
export SHABD_AUDIT="/var/lib/shabd/audit.jsonl"

# Allow-lists by LDAP username
export SHABD_SUPERUSERS="abhishek,risk-officer"
export SHABD_ADMINS="amit.clerk,priya.manager"

# Behind nginx with TLS termination
export SHABD_UI_SECURE_COOKIES=1
```

### C. Run

```bash
python examples/ui_production.py
# UI live on http://0.0.0.0:8080/
```

Browser opens to the login form. Sign in with LDAP creds → land on the
dashboard.

---

## 2. Roles

| Role        | Can do                                                              |
|-------------|---------------------------------------------------------------------|
| `superuser` | Everything `admin` can, plus view/manage other users' sessions.     |
| `admin`     | Invoke scoped spells, see every Grimoire page, see every audit row. |
| `user`      | Invoke unscoped spells, see only own Grimoire pages / audit rows.   |

Role resolution order:

1. If `SHABD_SUPERUSERS` lists the username → `superuser` + `admin` + `user`.
2. Else if `SHABD_ADMINS` lists the username → `admin` + `user`.
3. Else → `user` only.
4. Additionally, any role present in the Keycloak token's
   `realm_access.roles` is added on top.

This means you can bootstrap a single superuser via env var on day 1
and then move role management into Keycloak later without restarting.

---

## 3. How each page works

### Dashboard
Counters refresh every 5 s: registered spells, audit pages, chain
status, recent calls table.

### Spells
The schema for each spell is rendered as a form. Each field's
`description` / `example` / `type` is used as the input placeholder.
Submit → POSTs to `/api/invoke/<spell>` → result rendered inline.

Spells that declare `scopes=[...]` are visible to everyone but only
**runnable by `admin`** through the UI (because the UI does not issue
per-user scoped tokens; that plumbing is one layer down — wire your
own `app.issue_token(...)` if you want finer-grained scopes).

### Grimoire
Lists every page with its sequence, spell, subject, OK flag, hash
prefix, and timestamp. The status banner shows `Chain intact` /
`TAMPER at seq N` based on `app.grimoire.verify()`. Non-admins see
only their own pages.

### Audit Log
Recent in-memory `CallRecord`s, filterable by spell, subject, and
`errors only`. Non-admins see only their own calls.

### Agent Lab
A no-code agent runner. Pick a system prompt, write a user prompt,
optionally supply a JSON `plan` for the `MockBackend`. Useful for
demoing the agent loop without a real LLM. To use a real LLM, edit
the example and swap `MockBackend` for `OpenAICompatBackend` /
`AnthropicBackend` etc.

### Orchestrator
Classify a query against every registered intent. The `via` field
shows which classifier stage caught the query: `keyword`, `synonym`,
`ngram`, `embedding`, or `llm`.

### Notary
Publish a fresh notary root for the current chain head; view peer
roots that have been received; view countersignatures held. Each
displayed item is a signed JSON object that the regulator can verify
offline with `scripts/verify_audit.py`-style scripts.

### Users
Admin-only. Lists active sessions, who is logged in right now, and
the allow-lists.

### Settings
Read-only view of the running config so that during a Sev-1 you can
see exactly which Keycloak realm, audit path, and superuser list the
server thinks it has.

---

## 4. Production deployment

### Behind nginx

```nginx
server {
    listen 443 ssl http2;
    server_name shabd.bank.internal;
    ssl_certificate     /etc/ssl/bank.crt;
    ssl_certificate_key /etc/ssl/bank.key;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For  $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 60s;
    }
}
```

### Systemd unit

```ini
[Unit]
Description=SHABD UI
After=network.target

[Service]
ExecStart=/opt/shabd/venv/bin/python /opt/shabd/examples/ui_production.py
EnvironmentFile=/etc/shabd/ui.env
Restart=always
User=shabd
Group=shabd

[Install]
WantedBy=multi-user.target
```

### Docker

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY shabd.py shabd_agent.py shabd_orchestrator.py shabd_notary.py shabd_ui.py /app/
COPY examples/ui_production.py /app/main.py
EXPOSE 8080
CMD ["python", "/app/main.py"]
```

```yaml
# docker-compose.yml
services:
  shabd-ui:
    build: .
    ports: ["8080:8080"]
    environment:
      KEYCLOAK_URL: "https://keycloak.bank.internal"
      KEYCLOAK_REALM: "Ultimatix"
      KEYCLOAK_CLIENT_ID: "Tcs-nginx-manager"
      SHABD_SECRET: "${SHABD_SECRET}"
      NOTARY_SECRET: "${NOTARY_SECRET}"
      SHABD_AUDIT: "/audit/audit.jsonl"
      SHABD_SUPERUSERS: "abhishek"
      SHABD_UI_SECURE_COOKIES: "1"
    volumes:
      - shabd-audit:/audit
volumes: { shabd-audit: {} }
```

### High-availability tips

* Sessions are in-memory by default. For a multi-replica deployment,
  ride the load balancer with sticky sessions (cookie `shabd_sid`),
  or pass `session_store=<your-redis-shim>` to `UIServer(...)`.
* The Grimoire chain is per-process. For HA, point `grimoire_log_path`
  at shared storage and use the existing replication primitives in
  `shabd_enterprise.py`.
* Each instance should publish its own notary root regularly. Two-way
  exchange with peer banks reduces blast radius if any one node is
  compromised.

---

## 5. Local development (no Keycloak)

```bash
export SHABD_UI_BOOTSTRAP_USER=admin
export SHABD_UI_BOOTSTRAP_PASSWORD="$(openssl rand -hex 16)"
python examples/ui_production.py
```

Sign in with `admin` + the password you just set. Useful for testing
without touching your Keycloak instance.

---

## 6. Hardening checklist

* [x] `SHABD_SECRET` and `NOTARY_SECRET` set to a 32+ byte random value.
* [x] Behind nginx with TLS (no plaintext HTTP from outside).
* [x] `SHABD_UI_SECURE_COOKIES=1` set.
* [x] `SHABD_SUPERUSERS` is a tiny list (1-2 humans, named).
* [x] Audit file (`SHABD_AUDIT`) lives on a volume with snapshots.
* [x] Notary roots are shipped to peers at least daily.
* [x] Keycloak client uses **confidential** mode in production with a
      rotated `KEYCLOAK_CLIENT_SECRET`.
* [x] Login throttling is on (default 5 attempts / 30 s per user).
* [x] Browser CSP / HSTS / frame-deny headers are sent by SHABD; nginx
      can layer more if your bank requires.

---

## 7. Testing

```bash
python tests/test_ui.py     # 14 tests covering JWT decode, bootstrap
                            # login, throttle, CSRF, RBAC, full live
                            # HTTP round-trip
```

All 14 tests are stdlib-only.
