# SHABD Studio — visual chatbot builder

A second, light-themed UI (separate from the heavy `shabd_ui` control
plane) where a non-developer drags tools, agents and flows onto a canvas,
wires them into an Assistant, sets a system prompt, tests it live, and
clicks **Publish** to get a ready API. Pure standard library, zero
dependencies — same philosophy as the rest of SHABD.

## What it is vs the main UI

| | `shabd_ui` (control plane) | `shabd_studio` (builder) |
|---|---|---|
| Audience | admins / ops | anyone |
| Look | dark dashboard | white drag-drop canvas |
| Job | manage spells, agents, flows, users, audit | compose & publish a chatbot |
| Backend | itself | **reuses `shabd_ui`** |

The Studio has no backend of its own — it calls the same `UIServer`, so
every tool/agent/flow you built in the main UI is instantly available as
a draggable palette node, and every chat turn lands in the same Grimoire
audit chain.

## Run it

Alongside the main UI (shares login — same host):

```bash
python -m shabd_ui --port 8080 --studio-port 8095
```

* Main UI:  `http://localhost:8080/`  (sign in here first)
* Studio:   `http://localhost:8095/`

The Studio accepts the cookie the main UI set at login (same host,
different port), so no separate sign-in.

## Build a bot

1. **Drag** tools / agents / flows from the left palette onto the canvas.
   Each becomes a node, auto-wired to the central **🤖 Assistant**.
2. Click the **Assistant** node → set its **system prompt**, **greeting**,
   and optionally **force tool use**.
3. Give the bot a **name** (top bar).
4. **Test** it live in the right-hand chat panel.
5. **Publish**.

A chatbot's toolset = the tools you dragged in **plus** the tools of any
agent you dragged in (merged, deduped).

## What Publish gives you

* **API** — call from any project, any language:

  ```bash
  curl -X POST http://localhost:8095/chat/<bot> \
       -H "Content-Type: application/json" \
       -d '{"message":"hello", "history":[]}'
  # {"ok":true, "bot":"<bot>", "reply":"...", "tools_used":[...]}
  ```

* **Embed** — drop one line into any website for a floating chat bubble:

  ```html
  <script src="http://localhost:8095/embed/<bot>.js"></script>
  ```

* **Hosted page** — a ready shareable chat page:

  ```
  http://localhost:8095/c/<bot>
  ```

## The unique bits

* **Composable bots.** A published bot shows up in the palette under
  **Bots**, so you can drag a bot into another bot — build a "bot of
  bots".
* **Tamper-evident conversation.** Every chat turn is appended to the
  Grimoire hash-chain as a `__chat:<bot>` page. You can prove later
  exactly what the bot said and when — compliance-grade, no separate
  logging stack.
* **Zero build, zero deps.** The drag-drop canvas, the wires, the embed
  widget and the hosted page are all plain HTML + vanilla JS served by
  stdlib Python. Nothing to `npm install`.

## Notes

* Set an LLM in the main UI's **Settings** (Ollama / OpenAI / Anthropic)
  for real answers. With no LLM, the bot replies with a placeholder.
* `/chat/<bot>` is public by default. Pass `Authorization: Bearer <token>`
  to record the caller's identity in the audit chain; an invalid token is
  rejected with 401.
* Bots persist in the same `state.json` sidecar as spells/agents/flows,
  so they survive a restart.
