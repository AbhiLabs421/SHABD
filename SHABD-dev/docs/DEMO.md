# SHABD — Live Demo Runbook

Everything you need to give a full live demo: control-plane UI → Studio →
published bot → API → enterprise backend. Zero dependencies, no Docker.

## 0. Setup (once)

```bash
git clone https://github.com/Kumar123ips/SHABD.git
cd SHABD && git checkout dev
python3 --version                 # 3.10+  (no pip install — stdlib only)
export SHABD_SECRET=$(python3 -c "import secrets;print(secrets.token_hex(32))")
```

## 1. Launch UI + Studio (one command)

```bash
python3 -m shabd_ui --spells demo_spells.py \
        --bind 127.0.0.1 --port 8080 --studio-port 8095 --foreground
```

- Control-plane UI → <http://127.0.0.1:8080/>
- Visual Studio    → <http://127.0.0.1:8095/>

Optional: `--fastapi-port 8000` (needs `pip install fastapi uvicorn`).
Drop `--spells demo_spells.py` to start empty and build in `/builder`.

## 2. Browser walkthrough (control-plane UI)

1. `/register` — first account becomes **superuser**.
2. Theme switch (bottom-left) → pick a palette.
3. `/spells` — invoke `discount` (price 1000, pct 15) → `{"final": 850.0}`.
4. `/builder` — paste Python, "Register spell" (superuser only; audited).
5. `/knowledge` — create a KB, paste text, **Expose as tool** → `kb_<name>`.
6. `/agent` — new agent, tick tools, Save, Run.
7. `/settings` — set the LLM backend (Ollama/OpenAI/Anthropic) for real answers.
8. `/tokens` — mint a scoped bearer token (for the API demo below).
9. `/grimoire` — show the tamper-evident audit chain verifying green.

## 3. Studio → build a bot → publish

1. Open <http://127.0.0.1:8095/>.
2. Name the bot (e.g. `sales_bot`); edit the Assistant node's system prompt.
3. Drag a tool (e.g. `discount`) onto the canvas and wire it to the Assistant.
4. **Publish** → you get:
   - API:    `POST http://127.0.0.1:8095/chat/sales_bot`
   - Embed:  `<script src="http://127.0.0.1:8095/embed/sales_bot.js"></script>`
   - Hosted: `http://127.0.0.1:8095/c/sales_bot`  ← open in a new tab and chat.

## 4. Show the API (terminal)

```bash
# published bot — one message:
curl -X POST http://127.0.0.1:8095/chat/sales_bot \
     -H "Content-Type: application/json" -d '{"message":"1000 pe 15% off?"}'

# a spell directly (scoped token from /tokens):
curl -X POST http://127.0.0.1:8080/spells/discount \
     -H "Authorization: Bearer <TOKEN>" \
     -H "Idempotency-Key: $(uuidgen)" -d '{"price":1000,"pct":15}'

# MCP-compatible tool manifest — every tool in one place:
curl -s http://127.0.0.1:8080/manifest | head
```

## 5. Enterprise backend — Praman identity + Smriti cache

```bash
cp config.example.yaml config.yaml
python3 -m shabd_config --config config.yaml       # Praman :8899 + Smriti :6390

# in another terminal:
curl -X POST http://127.0.0.1:8899/praman/token \
     -d grant_type=password -d username=<u> -d password=<p>
curl -s http://127.0.0.1:8899/.well-known/openid-configuration
curl -s http://127.0.0.1:8899/praman/jwks
```

## 6. "Prove it" — hand the auditor an independent verifier

```bash
# anyone can verify the audit chain was not tampered with — no secret needed:
python3 -m shabd_verify shabd-audit.jsonl
```

## Stop

```bash
# Ctrl-C if --foreground, else:
pkill -f shabd_ui
```
