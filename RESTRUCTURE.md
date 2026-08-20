# SHABD — new structure (frontend + microservices)

This document explains the restructured SHABD: a **React frontend** and an
independent set of **Python microservices**, plus a standalone **MCP stdio**
server. The original single-file modules still exist at the repo root; the new
stack lives under `frontend/`, `backend/`, and `shabd_stdio/`.

## Folder map

```
SHABD-dev/
├── frontend/                     React SPA (Vite). Talks ONLY to the gateway.
│   └── src/pages/                Login, Dashboard, Spells, Grimoire, Notary, Agent
├── backend/
│   ├── shabd_core/               the SHABD library (shared by every service)
│   │   ├── shabd.py, shabd_notary.py, shabd_users.py, shabd_agent.py …
│   │   ├── stable_secret.py      ← fixes the "Tamper detected" bug
│   │   └── demo_spells.py        one place to define your tools
│   ├── services/
│   │   ├── gateway/              :8000  single front door + login gate
│   │   ├── spells_service/       :8001  runs spells + owns Grimoire
│   │   ├── notary_service/       :8002  cross-entity witness
│   │   ├── users_service/        :8003  register/login, mints tokens
│   │   └── agent_service/        :8004  LLM agent (Ollama)
│   ├── run_all.py                dev launcher for all 5 services
│   └── .env                      OLLAMA_API_KEY etc. (gitignored)
├── shabd_stdio/server.py         MCP server for Claude Desktop (UI-independent)
├── shared/                       .shabd-secret + data/ (grimoire logs)
└── docker-compose.microservices.yml
```

**Why services can fail independently:** each service is its own process with
its own FastAPI app and its own Grimoire chain. They share only (a) the stable
secret file and (b) plain HTTP through the gateway. Restart `agent_service`
mid-conversation and `spells_service` never notices.

## Run it (dev)

```bash
# 1) backend — all 5 services in one terminal
cd backend
pip install -r requirements.txt
python run_all.py            # gateway on http://127.0.0.1:8000

# 2) frontend — in another terminal
cd frontend
npm install
npm run dev                  # http://127.0.0.1:5173  (proxies /api -> gateway)
```

Open http://127.0.0.1:5173 → **Register** (first account becomes superuser) →
explore Spells, Grimoire, Notary, Agent.

## The two things you asked about

### Grimoire and the "baar baar Tamper" message

The Grimoire signs every audit page with the app `secret`. On restart it reloads
the pages and re-verifies the signatures. If the secret **changed** between runs,
every signature fails and the dashboard says "Tamper detected" — even though
nobody tampered. The old launcher fell back to `SHABD_SECRET` *or* `"x"*32`, so a
run with the env var and a run without it used two different keys.

Fix: `backend/shabd_core/stable_secret.py` resolves the secret ONE way for every
process — env var if set, else a generated key persisted to
`shared/.shabd-secret` and reused forever. Now the chain stays green across
restarts and across services.

### Notary — with a real example

- **Grimoire** stops *you* from silently editing *your own* history.
- **Notary** stops two partners (say a Bank and an NBFC) from lying to *each
  other*. Each publishes a signed **root** (a snapshot of its chain head); the
  other **counter-signs** it. After that, neither can rewrite past history
  without invalidating the witness the other holds.
- An **inclusion proof** lets a regulator verify "decision #N existed at that
  moment" offline, without seeing the rest of the chain.

Try it on the **Notary** page: *Publish a root*, then *Build inclusion proof* for
seq `0`. You'll see `Proof valid`. If someone rewrites a past page, the head no
longer matches the countersigned root and the proof returns `head_mismatch`.

## MCP stdio (Claude Desktop) — now separate

```bash
python shabd_stdio/server.py
```

No UI, no login required. Add it to `claude_desktop_config.json` (see the file's
header for the exact JSON).
