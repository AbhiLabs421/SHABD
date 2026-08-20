# SHABD usage guide

A practical walkthrough for the v2.9.1 single-file production release.
No Docker. No Kubernetes. No external auth provider. Just one
Python file, one command, one browser tab.

This guide assumes you have Python 3.10+ on a Linux/macOS machine.

---

## 1. The 60-second start

```bash
# 1. Get the code
git clone https://github.com/Kumar123ips/SHABD
cd SHABD

# 2. Start the UI in the background
python -m shabd_ui --daemon --port 8080

# 3. Open the browser
open http://localhost:8080/register
```

Register the first user. **That user automatically becomes the
superuser** — no separate admin setup, no env vars, no config file.

When you're done:

```bash
python -m shabd_ui --stop
```

That's the entire deployment.

---

## 2. The mental model

There are four things you'll touch:

| Term | What it is |
|---|---|
| **Spell** | A Python function exposed to AI agents / HTTP / SDK. Can be defined in code (`@app.spell`) or live from the UI's Spell Builder page. |
| **Token** | A bearer token a client carries when calling a spell. Carries the subject and a scope list. Issued from the UI. |
| **Scope** | A label that gates calls. A spell can require one or more scopes; a token can only call spells whose scopes it carries. |
| **Grimoire** | The hash-chained audit log. **Every** spell call, every user register/login, every admin action lands here. Tamper-evident by design. |

That's the whole vocabulary.

---

## 3. The flow for someone building an internal AI tool

### 3.1 Start the server

```bash
python -m shabd_ui --daemon \
                   --port 8080 \
                   --audit /var/lib/shabd/audit.jsonl
```

`--audit` makes the Grimoire chain survive restarts (the user store
lives in the chain, so this also persists users).

### 3.2 First-time setup in the browser

Open `http://localhost:8080/register`:

* Pick a username.
* Pick a password (min 8 characters).
* Submit.

You're now signed in as the superuser. The badge in the sidebar shows
your role chips.

### 3.3 Build your first spell from the browser

Go to **Spell Builder** in the sidebar (visible only to superusers).
Paste this:

```python
def discount(price: float, pct: float) -> dict:
    '''Apply a percentage discount.'''
    return {
        "final": price * (1 - pct / 100),
        "saved": price * pct / 100,
        "pct": pct,
    }
```

Fill in:

* **Name** → `discount`
* **Description** → `Apply a percentage discount`
* **Required scopes** → leave empty for now
* **Tags** → `retail, billing`

Click **Register spell**. You'll see a green chip with the source hash.

Go to **Spells** in the sidebar — the new `discount` form is already
there. Fill in `price=1000, pct=15` and click **Invoke**. You should
see `{"ok": true, "result": {"final": 850.0, ...}}`.

The spell is live. No restart, no redeploy.

### 3.4 Lock the spell with a scope

Go to **Scopes**. Find `discount`, type `pricing` in its scope input,
press **Save**. Future calls without the `pricing` scope will be
rejected.

### 3.5 Mint a token for your external project

Go to **Issue Token**:

* **Subject** → `agent-bob` (any identifier — your project, your agent,
  your bot)
* **Scopes** → `pricing`
* **TTL** → `3600`

Click **Issue**. A long string appears. Copy it.

### 3.6 Use the spell from outside

Choose any language. Example: Python.

```python
from shabd_client import SHABDClient

client = SHABDClient("http://your-server:8080",
                     token="ey…paste-the-token…")
client.cast("discount", {"price": 1000, "pct": 15})
# {"final": 850.0, "saved": 150.0, "pct": 15}
```

Or `curl`:

```bash
curl -H "Authorization: Bearer ey…" \
     -H "Content-Type: application/json" \
     -d '{"price":1000,"pct":15}' \
     http://your-server:8080/spells/discount
```

Or feed every spell to a GPT/Claude/Gemini model at once:

```python
client = SHABDClient("http://your-server:8080", token=TOK)
tools = client.tools_for_openai()  # auto-formatted manifest
```

Each call is logged to the Grimoire chain. PII is auto-redacted (the
chain stores hashes, not raw args).

### 3.7 Invite teammates

Go to **Users** (admin or superuser only):

* Fill **Username**, **Password**, tick the role checkboxes
  (`user`, `admin`, `superuser`).
* **Create**.

The new user can sign in at `/login`. Every register / login /
role-change is itself a Grimoire page — that's the **revolutionary
identity-in-the-chain** angle.

### 3.8 Talk to any other SHABD/MCP server

Go to **Client Console**. Type the remote server's URL and a token. Hit
**Manifest**. The console shows every remote spell with an auto-form.
Invoke any of them. The UI proxies through the backend — no CORS, no
token in the browser.

---

## 4. The flow for someone integrating SHABD into their app

You don't have to use the UI at all. Drop SHABD into a normal Python
project:

```python
# my_spells.py — the file you'd point at with --spells
from shabd import SHABD

# `app` is supplied by `python -m shabd_ui` at boot.
# If running standalone, create one: app = SHABD("mine", secret="…")

@app.spell(tags=["billing"], scopes=["pricing"])
def discount(price: float, pct: float) -> dict:
    return {"final": price * (1 - pct/100), "pct": pct}

@app.spell(scopes=["compliance"], tags=["kyc"])
def kyc(name: str, pan: str) -> dict:
    return {"verified": True, "name": name}
```

Then:

```bash
python -m shabd_ui --daemon --spells my_spells.py
```

Both the UI-built and code-built spells show up together.

For programmatic access from a different process:

```python
from shabd_client import SHABDClient
c = SHABDClient("http://shabd:8080", token=TOK)
c.cast("kyc", {"name": "Amit", "pan": "AAAPA1234A"})
```

---

## 5. The flow for plugging SHABD into an LLM-driven agent

SHABD is MCP-compatible — every spell's schema is in OpenAI / Anthropic
/ Ollama tool format. So a model that supports tools can call your
spells directly:

```python
from shabd_client import SHABDClient
from openai import OpenAI                       # any model SDK

shabd = SHABDClient("http://shabd:8080", token=TOK)
oai = OpenAI()

resp = oai.chat.completions.create(
    model="gpt-4o",
    messages=[{"role": "user", "content": "What's 1000 minus 15%?"}],
    tools=shabd.tools_for_openai(),
)

for choice in resp.choices:
    if choice.message.tool_calls:
        messages = shabd.dispatch_openai_tool_calls(
            choice.message.tool_calls)
        # ... feed `messages` back to the model
```

Same pattern with Anthropic (`shabd.tools_for_anthropic()`) or with
Ollama / local models.

For a fully self-hosted agent loop, use `shabd_agent.py`:

```python
from shabd_agent import Agent
from shabd import SHABD

agent = Agent.from_shabd(app, llm=MyBackend(),
                          system="You are a helpful banking assistant.")
result = agent.run("Apply 15% off on ₹1000")
print(result.answer)
```

---

## 6. CLI reference

```
python -m shabd_ui [options]

Options
  --bind            address to bind (default 0.0.0.0)
  --port            port to listen on (default 8080)
  --audit           Grimoire JSONL path (default shabd-audit.jsonl)
  --spells          optional spells file to import (default my_spells.py)
  --pid             pid file path (default shabd-ui.pid)
  --log             daemon log path (default shabd-ui.log)
  --daemon          run in background
  --foreground      (default) run in foreground
  --stop            stop the running daemon
  --status          show daemon status
  --secure-cookies  set Secure flag on cookies (needs HTTPS)
  --no-register     disable self-registration (admin-invite only)
```

Common recipes:

```bash
# Quick local poke
python -m shabd_ui

# Production
python -m shabd_ui --daemon \
    --bind 0.0.0.0 --port 8080 \
    --audit /var/lib/shabd/audit.jsonl \
    --log /var/log/shabd/ui.log \
    --pid /run/shabd/ui.pid \
    --secure-cookies \
    --no-register \
    --spells /opt/myapp/spells.py

# Restart
python -m shabd_ui --stop
python -m shabd_ui --daemon ...   # same flags

# Behind systemd? Run --foreground from a unit file.
```

---

## 7. REST/JSON API reference

Every browser page is a thin layer over a JSON endpoint. Anything you
do in the UI you can do from a script.

| Action | Endpoint | Method | Role |
|---|---|---|---|
| Health | `/healthz` | GET | public |
| Register | `/register` | POST form | public |
| Login | `/login` | POST form | public |
| Dashboard | `/api/dashboard` | GET | session |
| Spells list | `/api/spells` | GET | session |
| Invoke spell | `/api/invoke/<name>` | POST + CSRF | session |
| Audit log | `/api/audit` | GET | session |
| Grimoire | `/api/grimoire` | GET | session |
| Agent run | `/api/agent/run` | POST + CSRF | session |
| Orchestrator | `/api/orchestrator/classify` | POST + CSRF | session |
| Notary state | `/api/notary/state` | GET | session |
| Notary publish | `/api/notary/publish` | POST + CSRF | admin |
| Create spell | `/api/spells/create` | POST + CSRF | superuser |
| Delete spell | `/api/spells/<n>/delete` | POST + CSRF | superuser |
| Scopes list | `/api/scopes` | GET | admin |
| Set scopes | `/api/scopes/<n>` | POST + CSRF | admin |
| Issue token | `/api/tokens/issue` | POST + CSRF | admin |
| Client proxy | `/api/client/call` | POST + CSRF | session |
| Users list | `/api/users` | GET | session |
| Create user | `/api/users/create` | POST + CSRF | admin |
| Set roles | `/api/users/<u>/roles` | POST + CSRF | admin |
| Reset pw | `/api/users/<u>/password` | POST + CSRF | admin |
| Delete user | `/api/users/<u>/delete` | POST + CSRF | superuser |

Plus the **native SHABD endpoints** (the wire format MCP clients
expect):

| Endpoint | Method | What |
|---|---|---|
| `/manifest` | GET | All spells in OpenAI / Anthropic / MCP format |
| `/spells/<name>` | POST | Invoke (Bearer token + scopes enforced) |
| `/grimoire/head` | GET | Latest chain head hash |
| `/grimoire/verify` | GET | Chain integrity check |

Send the `Authorization: Bearer …` header and you're in.

---

## 8. Roles in one paragraph

* **user** — sign in, view own audit pages, run spells whose token
  scopes match.
* **admin** — everything **user** can do + create users, set roles
  (except superuser), reset passwords, issue tokens, edit spell scopes,
  publish notary roots.
* **superuser** — everything **admin** can do + open the Spell Builder
  (run sandboxed Python), delete users, delete UI-created spells.

The first registered user is auto-promoted to all three roles. After
that, roles are assigned manually from the **Users** page.

---

## 9. What's persistent and what isn't

| Lives in | Survives restart? |
|---|---|
| Spells defined in code (`my_spells.py`) | yes — they re-register every boot |
| Spells built from the UI (`/builder`) | **no** — currently RAM-only. Paste the source into a file to persist. |
| Users | yes — replayed from the Grimoire chain |
| Sessions (cookies) | no — sign-in is required after restart |
| Audit chain | yes — JSONL on disk at `--audit` |

If you want UI-built spells to persist, use Spell Builder for
experimentation, then copy the source into your `my_spells.py` and
restart. (Persistent UI-built spells is on the v3.0 list.)

---

## 10. What does NOT exist like this anywhere else

Honest framing. Each row has the closest competitor and what they
miss:

| Capability | Closest competitor | What they're missing |
|---|---|---|
| Single-file Python install, zero deps | FastAPI / FastMCP | They are libs, not full stacks (no UI, no audit, no users) |
| Browser-built tools registered live | Flowise, Langflow | Need Docker + Postgres; no audit chain; Node.js |
| Tamper-evident audit chain for spell calls | Datadog / Splunk | They store logs but cannot prove tamper — no Merkle/hash chain |
| Identity events in the SAME hash chain | none I know of | Even AWS CloudTrail uses a separate IAM event stream |
| Cross-entity countersignatures (no blockchain) | Hyperledger / Quorum | Need a consensus protocol; we use peer-to-peer HMAC |
| First-user → superuser self-bootstrap | Nextcloud, Mattermost | They do this but only for chat / files, not for AI tools |
| MCP-compatible wire format | Anthropic MCP servers | They are protocol-only; no UI; no user mgmt; no audit |
| Visual scope editor that takes effect live | AWS IAM, Auth0 | Heavy SaaS; not portable; not per-tool |
| Daemon mode with one `python -m` command | systemd + unit files | Need ops setup; SHABD just works on a fresh box |

The novel combination is: **a single Python file that gives you a
tool-server + tamper-evident audit log + identity-in-the-chain + live
visual editor + remote-server console — without Docker, Kubernetes,
Postgres, or an SSO provider.**

If you find a project that ticks every box on this list, please open
an issue — I'll happily list it as prior art.

---

## 11. Troubleshooting

| Symptom | What to check |
|---|---|
| `already running (pid N)` on start | `python -m shabd_ui --stop` first |
| Daemon dies silently | `tail -f shabd-ui.log` — the daemon's stdout/stderr is there |
| `bad credentials` after restart | Did you set `--audit` to a persistent path? Without it, the user store rebuilds empty. |
| `chain_ok=False` in dashboard | Someone (or something) edited the audit JSONL on disk. That's the chain doing its job; investigate. |
| Spell not visible after Builder submit | Reload the page — the new spell appears under `/spells` immediately, but the table cached. |
| `403 csrf token mismatch` from a script | Browser sessions need the `X-CSRF` header. External clients should use a bearer token instead — no CSRF needed. |
| Browser warning about cookies | Add `--secure-cookies` only if you terminate TLS in front. Local HTTP without TLS won't accept Secure cookies. |

---

## 12. The smallest "this is wired up correctly" check

After a fresh start + register + login, in your terminal:

```bash
curl -s http://localhost:8080/healthz
# {"ok": true}

curl -s http://localhost:8080/manifest | python3 -m json.tool | head
# {
#   "shabd_version": "2.9.1",
#   "name": "shabd-ui",
#   "spells": [...]
# }
```

If both work, the server is up, the manifest is being served, and
LLM clients can discover your spells.

---

## 13. Where to read more

* `shabd.py` — the core engine (spells, schema, tokens, Grimoire).
* `shabd_ui.py` — every page, every API, in one file.
* `shabd_users.py` — the user store that proved identity can live in
  the audit chain.
* `shabd_ui_cli.py` — the launcher.
* `tests/` — 218 tests across 11 files. Read these as executable
  documentation.

Or just open the UI and click around — every page is rendered from the
JSON endpoint of the same name, and the JSON endpoint is documented in
§7.
