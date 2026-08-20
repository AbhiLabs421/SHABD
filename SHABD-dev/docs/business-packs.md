# Business Packs

`shabd_packs/` is a collection of vertical packs that turn SHABD into a
revenue-shaped product for Indian financial-services customers. Each
pack is a one-line install and immediately gives you a set of spells
worth selling.

```python
from shabd import SHABD
from shabd_packs import sanctions, regtech, pretrade, ccil

app = SHABD("acme-bank", secret=os.environ["SHABD_SECRET"])

sanctions.install(app, lists=("OFAC", "UN", "RBI"))
regtech.install(app, regulator="RBI", entity_code="ACME-001")
pretrade.install(app)
ccil.install(app, member_id="MBR-ACME-001")
```

That's the whole onboarding — sanctions screening, regulator reports,
pre-trade risk gates, and a CCIL bridge are all live behind one HTTP
endpoint, with the Grimoire chain proving every call.

---

## 1. Sanctions & PEP screening — `shabd_packs.sanctions`

**Sales line:** "Sanctions / PMLA compliance in one import. Every
screening call lands in the Grimoire chain, so when FIU-IND auditor
asks 'when did you screen party X?', the answer is one cryptographic
proof, not a spreadsheet."

### Spells

| Spell | Purpose |
|-------|---------|
| `screen_party(name, country, dob_or_inc_date)` | Single-party screening |
| `screen_transaction(party_a, party_b, amount)` | Both parties at once |
| `list_status()` | Freshness check — auditors ask first |
| `refresh_lists()` | Operator-triggered refresh (audited) |

### Wire-in inside other spells

```python
from shabd_packs import sanctions

lister = sanctions.install(app)

@app.spell
def transfer(from_party: str, to_party: str, amount: Money) -> dict:
    sanctions.block_if_sanctioned(from_party, lister)
    sanctions.block_if_sanctioned(to_party, lister)
    ...
```

### Plugging in real lists

Default ships a tiny demo set. In production:

```python
class WorldCheckLister(sanctions.Lister):
    def refresh(self):
        ...   # fetch from Refinitiv / LSEG WorldCheck

sanctions.install(app, loader=WorldCheckLister(["OFAC", "UN", "RBI"]))
```

### Revenue model

* ₹2 L – ₹10 L per month per bank.
* Per-screening micro-fee for screening-as-a-service.

---

## 2. RegTech reports — `shabd_packs.regtech`

**Sales line:** "Auto-generate FIU-IND / RBI / SEBI reports from the
same audit chain the regulator wants to see. Every report is cryptographically
signed."

### Spells

| Spell | Purpose |
|-------|---------|
| `generate_ctr(date_yyyy_mm_dd)` | RBI Cash Transaction Report |
| `generate_str(case_id, summary, parties, amount)` | FIU-IND STR |
| `generate_form_61a(quarter, rows)` | High-value transaction report |
| `generate_digital_lending_audit(loan_id, ...)` | RBI digital-lending audit packet |
| `generate_ntrp(date)` | Non-Transactional Reporting Platform |

### Wiring to your data

```python
def my_transactions():
    return db.fetch_all("SELECT ts, amount, ccy, mode, ... FROM tx")

regtech.install(app, regulator="RBI", entity_code="BANK-123",
                txn_source=my_transactions)
```

### Revenue model

* ₹50 K – ₹5 L per month per bank as a compliance subscription.
* The RBI digital-lending audit packet is the single most-asked-for
  artefact from regulators in 2024-25 — pricing it standalone is fair.

---

## 3. Pre-trade risk gateway — `shabd_packs.pretrade`

**Sales line:** "Stop the next fat-finger before it leaves the box.
SEBI-style pre-trade limits + audit chain in two lines."

### Spells

| Spell | Purpose |
|-------|---------|
| `check_pre_trade(strategy, symbol, side, qty, limit_price)` | The check |
| `position_inquiry(strategy, symbol)` | Read-only inquiry |
| `reset_limits(strategy)` | Operator-only reset |

### Setting limits

```python
book = pretrade.install(app)
book.set_position_limit("momentum-alpha", "RELIANCE", 50_000)
book.set_notional_limit("momentum-alpha", 250_000_000)   # ₹25 cr/day
```

### Revenue model

* ₹3 L – ₹15 L per month per trading desk.
* For CCIL members: bundle with `shabd_packs.ccil` for ₹5 L+ per month.

---

## 4. CCIL bridge — `shabd_packs.ccil`

**Sales line:** "A single AI-tool surface for NDS-OM, repo, OTC
derivative reporting, and exposure inquiry — with the audit chain CCIL
will love."

### Spells

| Spell | Purpose |
|-------|---------|
| `book_repo(counterparty_mid, isin, qty, rate_pct, tenor_days)` | Repo segment |
| `book_ndsom_trade(counterparty_mid, isin, qty, price, side)` | NDS-OM outright |
| `report_otc_derivative(trade_id, counterparty, notional, tenor_days, product)` | TRP |
| `query_member_exposure(member_id)` | Read-only inquiry |

### Adapting to your CCIL gateway

```python
class MyMemberGateway(ccil.Backend):
    def submit_repo(self, payload):
        return my_ccil_client.repo_submit(payload)
    def submit_ndsom(self, payload):
        return my_ccil_client.nds_om_submit(payload)
    def submit_trp(self, payload):
        return my_ccil_client.trp_submit(payload)

ccil.install(app, backend=MyMemberGateway(), member_id="MBR-1234")
```

### Revenue model — the **B2B2B opportunity**

* Sell to CCIL itself: ₹50 L – ₹2 cr per year for the platform.
* CCIL then offers it to its ~250 members as part of their service
  tier. 20-30% revenue share.
* Per-trade micro-fee for TRP auto-submission.

---

## Composing packs with enterprise features

```python
from shabd_enterprise import (
    SQLiteGrimoirePersistence, RBACPolicyEngine, install_enterprise,
)
from shabd_packs import sanctions, regtech, pretrade, ccil

app = SHABD("acme-bank", secret=os.environ["SHABD_SECRET"])

sanctions.install(app)
regtech.install(app, regulator="RBI", entity_code="ACME-001")
pretrade.install(app)
ccil.install(app, member_id="MBR-ACME-001")

rbac = RBACPolicyEngine()
rbac.add_rule("compliance", allow_prefixes=["screen_*", "generate_*"])
rbac.add_rule("dealer",     allow=["book_repo", "book_ndsom_trade"])
rbac.add_rule("algo",       allow=["check_pre_trade"])

install_enterprise(
    app,
    sqlite_store=SQLiteGrimoirePersistence("/var/lib/shabd/audit.db"),
    rbac=rbac,
)

app.serve(port=8765)
```

That is your sellable demo. Five minutes to set up, one Grafana
dashboard, one Grimoire verification endpoint, end-to-end audit trail.

A complete worked example is in
[`examples/bank_full_stack.py`](../examples/bank_full_stack.py).
