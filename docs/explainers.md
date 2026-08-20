# Notary and Orchestrator — what they actually do

If the rest of SHABD made sense but these two felt fuzzy, this is for you.
Each section answers: **what is it, why exists, concrete example, where in the UI.**

---

## Part 1 · The Notary

### What it is, in one line
A way for **two separate organisations** (your bank + a partner NBFC, two
trading firms, you + your regulator) to **prove later** that nobody edited
their audit logs after the fact — **without using a blockchain**.

### Why it exists
You and a partner share a workflow:
* You approve a loan, they fund it (co-lending).
* You issue a payment, they settle it (CCIL members).
* You make a decision, they're audited on it (regulator).

If audit logs ever disagree later — yours says "₹50 lakh, Alice" and
theirs says "₹5 lakh, Alice" — who is lying?

Today's answers:
* Trust a central authority (regulator) → slow, manual.
* Use blockchain → expensive consensus, lots of ops, banks hate it.
* Use a notarised PDF → manual, human in the loop.

The SHABD Notary gives a fourth answer: **mutual countersignatures over
hash-chain heads, exchanged peer-to-peer.**

### Concrete worked example — Bank A and NBFC B do co-lending

#### Setup
* **Bank A** runs SHABD. Every loan approval lands as a Grimoire page,
  hash-linked to the previous one.
* **NBFC B** also runs SHABD. Same thing, different chain.
* They share secrets out-of-band (HMAC keys — like SSH keys).

#### A normal Monday
```
Bank A's chain         seq 0 → seq 1 → seq 2 → seq 3 → seq 4
                       (genesis) (Alice ₹1L)  (Bob ₹2L)  (Carol ₹5L)  (Dave ₹3L)
                                                                              ↑
                                                                              head
```

#### At end of day, Bank A "publishes a root"
Bank A snapshots the current chain head and signs it:

```python
root = {
  "entity": "bank-A",
  "head":   "9f3a…b2e1",   # the hash at seq=4
  "seq":    4,
  "pages_count": 5,
  "ts":     1735574400,
  "signature": "ab12…cdef",  # HMAC-SHA256(secret, canonical-json)
}
```

Bank A sends this `root` to NBFC B. Email, SFTP, SWIFT MT message,
HTTPS POST — doesn't matter. **No blockchain, no consensus.**

#### NBFC B "countersigns" it
B verifies A's signature, then signs over it:
```python
countersig = {
  "witness_of": "bank-A",
  "witnessed_signature": "ab12…cdef",
  "counter":   "bank-B",
  "counter_ts": 1735574460,
  "signature": "78ee…2342",   # HMAC-SHA256(B's secret, canonical-json)
}
```
B keeps a copy, sends a copy back to A.

#### What this proves
Suppose 3 months later somebody edits Bank A's chain page seq=1 to say
"Alice ₹10L" instead of "Alice ₹1L".

* Editing `args_hash` on page seq=1 → page seq=1's `hash` changes →
  page seq=2's `prev` doesn't match → **chain breaks at seq=2**.
* But the editor could try to recompute the whole tail:
  `hash[1] → hash[2] → … → hash[4]` to make it consistent.
* If they succeed, the new chain has a different head at seq=4.
* The Notary root B holds says `head = 9f3a…b2e1`. The new chain
  has a different head. **B's stored copy contradicts A.**
* B's countersignature was issued at `ts = 1735574460`, signed with
  B's key. A cannot forge B's key. **Tamper is provable.**

```
Regulator query: "Was Alice's loan ₹1L or ₹10L?"

Auditor reads:
  - Bank A's current chain (claims ₹10L)
  - Bank B's archived countersignature (commits to hash 9f3a…b2e1)
  - Replays A's chain from genesis with original page seq=1
  - Hash at seq=4 = 9f3a…b2e1 → matches B's commitment
  - Hash at seq=4 with the edit = different → contradicts B's commitment
  → Conclusion: A edited the chain.
```

#### What it's NOT
* It is **not** a blockchain — there is no consensus, no mining, no
  gas fees, no nodes that synchronise. Just two parties signing each
  other's chain heads peer-to-peer.
* It is **not** real-time — A and B exchange roots periodically
  (every hour, every day). Tampering between exchanges is detectable
  but not blocked.
* It does **not** require either party to share the chain contents.
  Only the hash head is shared — privacy is preserved.

### Where in the UI
* `/notary` page — Publish Root button (admin), see your published
  roots, peer roots received, countersignatures held.
* Tests at `tests/test_notary.py` — read these for the full state
  machine.

### When to use this vs not
**Use it when:**
* Your business has at least one partner or regulator who cares
  about your audit log.
* You want regulator-grade evidence without a blockchain stack.
* Audit forensics is a real possibility.

**Skip it when:**
* You're an internal-only tool (no second party to sign with).
* You don't have a strong reason to prove tamper to outsiders.

---

## Part 2 · The Orchestrator

### What it is, in one line
Routes a user's free-form question to the **right specialist agent**
based on what the question is about — like a smart receptionist at a
bank: "leave query goes to HR desk, VPN issue goes to IT helpdesk."

### Why it exists
You have N agents:
* HR Agent — answers leave / policy / attendance questions.
* IT Agent — raises tickets, troubleshoots VPN.
* Banking Agent — opens accounts, handles transfers.
* Trading Agent — places orders, checks positions.

User types: "kal ki chuti chahiye" (need tomorrow's leave).
Should this go to HR? IT? Banking? Obviously HR — but how does the
software know?

A single mega-agent that handles everything would:
* Waste tokens (HR queries shouldn't load trading tools).
* Make security risks (HR queries shouldn't even see the trading API).
* Be hard to debug (one giant log file).

Orchestrator solves this: route first, then run the right agent.

### How it decides — the 5-stage cascade

Each stage is FAST and CHEAP first, then SLOWER but smarter:

```
User query: "kal ki chuti chahiye"
                ↓
  Stage 1: KEYWORD MATCH (microseconds)
    Does any intent's keyword appear?
    HR intent has keywords: [leave, policy, holiday, chuti]
    "chuti" → MATCH (confidence 1.0) → route to HR. DONE.
```

If keyword fails, drop to stage 2:

```
  User query: "rest chahiye ek din"  (need rest one day)
                ↓
  Stage 1: KEYWORD → no match
                ↓
  Stage 2: SYNONYM MATCH (milliseconds)
    SHABD ships with synonyms: rest → leave, samasya → problem,
    kharab → broken, chuti → leave …
    "rest" → leave → HR has "leave" → MATCH (confidence 0.85)
```

If both fail:

```
  User query: "tomorrow off ke liye apply karna hai"
                ↓
  Stage 1,2: don't quite match
                ↓
  Stage 3: N-GRAM OVERLAP (milliseconds)
    Compare 2-grams and 3-grams of query vs each intent's description.
    HR description: "HR questions — leaves, attendance, policy."
    Overlap on "leave"/"off" semantics → confidence 0.65
```

If all three statistical methods fail:

```
  User query: "main kal nahi aa paunga"  (I won't be able to come tomorrow)
                ↓
  Stage 1,2,3: no clear winner
                ↓
  Stage 4: EMBEDDING SIMILARITY (~100 ms with sentence-transformers)
    Encode query + each intent description into vectors.
    Cosine similarity finds HR is closest (0.78).
```

If even that's tied or low confidence:

```
  Stage 5: LLM CLASSIFIER (~500 ms with Ollama)
    Send a tiny prompt: "Classify this query into [HR, IT, banking,
    trading, fallback]: 'main kal nahi aa paunga'"
    LLM returns: HR (confidence 0.95)
```

Each stage has a **confidence threshold**. If stage N hits it, we
short-circuit and stop. Most queries resolve in stage 1-2 (cheap).
Hard queries fall through to stage 5 (expensive but rare).

### Worked example — the receptionist analogy

Think of a bank lobby with 5 desks:

| Desk | Says yes when |
|---|---|
| **Desk 1 (eagle eyes)** | The customer says a magic word — "leave", "vpn", "trade" |
| **Desk 2 (multilingual)** | The customer's word translates to a magic word (chuti = leave) |
| **Desk 3 (statistician)** | The customer's phrases overlap with a department's brochure |
| **Desk 4 (psychologist)** | The customer's *meaning* is close to a department's (semantic) |
| **Desk 5 (the manager)** | Last resort — calls the LLM, costs more |

Customers walk in. Desk 1 grabs them first. If unsure, Desk 1 passes
to Desk 2. And so on until Desk 5 makes the final call.

### What "intent" means in code
```python
from shabd_orchestrator import Orchestrator, SemanticIntentClassifier
from shabd_agent import Agent, MockBackend

orch = Orchestrator(classifier=SemanticIntentClassifier(),
                    audit_app=app)

@orch.intent("hr",
             keywords=["leave", "policy", "holiday", "vacation",
                       "chuti", "rest"],
             description="HR — leaves, attendance, payroll.")
def _hr(_payload):
    return Agent(llm=MockBackend(["Your leave balance is 12 days."]),
                 system="You are an HR helper.")

@orch.intent("it",
             keywords=["ticket", "vpn", "laptop", "wifi", "kharab"],
             description="IT — raise tickets, fix laptops.")
def _it(_payload):
    return Agent(llm=MockBackend(["Ticket raised."]),
                 system="You are an IT helper.")

@orch.intent("fallback")
def _fb(_p):
    return Agent(llm=MockBackend(["I can help with HR or IT."]))

# Now route a query
result = orch.handle("kal ki chuti chahiye")
print(result)   # {'intent': 'hr', 'confidence': 1.0, 'via': 'keyword',
                #  'answer': 'Your leave balance is 12 days.'}
```

### Try it in the UI
* `/orchestrator` page — there's a text box. Type a query, click
  Classify. The result shows:
  * **intent** picked
  * **confidence** number (0.0 - 1.0)
  * **via** — which of the 5 stages decided (keyword/synonym/ngram/embedding/llm)
* Registered intents are listed below with their keywords.

### When to use this vs not
**Use it when:**
* You have multiple distinct agents and a single chat interface.
* You support multiple languages / colloquial inputs (Hindi mixed
  English).
* You want explainable routing — you can audit why a query went
  where.

**Skip it when:**
* You have one agent only.
* Every query is short and structured (a form, not free text).
* You're happy to let the LLM itself dispatch tool calls without
  intent routing.

### What the Orchestrator is NOT
* **Not** a multi-agent collaboration framework. It picks ONE agent
  per query. (For ensembles, use `shabd_agent.ConsensusBackend`.)
* **Not** a workflow engine — no branching, no parallel tasks. It's
  a router with explanation.
* **Not** a separate LLM call by default — stages 1-3 don't even
  touch an LLM, which is most of the win.

---

## Comparing them at a glance

| | Notary | Orchestrator |
|---|---|---|
| Layer | Cross-organisation evidence | Per-query routing inside one org |
| Network | Peer-to-peer message exchange | Local in-process function call |
| Frequency | Every hour / day (periodic) | Every user query |
| Failure mode | Tamper detected later, in court | Wrong agent picked, immediate |
| Latency budget | Minutes (out of band) | Milliseconds (in the request path) |
| Crypto | HMAC-SHA256 + Merkle-ish chain | None |

They solve very different problems and you'll usually want both:
the Orchestrator handles routing today; the Notary handles forensics
six months later.
