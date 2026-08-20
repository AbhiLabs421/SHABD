# 🔮 SHABD

## Spell Hub for Agentic Builders & Developers

_Turn your Python functions into spells that AI agents can cast._


---

## What does "SHABD" mean?

**SHABD** (शब्द) is the Sanskrit and Hindi word for **"word"** — and in many traditions, a word spoken with intent is a *mantra*: language that makes something happen in the world.

That is exactly what this framework does. You write a function, give it a name, and it becomes a **spell** — a unit of capability that an AI agent can invoke by name to act on the real world. SHABD is the **hub** where all your spells live, are discovered, and are cast.

---

## The idea in one breath

Modern AI models are brilliant at *thinking*, but they cannot *do* anything on their own. They cannot query your database, call your API, render a chart, or send an email. To act, they need **tools** — and someone has to build, secure, and serve those tools.

SHABD is that someone, packaged into a single Python file.

```python
from shabd import SHABD

app = SHABD("my-tools")

@app.spell
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b

app.serve(port=8765)
```

One decorator. Your function is now a spell — discoverable, validated, secured, and callable by Claude, GPT, or a local Ollama model.

---

## Who is this for?

**Agentic builders** — people building AI agents and assistants that need to *act*, not just chat. SHABD gives your agent a clean, safe set of tools with auth and rate limits built in, so you can hand capabilities to a model without handing over the keys to everything.

**Developers** — engineers who want to expose existing Python code to AI without rewriting it or learning a heavy framework. If you can write a function with type hints, you can write a spell. No new mental model, no dependency tree to manage.

**Teams shipping to production** — because SHABD doesn't stop at the protocol. Authentication, scopes, caching, rate limiting, an HTTP API, and a live dashboard come in the box, so the path from prototype to production is a straight line.

---

## Why "spells" instead of "tools"?

The vocabulary is deliberate, and it maps cleanly onto familiar ideas:

| SHABD term | What it really is |
|------------|-------------------|
| **Spell** | A function the AI can call (a tool) |
| **Resource** | Data the AI can read (files, records, API responses) |
| **Prompt** | A reusable instruction template |
| **Chain** | A pipeline of spells, cast in sequence |
| **Group** | A namespace for one project's spells |
| **Manifest** | The spellbook — everything the AI is allowed to cast |

The metaphor isn't just for fun. It captures the right intuition: a spell is **named**, has **defined inputs**, produces an **effect**, and should only be cast by those with the right **permission**. That's exactly how a well-designed tool behaves.

---

## What makes SHABD different

There are excellent libraries for connecting functions to AI — most of them focus on the **protocol** (the Model Context Protocol, MCP) and leave the rest to you. SHABD takes a different stance: the protocol is necessary but not sufficient. Real systems also need security, performance controls, observability, and a way for non-AI clients to call the same tools.

So SHABD includes all of it, with three promises:

1. **One file.** The entire framework is `shabd.py`. Read it, audit it, copy it anywhere.
2. **Zero dependencies.** Pure Python standard library. Optional extras (Redis, YAML) are exactly that — optional.
3. **Batteries included.** HMAC tokens, scopes, rate limiting, a TTL cache, a circuit breaker, an HTTP/SSE/WebSocket server, a live dashboard, and full MCP support — out of the box.

---

## Three things only SHABD has

These are not in FastMCP or any other MCP framework today:

### 🔮 Grimoire — a tamper-evident audit log

Every spell cast appends a **hash-chained, HMAC-signed page** to an audit log. Editing any past page breaks the chain — an external auditor can verify integrity in O(n) without ever seeing raw PII (PII args are hashed in their *redacted* form).

```python
app.invoke("transfer", {"amount": 5000})
app.grimoire.verify()
# {"ok": True, "pages": 1, "head": "79615f2d…"}
```

This makes SHABD a natural fit for regulated environments — EU AI Act, India's DPDPA, RBI guidelines, healthcare, finance.

### 🧾 Semantic Types — strings that mean something

```python
from shabd import Email, Aadhaar, GSTIN, IndianPhone, Money, URL

@app.spell
def onboard(email: Email, aadhaar: Aadhaar, gstin: GSTIN) -> dict:
    ...
```

Each type validates at the boundary, exposes its meaning in the JSON schema (`x-semantic`, `x-pii`, `pattern`, `example`), and PII-flagged fields are auto-redacted in the Grimoire log.

### 🤖 AI-native errors — built for LLMs to self-correct

Every error response carries a `hint`, `example`, and `did_you_mean` so the calling agent can fix itself without a human:

```json
{
  "error": {
    "code": "spell_not_found",
    "message": "no such spell: serach_docs",
    "hint": "Did you mean 'search_docs'?",
    "did_you_mean": ["search_docs"]
  }
}
```

---

## A spell for every need

```python
# A spell — the basic tool
@app.spell
def search_docs(query: str) -> dict:
    """Search the knowledge base."""
    ...

# A resource — data the AI can read
@app.resource("/docs/{slug}")
def get_doc(slug: str) -> str:
    """Return a documentation page."""
    ...

# A prompt — a reusable template
@app.prompt("code_review")
def review(language: str = "python") -> str:
    """A reusable code-review prompt."""
    ...

# A chain — spells cast in sequence
app.chain("search_docs | summarize | translate", name="smart_search")

# A group — a namespace for a whole project
finance = app.group("finance")

@finance.spell
def calculate_gst(amount: float) -> dict:
    ...
```

---

## Where spells can be cast

A spell you write once becomes available everywhere SHABD reaches:

- **Claude Desktop** and other MCP clients, over `app.mcp_stdio()`
- **Any HTTP client** — Node, Java, C#, curl — over `app.serve()`
- **Local LLMs** like Ollama, by reading the manifest and routing tool calls
- **Other languages** — a .NET MCP service and a Python SHABD service can call each other, because MCP is a protocol, not a language

The same function. No rewrites. Cast from anywhere.

---

## The philosophy

SHABD is built on a few convictions:

- **Small is powerful.** A single readable file beats a sprawling framework you can't fully understand.
- **No surprises.** Zero dependencies means nothing breaks under you when an upstream package changes.
- **Security is not optional.** Tools that touch real systems need auth and limits *by default*, not as an afterthought.
- **Words have power.** Naming a capability clearly — as a spell — is the first step to using it safely.

---

## Start casting

Read the [README](README.md) for the full feature tour, browse [`examples/`](examples/) for working code, or dive into the chapter-by-chapter docs under [`docs/`](docs/) for the complete manual.

```python
from shabd import SHABD
app = SHABD("my-first-hub")

@app.spell
def hello(name: str) -> str:
    """Greet someone."""
    return f"Hello, {name}!"

app.serve(port=8765)
# Open http://localhost:8765/dashboard and cast your first spell.
```

---

**SHABD** — Spell Hub for Agentic Builders & Developers

Developed by **Shanti** & **Abhishek** · Questions: **ipsabhi423@gmail.com**

If SHABD helps you build, leave a ⭐ on [GitHub](https://github.com/Kumar123ips/SHABD).

