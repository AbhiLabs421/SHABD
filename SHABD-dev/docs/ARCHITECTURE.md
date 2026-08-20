# SHABD — Architecture & Flow Guide

> **What this document is.** A single, living map of the whole project: what
> each file does, where a request *starts*, how it *flows* through the system,
> and why the pieces are shaped the way they are. Read it top-to-bottom once to
> build a mental model; after that use the table of contents to jump.
>
> **Keep it updated.** Whenever you add a module, a page, or change the request
> pipeline, update the relevant section **and** add a line to the
> [Change log](#12-change-log--how-to-keep-this-current) at the bottom. Sections
> are deliberately small so edits stay cheap.

---

## Table of contents

1. [One-paragraph mental model](#1-one-paragraph-mental-model)
2. [The layered picture](#2-the-layered-picture)
3. [Module map — every file and its job](#3-module-map--every-file-and-its-job)
4. [Where does work *start*? (entry points)](#4-where-does-work-start-entry-points)
5. [The core object: the `SHABD` app and a `Spell`](#5-the-core-object-the-shabd-app-and-a-spell)
6. [The request lifecycle — the single most important flow](#6-the-request-lifecycle--the-single-most-important-flow)
7. [The no-code UI flow (browser → UIServer → app)](#7-the-no-code-ui-flow-browser--uiserver--app)
8. ["Connector as a tool" — KB, SQL Intelligence, Nova](#8-connector-as-a-tool--kb-sql-intelligence-nova)
9. [Agents, Orchestrator, and Studio](#9-agents-orchestrator-and-studio)
10. [Trust & audit — Grimoire and Notary](#10-trust--audit--grimoire-and-notary)
11. [Persistence — where state lives](#11-persistence--where-state-lives)
12. [Change log / how to keep this current](#12-change-log--how-to-keep-this-current)

---

## 1. One-paragraph mental model

SHABD turns an ordinary Python function into a **secure, audited, AI-callable
tool** (a *spell*). You register functions on a `SHABD` app object; the app
gives every call the same pipeline — schema validation, scope/auth checks, rate
limiting, circuit breaking, idempotency, caching, and a tamper-evident audit
entry. On top of that one core sit four *front doors* that all reach the same
spells: an **MCP/HTTP server** (for AI clients), a **no-code web UI**
(`shabd_ui`) for humans, an optional **FastAPI** app, and a visual **Studio**
(`shabd_studio`) for building chatbots. Everything is a single Python file with
zero required dependencies — no database, no Docker, no vector store needed.

---

## 2. The layered picture

```
                        ┌──────────────────────────────────────────────┐
   HUMANS               │  shabd_ui (no-code web UI)   shabd_studio      │
   (browser)            │  build spells, agents,       (drag-drop bot    │
                        │  KB/SQL/Nova connectors      builder + publish)│
                        └───────────────┬──────────────────────────────┘
                                        │ calls the SAME methods/app
   AI CLIENTS           ┌───────────────┴───────────────┐
   (Claude, Ollama,     │  MCP (stdio + HTTP)  ·  REST   │   shabd_fastapi
    scripts, agents)    │  /manifest /spells /query /ask │   (optional)
                        └───────────────┬───────────────┘
                                        │
                        ┌───────────────┴────────────────────────────────┐
   THE CORE             │                shabd.py  (SHABD app)             │
   (one object)         │   spell registry · invoke() pipeline · tokens    │
                        │   Grimoire audit chain · manifest/openapi         │
                        └───────────────┬────────────────────────────────┘
                                        │ supported by
        ┌───────────────┬──────────────┼───────────────┬─────────────────┐
   shabd_agent      shabd_orchestrator  shabd_users   shabd_notary   shabd_enterprise
   (LLM backends +  (intent routing +   (login /      (cross-org     (LDAP/SAML, HSM,
    agent loop)      multi-agent flows)  RBAC users)   audit trust)   Postgres, mTLS…)
```

The golden rule: **every front door ends up calling `app.invoke(spell, args)`**
(or the async variant). There is exactly one place where a tool actually runs,
so security and audit are guaranteed no matter who calls in.

---

## 3. Module map — every file and its job

| File | Lines | Role | Key classes / entry points |
|------|-------|------|----------------------------|
| `shabd.py` | ~4700 | **The core framework.** The `SHABD` app, spell registration, the `invoke()` pipeline, token minting/verification, the Grimoire audit chain, MCP + HTTP transports, `manifest()`/`openapi()`, chains, semantic types. | `SHABD`, `Spell`, `Grimoire`, `Context`, `Email/Aadhaar/Money…` |
| `shabd_ui.py` | ~7500 | **The no-code web UI.** A single-file HTTP server that renders every page (Dashboard, Spells, Spell Builder, Agent Lab, Orchestrator, Chains, Knowledge Base, SQL Intelligence, Nova, Notary, Tokens, Users, Settings…) and exposes JSON APIs that drive the core app. | `UIServer`, `Session`, `_compile_spell_source` |
| `shabd_studio.py` | ~720 | **Visual chatbot builder.** A light-themed drag-drop canvas; drop tools/agents onto an Assistant, set a prompt, test live, **Publish** → REST + one-line embed `<script>` + hosted chat page. Shares the UIServer's app & sessions. | `StudioServer` |
| `shabd_agent.py` | ~1320 | **The agent loop + LLM backends.** The universal tool-calling loop and adapters for OpenAI-compatible (incl. Ollama), Anthropic, Gemini, and a deterministic `MockBackend` for air-gapped demos/tests. | `Agent`, `OpenAICompatBackend`, `AnthropicBackend`, `MockBackend`, `ToolRegistry` |
| `shabd_orchestrator.py` | ~770 | **Routing & multi-agent flows.** Intent classification (keyword + optional semantic), cost tracking/budgets, LLM fallback chains, and the `Orchestrator` that picks an agent for a query and runs sequential/parallel flows. | `Orchestrator`, `IntentClassifier`, `CostTracker` |
| `shabd_users.py` | ~380 | **User store + auth.** scrypt password hashing, roles (superuser/admin/user), used by the UI for login/register and RBAC. | `UserStore`, `User` |
| `shabd_praman.py` | ~720 | **Built-in identity provider ("Praman").** Pure-stdlib OAuth2/OIDC-style server: users/roles/clients, `/token` (password + refresh + client-credentials), `/userinfo`, `/introspect`, `/revoke`, discovery, **HS256 or RS256** JWTs (RS256 publishes a JWKS so any client verifies offline), TOTP MFA, lockout, alg-substitution defense, and a standalone `PramanServer`. Config-selectable: builtin **or** external Keycloak. Every auth event can be Grimoire-audited. See `docs/PRODUCTION-READINESS.md`. | `Praman`, `PramanRealm`, `PramanServer`, `IdentityProvider`, `identity_from_config` |
| `shabd_rsa.py` | ~220 | **Pure-Python RSA** (zero deps): Miller-Rabin keygen, RSASSA-PKCS1-v1_5/SHA-256 sign+verify, and JWK export. Backs Praman's RS256/JWKS so external services verify SHABD tokens with a public key — no `cryptography` C extension, keeping the air-gap promise. | `generate_keypair`, `sign_pkcs1v15_sha256`, `verify_pkcs1v15_sha256`, `jwk_public` |
| `shabd_smriti.py` | ~480 | **Built-in cache / coordination server ("Smriti").** Pure-stdlib, RESP-subset (so real `redis-py` clients work too): PING/SET+EX/GET/DEL/INCR/EXPIRE/TTL/EXISTS/FLUSHALL, optional AUTH + AOF persistence. `SmritiCache` slots into SHABD's `ConjurePlugin` cache + distributed rate-limit hooks. Config-selectable: in-process builtin / Smriti server / external Redis. | `SmritiServer`, `SmritiClient`, `SmritiStore`, `SmritiCache`, `cache_from_config` |
| `shabd_config.py` | ~230 | **The single production control surface.** Loads one `config.yaml`/`.json` (PyYAML if present, else a built-in YAML-subset parser — zero-dep) and builds the selected providers: identity (Praman/Keycloak), cache (Smriti/Redis), persistence, secret source (env/file/inline), TLS. `config.example.yaml` documents the whole posture. | `ProductionConfig`, `load_config`, `resolve_secret` |
| `shabd_verify.py` | ~150 | **Independent audit-log verifier** ("don't trust us, verify"). A standalone, zero-dep file an auditor/regulator runs themselves to re-derive the Grimoire SHA-256 hash chain and confirm no tampering — no SHABD, no DB, no secret needed (hash chain); `--secret` also checks HMAC signatures. CLI: `python -m shabd_verify audit.jsonl`. | `verify_pages`, `verify_file`, `main` |
| `shabd_notary.py` | ~420 | **Cross-entity trust.** Two organisations countersign each other's audit-chain heads; regulator-ready inclusion proofs with stdlib verifiers. | `AgentNotary`, `NotaryRoot`, `InclusionProof` |
| `shabd_fastapi.py` | ~300 | **Optional FastAPI front-end.** Wraps the same app in FastAPI/uvicorn if you want that ecosystem. Pure adapter — no new business logic. | `build_fastapi()`, `run()` |
| `shabd_ui_cli.py` | ~320 | **The launcher.** `python -m shabd_ui_cli` (or the `shabd-ui` entry point): loads spells, starts the UI, and optionally the FastAPI and Studio servers in threads. | `main()`, `_maybe_start_studio` |
| `shabd_client.py` | ~250 | **Python client SDK.** Talks to a running SHABD server over HTTP with tracing headers. | `SHABDClient` |
| `shabd_enterprise.py` | ~1200 | **Enterprise plug-ins (opt-in).** LDAP/SAML auth, HSM/file key providers, RBAC policy engine, SQLite/Postgres Grimoire persistence, mTLS, OTLP/Prometheus/Kafka exporters. Nothing here is required to run. | `RBACPolicyEngine`, `PostgresGrimoirePersistence`, `X509Signer` |
| `shabd_packs/` | — | Ready-made "business packs" (bundles of spells) you can load. | — |

---

## 4. Where does work *start*? (entry points)

There are four ways in, and it helps to know which one you're looking at:

1. **CLI launcher — the usual production start.**
   `shabd_ui_cli.py :: main()` parses flags, builds a `SHABD` app, loads your
   spells file, constructs a `UIServer`, and calls `.serve()`. With
   `--studio-port` / `--fastapi-port` it also starts those servers in threads
   that **share the same app and UIServer** (so a spell built in the UI is
   instantly visible to Studio and the API).
   ```
   python -m shabd_ui_cli --port 8080 --studio-port 8095
   ```

2. **The no-code UI — where a human starts.**
   A browser hits `UIServer.serve()`'s request handler. Page routes
   (`/builder`, `/agent`, `/spells`, …) render HTML; the `/api/**` routes are
   JSON endpoints that call `UIServer` methods, which call the core app.
   → See [§7](#7-the-no-code-ui-flow-browser--uiserver--app).

3. **Direct import — where a developer starts.**
   ```python
   from shabd import SHABD
   app = SHABD("my-svc", secret="…")

   @app.spell(tags=["math"])
   def add(a: int, b: int) -> int:
       return a + b

   app.serve()          # MCP + HTTP on :8765
   ```
   The `@app.spell` decorator is the true beginning of everything: it builds the
   JSON schema from the type hints and puts the function in the registry.

4. **An AI client — where a machine starts.**
   An MCP client (Claude Desktop, etc.) speaks JSON-RPC over stdio
   (`app.mcp_stdio()`), or any client calls the HTTP routes
   (`GET /manifest`, `POST /spells/<name>`, `POST /query/<agent>`,
   `POST /ask`). All of these converge on `invoke()`.

---

## 5. The core object: the `SHABD` app and a `Spell`

- **`SHABD` (in `shabd.py`)** is the one object that holds everything: the
  `_spells` registry, the token signer, the `Grimoire` audit chain, the rate
  limiter, circuit breaker, cache, and plugin/hook lists.

- **A `Spell`** is a registered function plus its metadata: the JSON `schema`
  derived from type hints, required `scopes`, `tags`, cache/retry/timeout/
  concurrency settings, and whether it wants the `Context`.

- **Schema derivation** (`_build_schema` → `_type_to_schema`) turns Python type
  hints into JSON Schema. It understands primitives, `list`/`dict`/`tuple`,
  `Optional`, `Literal` (→ enum), dataclasses, the semantic types
  (`Email`, `Aadhaar`, `Money`, …), and **unions**:
  `Union[int, float]` (and the `int | float` syntax) become
  `{"anyOf": [{"type":"integer"}, {"type":"number"}]}`.

  > **Worked example (verified).** The spell
  > ```python
  > from typing import Union
  > def add(a: Union[int, float], b: Union[int, float]) -> Union[int, float]:
  >     return a + b
  > ```
  > registers with **no validation error**. Its schema is
  > `anyOf[integer, number]` for each arg. `add(2,3) → 5`,
  > `add(2.5,1.25) → 3.75`, `add(2,1.5) → 3.5`. A wrong type such as
  > `add("hello", 3)` is *correctly* rejected with
  > `ValidationError: a: matches none of allowed types`. That rejection is the
  > system working as designed, not a bug in the spell.

---

## 6. The request lifecycle — the single most important flow

Every tool call, from any front door, funnels through
`SHABD._invoke_async_inner` (in `shabd.py`). Understanding this ordered pipeline
is understanding the framework:

```
POST /spells/add {"a":2,"b":3}            (or app.invoke("add", {...}))
        │
        ▼
1.  shutdown gate ......... refuse new work if the server is draining
2.  schema validation ..... args must match the spell's JSON Schema  ── fail → ValidationError
3.  authz (scopes) ........ token/session must hold the spell's scopes ── fail → ForbiddenError
4.  rate limit ............ per-subject / per-spell token bucket ..... ── fail → RateLimitError
5.  circuit breaker ....... if the spell is failing a lot, short-circuit ─ open → ConjureError
6.  idempotency ........... same Idempotency-Key + body → replay cached result (no re-run)
7.  concurrency cap ....... optional per-spell asyncio semaphore
8.  cache lookup .......... if cache_ttl set, return cached result on hit
9.  before-hooks/plugins .. policy hooks (RBAC, sanctions) may deny here
10. THE CALL .............. run the function, with retries + timeout
11. after-hooks/plugins ... metrics, custom side-effects
12. Grimoire append ....... write a hash-chained audit page (tamper-evident)
        │
        ▼
   result (JSON) ──────────► back out through whichever front door called in
```

Two things make this powerful:

- **There is only one pipeline.** A human clicking "Invoke" in the UI, an agent
  calling a tool, and an external `curl` all get the *same* checks and the
  *same* audit trail. You cannot accidentally bypass security by using a
  different door.
- **The audit is a chain, not a log.** Step 12 commits each entry to the hash of
  the previous one (see [§10](#10-trust--audit--grimoire-and-notary)), so any
  after-the-fact edit is detectable.

---

## 7. The no-code UI flow (browser → UIServer → app)

`shabd_ui.py` is a self-contained web app. The shape of every interaction:

```
Browser page (e.g. /builder)                UIServer                     SHABD app
─────────────────────────────       ───────────────────────       ──────────────────
1. User types a spell + clicks   →   2. POST /api/spells/create  →  3. _compile_spell_source()
   "Register"                          (session cookie + CSRF)         compiles in a curated
                                                                       _SAFE_BUILTINS namespace
                                       4. app.spell(...)(fn)       →  5. schema built, Spell
                                                                       added to registry
                                       6. _save_state_file()          (persist to sidecar JSON)
7. Table refreshes, spell is    ←     ─────────────────────────      + Grimoire audit page
   now callable everywhere
```

Key facts to keep in mind when editing the UI:

- **Auth model.** `Session` objects (cookie `shabd_sid`) carry roles
  (superuser/admin/user) and a CSRF token. Spell *building* is superuser-gated;
  spell *invoking* respects scopes.
- **Pages vs APIs.** Each `_page_*` method returns HTML + a small vanilla-JS
  `<script>`; the script calls `/api/**` JSON endpoints. No framework, no build
  step.
- **The sandbox is a guard-rail, not a jail.** `_SAFE_BUILTINS` blocks the
  accidental `os`/`subprocess` foot-gun but still allows `__import__` — a
  determined superuser can escape. That is deliberate policy; the *audit chain*
  is the real safety net (every create is recorded).
- **Everything a user builds is re-created on boot** from the sidecar state file
  (`_recreate_dynamic_on_boot`), so UI-made spells/agents/chains/KB/SQL/Nova
  tools survive a restart.

---

## 8. "Connector as a tool" — KB, SQL Intelligence, Nova

Three features share one revolutionary pattern: **configure an external (or
local) data source once, click "Expose as tool," and it becomes a normal spell**
that shows up in Spells, `/manifest`, Agent Lab, Studio's palette, and every API
— indistinguishable from a hand-written spell.

| Feature | What it wraps | Data path | Exposed spell |
|---------|---------------|-----------|---------------|
| **Knowledge Base** | *Your own* pasted documents | Pure-stdlib TF-IDF cosine retrieval — no vector DB | `kb_<name>(question)` |
| **SQL Intelligence** | An *external* text-to-SQL service (its API + key) | Server-side HTTP proxy hides the key & dodges CORS | `sql_<name>(query)` |
| **Nova** | An *external* multi-tenant RAG platform | Proxy drives Tenants→Pipelines→Ingest→Query; stdlib multipart forwards PDF/DOCX bytes | `nova_<name>(question)` |

Why proxy on the server? So the API key never reaches the browser, and so the
external call is itself pushed through `invoke()` — meaning **even a call to a
third-party service is scope-checked and audited** like any other spell.

The flow is always the same three steps:

```
   Configure  ──►  Expose as tool  ──►  Use it anywhere
   (base URL,      (registers a          (Agent Lab, Studio,
    key, options)   kb_/sql_/nova_ spell)  /query, /ask, curl)
```

---

## 9. Agents, Orchestrator, and Studio

- **Agent (`shabd_agent.py`).** An agent = a system prompt + a chosen set of
  spells (tools) + an LLM backend. The loop: send the prompt and tool schemas to
  the LLM → the LLM picks a tool and arguments → SHABD runs that spell (through
  the full pipeline!) → the result goes back to the LLM → repeat up to
  *max_steps* → final answer. `MockBackend` lets this run deterministically with
  no external LLM (used by tests and offline demos).

- **Orchestrator (`shabd_orchestrator.py`).** One layer up: given a free-text
  query, an `IntentClassifier` (keyword and/or semantic) routes it to the right
  *agent*; multi-agent **flows** run agents sequentially (chained) or in
  parallel (an LLM merges the answers). Public entry: `POST /ask` picks the agent
  for the caller. `CostTracker`/`BudgetExceeded` keep spend bounded.

- **Studio (`shabd_studio.py`).** The visual builder. It reads the same app's
  spells/agents/flows as a drag-drop **palette**, lets a non-developer wire them
  into an Assistant node, set a prompt/greeting, test live, and **Publish**. A
  published bot yields three things: a REST endpoint (`POST /chat/<name>`), a
  one-line embed widget (`<script src=".../embed/<name>.js">`), and a hosted chat
  page (`/c/<name>`). A published bot can itself become a palette node, so bots
  compose.

---

## 10. Trust & audit — Grimoire and Notary

- **Grimoire (`shabd.py`).** The append-only audit chain. Each *page* records a
  call (spell, subject, ok/fail, timestamp, redacted args) and commits to the
  previous page's hash. `grimoire.verify()` walks the chain and reports the first
  break, so tampering is detectable without a database or blockchain.

- **Notary (`shabd_notary.py`).** Takes trust across organisational boundaries.
  Entity A publishes a signed *root* over its Grimoire head; entity B
  *countersigns* it. Later, A can hand a regulator an *inclusion proof* that a
  specific call is in a head both parties signed — verifiable with a tiny stdlib
  function, no shared infrastructure.

Together they give "blockchain-grade" tamper evidence with plain Python and HMAC
— which is exactly the property regulated Indian financial workflows (CCIL/banking)
need on a restricted network.

---

## 11. Persistence — where state lives

- **The Grimoire** is a hash-chained JSONL file at `grimoire_log_path`
  (or SQLite/Postgres via `shabd_enterprise` if you opt in).
- **UI-created objects** (builder spells, agents, intents, flows, chains,
  chatbots, KBs, SQL services, Nova config, tokens metadata, LLM config) are
  saved to a **sidecar `.state.json`** next to the Grimoire file
  (`_save_state_file` / `_load_state_file`). On boot,
  `_recreate_dynamic_on_boot` re-registers every dynamic spell so nothing is
  lost across restarts.
- **No external datastore is required.** That is a core design promise: a single
  Python process, files on disk, done.

---

## 12. Change log / how to keep this current

When you change the system, edit the section above **and** append one line here.
Keep entries newest-first.

| Date | Author | What changed (and which section to check) |
|------|--------|-------------------------------------------|
| 2026-07-07 | Abhishek | Initial architecture & flow guide written. Covers core pipeline (§6), UI flow (§7), connector-as-a-tool pattern for KB/SQL/Nova (§8), agents/orchestrator/studio (§9), Grimoire/Notary (§10). |

### How to extend this doc safely
- **Adding a module?** Add a row to the [module map](#3-module-map--every-file-and-its-job)
  and, if it introduces a new front door or pipeline step, update §4 or §6.
- **Adding a UI page?** It almost always follows the §7 pattern — usually no new
  section is needed, just mention it in the module map row for `shabd_ui.py`.
- **Adding a new connector?** Add a row to the §8 table; the three-step
  Configure → Expose → Use flow should still hold — if it doesn't, say why.
- **Changing the invoke pipeline?** This is the highest-impact change in the
  codebase. Update the numbered list in §6 exactly, because people rely on the
  ordering (e.g. "validation happens before authz").
