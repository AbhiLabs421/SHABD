# Changelog

All notable changes to SHABD are documented here.

## [2.31.0] — 2026 — "Prove it" independent audit verifier + demo runbook

### Added

* **`shabd_verify.py` — independent, zero-dependency audit-log verifier.** Hand
  this ONE file plus the exported audit JSONL to an auditor / regulator: they
  re-derive the Grimoire SHA-256 hash chain themselves and confirm the log was
  not tampered with — no access to SHABD, no database, and **no secret required**
  for the tamper-evidence check. Pass `--secret` to also verify HMAC signatures
  (authenticity). CLI: `python -m shabd_verify audit.jsonl [--secret <hex>] [--json]`.
  10 tests, including proof that even a fully re-hashed forged chain is caught by
  the signature check.
* **`docs/DEMO.md` + `demo_spells.py`** — a copy-paste live-demo runbook (UI →
  Studio → published bot → API → enterprise backend → "prove it") and ready-made
  demo spells so the UI has content from the first launch.

## [2.30.0] — 2026 — Enterprise production stack (pure-Python, zero-dep)

A major step toward a sellable, enterprise-grade product: SHABD now ships
pure-Python, zero-dependency built-ins for the services an enterprise usually
installs — identity (Keycloak-class), cache/coordination (Redis-class) — each
with an **external alternative selectable in `config.yaml`**. No Docker, no
downloaded images; air-gap friendly. See `docs/PRODUCTION-READINESS.md`.

### Added

* **`shabd_praman.py` — built-in identity provider ("Praman").** OAuth2/OIDC-style
  server: password / refresh (rotating) / client-credentials grants, `/userinfo`,
  `/introspect`, `/revoke`, discovery; **HS256 or RS256** tokens (RS256 publishes
  a JWKS so external services verify offline); **TOTP MFA** (RFC-6238),
  brute-force lockout, password policy; alg-substitution defense; standalone
  `PramanServer`. Config-selectable **builtin (Praman) or external Keycloak**.
* **`shabd_rsa.py` — pure-Python RSA** (Miller-Rabin keygen, RSASSA-PKCS1-v1_5/
  SHA-256, JWK export) backing RS256/JWKS with no C-extension dependency.
* **`shabd_smriti.py` — built-in cache/coordination server ("Smriti").** RESP-subset
  wire protocol (real `redis-py` clients work too), optional AUTH + append-only-file
  persistence; `SmritiCache` slots into the `ConjurePlugin` cache + distributed
  rate-limit hooks. Config-selectable **in-process / Smriti / external Redis**.
* **`shabd_config.py` — single production control surface.** One `config.yaml`/`.json`
  builds the selected identity/cache/persistence/secret providers; zero-dep YAML
  subset parser (PyYAML used when present); secret sourcing (env/file/inline);
  **CLI**: `python -m shabd_config --config config.yaml` launches the built-in
  servers. `config.example.yaml` documents the full posture.
* **Tamper-evident identity audit.** `grimoire_audit_bridge(app)` writes every
  Praman auth event into the Grimoire hash chain (auto-wired via
  `build_identity(app=…)`) — identity history is cryptographically verifiable,
  unlike a stock IdP's editable event table. Proven by test.
* **Browser security headers.** Every UI response now carries a Content-Security-
  Policy, Strict-Transport-Security, and Permissions-Policy (plus the existing
  frame-deny / nosniff / referrer-policy); toggle via `ui.security_headers`.
* **`SECURITY.md`** extended with an enterprise-auth section and honest caveats.

### Notes

* ~95 new tests across `test_praman`, `test_rsa`, `test_smriti`, `test_config`.
* Praman is **POC-grade**: custom auth/crypto must get an independent security
  review + pen-test before it is the auth of record for real money. HS256 and
  external-Keycloak remain the fallbacks. "More secure than Keycloak" is **not**
  claimed; the defensible edge is *smaller attack surface, air-gap-native,
  tamper-evident audit*.

## [2.9.0] — 2026

### Added — `shabd_ui.py` becomes truly "build no-code" plus a remote-client console

Previous v2.8 UI let users *operate* SHABD from a browser but creating
spells, issuing tokens and editing scopes still required Python code.
v2.9 closes that loop and adds a remote client console.

  * **Spell Builder** — `/builder` (superuser only): paste Python in a
    textarea, the server compiles it inside a curated `_SAFE_BUILTINS`
    namespace (no `os`, `subprocess`, `open`, `eval`, `exec`), registers
    the function via `app.spell(...)` and writes an audit page to the
    Grimoire chain. UI-created spells can be deleted from the UI;
    code-declared spells stay read-only.
  * **Issue Token** — `/tokens` (admin only): form to mint scoped
    bearer tokens via `app.issue_token(subject, scopes, ttl)`. TTL
    clamped to `[60 s, 7 d]`. Every issuance audited.
  * **Scope editor** — `/scopes` (admin only): table of every spell
    and its required scopes; edits take effect immediately for new
    calls.
  * **Client Console** — `/client` (any signed-in user): server-side
    proxy that uses `SHABDClient` to ping, fetch manifest, verify
    Grimoire, and invoke spells on **any other SHABD HTTP server**.
    Brings every SHABD/MCP server you have into one browser tab,
    avoiding CORS by routing through the UI's own backend.
  * New routes: `POST /api/spells/create`, `POST /api/spells/{n}/delete`,
    `GET /api/scopes`, `POST /api/scopes/{n}`, `POST /api/tokens/issue`,
    `POST /api/client/call`.
  * RBAC enforced server-side: superuser-only for builder, admin-only
    for tokens & scopes, any-session for client console.
  * 36 new tests across four difficulty tiers (Easy, Medium, Hard,
    Complex) — covering the sandbox, RBAC, CSRF, two-server proxy
    flows, and a complete no-code lifecycle (build → scope → token →
    invoke via SHABDClient and via the console).

## [2.8.0] — 2026

### Added — `shabd_notary.py` (cross-entity audit anchoring)
Two regulated parties (bank ↔ NBFC, NBFC ↔ regulator, CCIL member ↔
CCIL, etc.) can now achieve blockchain-grade audit immutability *without
a blockchain*. Each party periodically signs its current Grimoire head
into a `NotaryRoot`, ships it to the peer, and stores the peer's
`Countersignature` over its own root. After exchange, neither party can
edit past chain pages without invalidating the other party's stored
countersignature. Verifiers (regulators, auditors) verify roots and
build inclusion proofs offline with stdlib-only helpers.

  * `AgentNotary` — bound to a SHABD `app`; publishes roots, receives
    peer roots, countersigns, and builds inclusion proofs.
  * `NotaryRoot`, `Countersignature`, `InclusionProof` — signed JSON
    data classes.
  * `verify_root`, `verify_countersignature`, `verify_inclusion` —
    pure-function verifiers (no SHABD app needed).

### Added — `shabd_ui.py` (production no-code dashboard)
Single-file, stdlib-only HTTP/HTML/JS UI exposing every SHABD module
through a polished browser dashboard. Designed for the TCS Ultimatix
shape (Keycloak password-grant SSO):

  * Auth — Keycloak OIDC password grant, or env-var bootstrap login
    for local dev.
  * RBAC — three roles enforced server-side: `superuser`, `admin`,
    `user`. Roles come from Keycloak `realm_access.roles` plus
    `SHABD_SUPERUSERS` / `SHABD_ADMINS` allow-lists.
  * Sessions — in-memory by default, HttpOnly cookie, per-session
    CSRF token on every privileged action.
  * Pages — Dashboard, Spells (form-rendered from JSON schema),
    Grimoire chain explorer, filterable Audit Log, no-code Agent Lab,
    Orchestrator (with live intent classifier), Notary panel, Users
    (admin), Settings.
  * Login throttle (5 attempts / 30 s per username).
  * Security headers (X-Frame-Options: DENY, X-Content-Type-Options,
    Referrer-Policy: same-origin).

### Added — tests
  * `tests/test_notary.py` — 8 tests covering root signing, peer
    countersignature, inclusion-proof verification, and the
    full bank ↔ NBFC co-lending flow.
  * `tests/test_ui.py` — 14 tests covering JWT decode, bootstrap auth,
    login throttle, full live HTTP round-trip, CSRF enforcement,
    RBAC helpers.

### Added — examples + docs
  * `examples/ui_production.py` — copy-paste production runner with
    5 example spells, orchestrator, notary, Keycloak config.
  * `docs/ui.md` — step-by-step deployment guide (Keycloak,
    nginx, systemd, Docker) and hardening checklist.

### Tests
  * Total: **147 stdlib-only tests, all passing** (was 125).

## [2.7.1] — 2026

### Added — `shabd_orchestrator.SemanticIntentClassifier`
Five-stage classifier that escalates from cheapest to costliest:

  1. Exact keyword match (free).
  2. Word-boundary match against an expanded synonym set, with a
     bundled `ENTERPRISE_SYNONYMS` dictionary covering Hindi
     transliterations (chuti / samasya / kharab), common acronyms
     (PTO / WFH), and tool-vendor language. Word boundaries kill the
     whole class of "`laptop` matches `pto`" substring bugs.
  3. Character n-gram cosine over keywords + description — typo
     tolerant, pure stdlib.
  4. Embedding cosine via any OpenAI-shaped `/v1/embeddings` endpoint
     (`OpenAICompatEmbeddings`). Opt-in, with a per-intent embedding
     cache so the catalogue is only embedded once.
  5. LLM classifier (inherited from `IntentClassifier`).

All five stages share a single API; the user picks which stages to
enable. Stages 1-3 run in pure standard library, so a deployment
with no embeddings endpoint or LLM still gets semantic routing.

### Tests
- 9 new tests in `tests/test_orchestrator.py` cover the synonym
  expansion, Hindi transliteration, acronym match, word-boundary
  protection against spurious substrings (`laptop` vs `pto`), n-gram
  typo tolerance, custom user synonyms, embedding-stage routing, and
  fallback for genuinely unrelated queries.
- Total suite: **125 stdlib-only tests, all passing.**

## [2.7.0] — 2026

The "Main Orchestrator in one file" release. Adds `shabd_orchestrator.py`
— a zero-dependency, single-file replacement for the LangGraph / LangChain
"intent → sub-agent → response" pattern that every serious enterprise AI
deployment converges on.

### Added — `shabd_orchestrator.py`
- `Orchestrator` — registers named intents, classifies each query,
  dispatches it to the matching sub-agent, and stamps the same
  Grimoire audit chain for every step. Decorator and imperative
  registration styles both supported.
- `IntentClassifier` — two-stage: keyword pass first (deterministic,
  no LLM cost), then LLM fallback with the intent catalogue in-prompt.
  Survives an LLM outage gracefully.
- `LLMFallbackChain` — wraps several backends in order. The next one is
  used as soon as the current raises. Classic enterprise pattern:
  try GPT-4o; fall back to self-hosted QWEN3 on rate-limit / 5xx.
- `CostTracker` + `TokenPriceTable` — per-session ₹ budget enforcement.
  Hard-stop at budget instead of discovering the overspend next month.
- Per-intent invariants and provenance tracking carry over from the
  agent layer with one declaration at the orchestrator.

### Added — tests + example
- `tests/test_orchestrator.py` — 11 new tests covering keyword
  classification, LLM-fallback classification, budget enforcement,
  fallback chain on failures, audit-chain population, and the
  decorator form.
- `examples/orchestrator_demo.py` — three-intent (Policy / AiOps /
  Fallback) walkthrough that runs fully offline.

### Tests
- Total suite: **116 stdlib-only tests, all passing** (was 105).

## [2.6.0] — 2026

The "genuinely novel agent features" release. Three things no other
agent framework ships today, all layered onto the existing
`shabd_agent.py` without breaking the v2.5 API.

### Added — `shabd_agent.ConsensusBackend`
Multi-LLM consensus for high-stakes tool calls. Calls N backends in
parallel; only forwards the tool call if at least `min_agreement` of
them returned the *same* tool name + canonical args. A single
hallucinating model can no longer push a ₹10 cr transfer through. If
quorum fails, the agent loop receives a structured `consensus_failed`
error and the LLM can replan — it does not crash.

### Added — `shabd_agent.ProvenanceTracker`
Every tool argument the LLM passes is tagged with its origin:
`user`, `tool:<name>`, `system`, or `llm_invented`. The tracker
indexes user prompts and tool outputs token-by-token, so a fabricated
Aadhaar, account number, or amount appears as `llm_invented` in the
step trace. Pair with an invariant to refuse to execute high-stakes
spells whose critical arguments are `llm_invented` — straightforward
prompt-injection defence.

### Added — `shabd_agent.Invariant` + `AgentSession`
Declarative cross-tool safety rules. Register as a decorator
(`@agent.invariant("daily_cap_2L")`) or imperatively
(`agent.add_invariant(name, check, message)`). Each rule sees the
full session-so-far including the pending tool call, and returning
`False` blocks the tool body from running. The block round-trips to
the LLM as a `tool` role `InvariantViolation` so the model can
replan rather than abort.

### Tests
- 12 new tests in `tests/test_agent_novel.py` covering quorum cases,
  fallback to LLM on consensus failure, provenance classification,
  invariant blocking, and composition of all three features.
- Total suite: **105 stdlib-only tests, all passing.**

### Example
- `examples/agent_novel_features.py` — fully offline demo that runs
  the consensus, provenance, and invariant features end-to-end.

## [2.5.0] — 2026

The "universal agent runtime" release. Adds `shabd_agent.py` — a
single-file, zero-dependency agent loop that works against any LLM
provider without installing `openai`, `anthropic`, `google-genai`,
`langchain`, or `llama-index`. Pure standard library.

### Added — `shabd_agent.py`
- `Agent` — the loop, with `max_steps`, hard timeout, duplicate-step
  detection, structured tool-error round-trip, and an `Agent.from_shabd`
  factory that binds tools to an existing SHABD app (so every tool
  call gets validation + RBAC + audit + idempotency for free).
- `ToolRegistry` — registers Python functions as tools; auto-derives
  the JSON schema from type hints; routes through SHABD if bound.
- `ToolError` — structured error with `hint`, `example`, `did_you_mean`
  so the LLM can self-correct in the very next turn.
- `OpenAICompatBackend` — covers OpenAI, Ollama (`/v1`), vLLM, LM Studio,
  Together, Groq, Mistral, LiteLLM, Fireworks, OpenRouter, Anyscale,
  Perplexity, DeepInfra. Any host that speaks the OpenAI shape.
- `AnthropicBackend` — Anthropic Messages API, including correct
  `tool_use` / `tool_result` block handling.
- `GeminiBackend` — Google `generateContent` API with `functionCall` /
  `functionResponse` conversion.
- `MockBackend` — deterministic, for offline tests and demos.
- `AssistantTurn`, `AgentResult`, `AgentStep` — normalized data types
  so the agent loop never has to branch on provider.

### Added — examples
- `examples/agent_universal.py` — one file, one CLI flag picks the
  backend. Default is `MockBackend` so it runs offline.

### Tests
- 13 new tests in `tests/test_agent.py` covering the registry, the
  loop, SHABD-bound dispatch, a live HTTP backend against a stub
  OpenAI server, and Anthropic + Gemini message conversion.
- Total suite: **93 stdlib-only tests, all passing.**

## [2.4.0] — 2026

The "fill in the remaining revenue packs" release. Closes the gap
against the published Top-10 revenue list and the bank/exchange
engineering checklist.

### Added — `shabd_enterprise.py`
- `PostgresGrimoirePersistence` — PostgreSQL / Oracle backend for the
  Grimoire chain. Schema mirrors `SQLiteGrimoirePersistence` so
  migration between the two is a `pg_dump` away. Drivers
  (`psycopg2-binary`, `oracledb`) import lazily.
- `install_enterprise(..., postgres_store=...)` wiring.

### Added — `shabd_packs/`
- `aml` — velocity / structuring / beneficial-owner checks with a
  `block_if_structuring()` helper for in-spell composition.
- `dpdpa` — DPDPA Consent Vault: `record_consent`, `withdraw_consent`,
  `verify_consent`, `data_subject_request`, `consent_audit_log`.
- `surveillance` — wash-trade, spoofing, layering, front-running and
  UPSI-window insider-alert detectors (SEBI Reg 9A shape).
- `reconciliation` — bulk ingest + break detection between external
  and internal settlement feeds.
- `algo_lifecycle` — registration → test → approval-request → signed
  approval → deployment → retirement lifecycle, all auditable.
- `reconstruction` — Trade-Reconstruction-as-a-Service: by trace_id,
  by spell, by subject, and a Merkle-style proof slice for external
  auditors; plus `replay_call` for in-memory replay.

### Tests
- 12 new tests in `tests/test_packs2.py` covering every new pack.
- Total suite: **80 stdlib-only tests, all passing.**

### Notes on the revenue-feature checklist
Done: RegTech reports, AML/Fraud, Pre-trade Risk, Trade Reconstruction,
CCIL Trade Reporting, AI Decision Audit (RBI digital-lending),
DPDPA Consent Vault, HSM + mTLS + Active-Active, Settlement
Reconciliation, Trade Surveillance, Algo Approval Audit, PostgreSQL
backend. The remaining "Market Maker Inventory Manager" is left as
an example template; "real Raft-lite consensus" remains a stub
because it should be backed by an external coordinator (etcd /
Consul) rather than implemented from scratch.

## [2.3.0] — 2026

The "bank / exchange / regulated industry" release. Adds two new
optional namespaces that turn SHABD into a sellable, vertical-shaped
product for Indian financial-services customers — without breaking
the core "single file, zero deps" promise.

### Added — `shabd_enterprise.py` (one optional file)
- `EnvKeyProvider`, `FileKeyProvider`, `HSMKeyProvider` — pluggable key
  sources for rotation and HSM-backed signing.
- `LDAPAuthProvider`, `SAMLAuthProvider`, `SSOTokenExchanger` — enterprise
  identity bridges.
- `RBACPolicyEngine` — declarative role-to-spell allow/deny matrix with
  prefix matching and per-attribute requirements.
- `SeparationOfDutiesPolicy` — dual-control for sensitive spells (wire
  transfers, treasury operations).
- `SQLiteGrimoirePersistence` — WAL-mode SQLite store for the audit
  chain with indexed by-trace lookup.
- `EncryptedGrimoireJSONL` — AES-GCM-at-rest wrapper (needs
  `cryptography`).
- `X509Signer` — append an X.509-signed page hash for courtroom-grade
  non-repudiation (needs `cryptography`).
- `MTLSConfig` + `install_mtls_on` — mutual TLS with optional CN
  allowlisting.
- `OTLPSpanExporter` — push OTLP/HTTP-JSON spans to Tempo / Jaeger /
  DataDog with no extra deps.
- `KafkaAuditStreamer` — Kafka producer for the audit chain
  (`kafka-python` optional, stdlib TCP fallback for demos).
- `PrometheusPushGateway` — push metrics to a Pushgateway from a batch
  job.
- `ClusterPeer` + `HAGrimoireCoordinator` — peer-to-peer replication
  for two- or three-node active-active.
- `install_enterprise(app, ...)` — bundled installer that wires
  whichever components you pass in.

### Added — `shabd_packs/` (revenue-shaped imports)
- `shabd_packs.sanctions` — `screen_party`, `screen_transaction`,
  `list_status`, `refresh_lists` + a `block_if_sanctioned()` helper.
- `shabd_packs.regtech` — RBI / FIU-IND / Form-61A / RBI digital-lending
  report generators with semantic-typed inputs.
- `shabd_packs.pretrade` — pre-trade risk gateway with position and
  notional limits, semaphore-backed for sub-millisecond checks.
- `shabd_packs.ccil` — NDS-OM repo / outright, OTC derivative TRP,
  member exposure inquiry with pluggable backend.

### Added — examples
- `examples/bank_full_stack.py` — composes every pack and every
  enterprise extra into a single ~5 minute "bank-grade tool layer"
  demo.
- `examples/flowise_integration.py` — Flowise Custom Tool + OpenAPI
  Toolkit walkthrough.

### Added — docs
- `docs/enterprise-features.md` — every enterprise component, when
  to use it.
- `docs/business-packs.md` — every revenue pack with pricing model.
- `docs/feature-map.md` — one-page mental model: what runs when, in
  what order.
- `docs/flowise-integration.md` — step-by-step Flowise integration
  with two approaches.

### Changed
- `_before_hooks` no longer swallows `ConjureError`. RBAC, SoD, and
  sanctions hooks need exceptions to propagate; benign hook failures
  still log and continue.
- `RBACPolicyEngine.allow_prefixes` accepts `"finance_*"`, `"finance.*"`,
  and `"finance*"` shapes interchangeably.

### Tests
- 31 core + 18 enterprise + 19 enterprise-2 + packs = **68 tests**,
  all stdlib-only, all passing.

## [2.2.0] — 2026

The "production-ready, banks-and-trading-grade" release.

### Added — enterprise observability & safety
- **Prometheus exposition** at `/metrics` (via `Accept: text/plain;version=0.0.4`
  or `?format=prom`). Includes counters and per-spell summary quantiles.
- **W3C TraceContext** — incoming `traceparent` is parsed and used as the
  parent of the current span. Every `Context` exposes `traceparent()` for
  outbound propagation. The bundled `SHABDClient` propagates it automatically.
- **Kubernetes-style probes** — split `/healthz` (liveness), `/readyz`
  (readiness; 503 during drain), `/startupz` (warmup).
- **Graceful shutdown** — SIGTERM handler that flips `/readyz` to 503,
  refuses new calls (`shutting_down`), and waits up to `shutdown_grace_s`
  for in-flight calls to drain before closing.

### Added — banking-grade safety
- **`Idempotency-Key` support** — first call with a key executes and is
  recorded; subsequent calls with the same key + same body replay the
  recorded response; same key + different body raises `idempotency_conflict`.
- **Per-spell concurrency limits** — `@app.spell(max_concurrent=N)` caps
  in-flight calls via an asyncio semaphore (protects fragile downstreams).
- **Zero-downtime secret rotation** — `additional_secrets=[old_key]` keeps
  tokens signed with a previous key valid during the rotation window.

### Added — audit chain extensions
- **`GrimoireJSONL`** — append-only on-disk persistence. The chain
  auto-loads + verifies at startup and `fsync`'s on every page.
- **`AuditWebhook`** — async, HMAC-signed POST of every Grimoire page to
  an external SIEM URL. Failures are logged but never block the call path.

### Added — agent SDK
- **`shabd_client.py`** — single-file, zero-dependency Python client with
  bearer auth, automatic `traceparent` propagation, `Idempotency-Key`
  support, network retries with backoff, and `tools_for_openai()` /
  `tools_for_anthropic()` helpers.

### Added — deployment artifacts
- `Dockerfile` (non-root, slim, healthcheck on `/readyz`)
- `docker-compose.yml` with Prometheus + Grafana
- `deploy/prometheus.yml` scrape config
- `deploy/k8s.yaml` (probes, PDB, runAsNonRoot, terminationGracePeriodSeconds)
- `deploy/shabd.service` (hardened systemd unit)
- `.github/workflows/ci.yml` (lint + type-check + tests on Python 3.10-3.13,
  live FastMCP comparison, Docker build + smoke test)

### Added — examples
- `examples/bank_transfer.py` — semantic types + idempotency + audit chain
- `examples/trading_orders.py` — concurrency caps + idempotency
- `examples/agent_with_shabd.py` — agent loop using `SHABDClient`

### Added — docs
- `docs/usage-book.md` — step-by-step "zero to production" walkthrough
- `docs/production-deployment.md` — secrets, persistence, drain, K8s, systemd
- `docs/observability.md` — Prometheus, traces, logs, sample PromQL
- `docs/agent-sdk.md` — `SHABDClient` reference + OpenAI/Anthropic loops
- `docs/runbook.md` — on-call symptom → signal → fix

### Tests
- 31 core tests + 18 new enterprise tests = 49 stdlib-only tests.

## [2.1.0] — 2026

The "be honest, ship the things only we have" release.

### Added — features no other MCP / tool framework ships today
- **Grimoire** — a hash-chained, HMAC-signed, tamper-evident audit log. Every spell cast appends an immutable page that commits to the previous page's hash. PII args are hashed in their redacted form, so external auditors can verify integrity without ever seeing raw values. HTTP: `GET /grimoire/verify`, `/grimoire/head`, `/grimoire/pages`.
- **Semantic Types** — first-class `Email`, `IndianPhone`, `Aadhaar`, `GSTIN`, `Money`, `URL` types that validate at the boundary, surface their meaning in the JSON schema (`x-semantic`, `x-pii`, `pattern`, `example`), and trigger automatic PII redaction in the Grimoire log.
- **AI-Native Errors** — every error now returns `hint`, `example`, and `did_you_mean` so the calling LLM can self-correct without a human. Spell-name typos auto-suggest the closest match.

### Fixed — honesty pass
- `tests/` directory now actually exists. 31 stdlib-only tests covering core, validation, AI-native errors, semantic types, Grimoire, auth, MCP surface, and the HTTP server.
- `examples/` directory now actually exists with runnable demos for every major feature.
- The FastMCP comparison table in the README is now verifiable — `tests/test_comparison.py` runs both servers side-by-side and prints the matrix at runtime. The prior table mis-stated that FastMCP lacks auth, rate limiting, and caching; in FastMCP 3.x these all ship in `fastmcp.server.middleware`. Those rows are corrected.

## [2.0.0] — 2026

The complete framework release. SHABD is now a full AI-function framework, not just an MCP server.

### Added — MCP feature parity
- **MCP Resources** via `@app.resource("/uri/{var}")` — expose files, database records, and API data. Appears in Claude Desktop's Resources tab.
- **MCP Prompts** via `@app.prompt("name")` — reusable prompt templates. Appears in Claude Desktop's Prompts tab.
- **Image and File returns** via `SpellImage` and `SpellFile` — render images inline and offer file downloads.
- **Python 3.10+ support** — works on 3.10, 3.11, and 3.12.

### Added — unique features
- **Spell Chains** via `app.chain("a | b | c")` — connect tools into a pipeline where each step feeds the next.
- **YAML Spells** — define REST-API-backed tools entirely in `config.yaml` with no Python.
- **Hot Reload** via `app.serve(hot_reload="my_spells.py")` — reload tools on file change without restarting.
- **Live Dashboard Playground** — test tools interactively from the browser; forms are auto-generated from schemas.
- **CPM config generation** via `app.cpm_config()` and the `/cpm-config` endpoint.
- **Call Replay** via `POST /replay/{trace_id}` — re-run any past call for debugging.
- **Multi-project Groups** via `app.group("name")` — namespace tools for multiple projects on one server.

### Changed
- Canonical class name is now `SHABD`. `Conjure` remains available as a backward-compatible alias.
- Secret environment variable is `SHABD_SECRET` (also accepts `CONJURE_SECRET`).

### Notes
- Still a single file, still zero required dependencies.

## [1.x] — earlier
- Initial framework: `@app.spell` tools, HTTP/SSE/WebSocket/stdio transports, HMAC auth, scopes, rate limiting, circuit breaker, TTL cache, live dashboard, MCP client proxy, and YAML configuration.
