# Enterprise Features

Everything in this chapter lives in `shabd_enterprise.py`. The core
`shabd.py` keeps its single-file, zero-dependency promise; this sidecar
adds the boxes you have to tick in a bank / exchange procurement form.

Mental model:

```
+--------------------------------------------------------------+
|  shabd.py            <-- core, zero deps, single file        |
|     SHABD app, semantic types, Grimoire (in-memory)          |
+--------------------------------------------------------------+
|  shabd_enterprise.py <-- optional, all stdlib where possible |
|     HSM, RBAC, SQLite chain, OTLP, mTLS, clusters, etc.      |
+--------------------------------------------------------------+
|  shabd_packs/*.py    <-- optional, revenue-shaped packs      |
|     sanctions, regtech, pretrade, ccil                       |
+--------------------------------------------------------------+
```

Each class below is a small, named building block. Compose them through
the `install_enterprise(...)` one-liner, or wire them by hand if you
need a non-standard order.

---

## Quick reference

| Feature | Class | Run-time deps | When you need it |
|--|--|--|--|
| Env-based key rotation | `EnvKeyProvider` | none | Day 1 |
| File-backed key store | `FileKeyProvider` | none | Vault Agent / Ansible-managed secrets |
| HSM key storage | `HSMKeyProvider` | `python-pkcs11` | RBI rule for tier-1 banks |
| LDAP / AD login | `LDAPAuthProvider` | `ldap3` | Enterprise SSO |
| SAML 2.0 | `SAMLAuthProvider` | your verifier | Federated identity |
| Upstream JWT exchange | `SSOTokenExchanger` | your verifier | Cognito / Okta / Auth0 |
| Declarative roles | `RBACPolicyEngine` | none | Separation of access |
| Dual-control writes | `SeparationOfDutiesPolicy` | none | Wire transfers, treasury ops |
| Append-only SQLite audit | `SQLiteGrimoirePersistence` | none (stdlib `sqlite3`) | DBA team comfort, indexed lookup |
| AES-GCM at rest | `EncryptedGrimoireJSONL` | `cryptography` | DPDPA / ISO 27001 |
| X.509-signed pages | `X509Signer` | `cryptography` | Court-grade non-repudiation |
| Mutual TLS | `MTLSConfig` + `install_mtls_on` | none | Zero-trust network |
| OTLP trace export | `OTLPSpanExporter` | none (`urllib`) | Tempo / Jaeger / DataDog |
| Kafka audit stream | `KafkaAuditStreamer` | `kafka-python` (optional) | SIEM in Kafka |
| Pushgateway metrics | `PrometheusPushGateway` | none | Short batch jobs |
| Peer-to-peer replication | `ClusterPeer` | none | 2- or 3-node active-active |
| Leader / follower coord | `HAGrimoireCoordinator` | none | Single-writer cluster |
| Bundled installer | `install_enterprise(app, ...)` | depends on parts | Compose everything in one call |

---

## 1. Keys and secrets

```python
from shabd_enterprise import EnvKeyProvider, FileKeyProvider, HSMKeyProvider

# Day-1 production
kp = EnvKeyProvider()                     # reads SHABD_SECRET (+ _OLD)

# Centralised secret manager (Vault Agent, Ansible, etc.)
kp = FileKeyProvider("/var/lib/shabd/keys")

# Hardware-backed (RBI tier-1 bank gate)
kp = HSMKeyProvider(slot_id=0, label="shabd-prod", pin="...")
```

Wire it once via `install_enterprise(app, key_provider=kp)`. The
`TokenManager` is swapped under the hood so the current key signs new
tokens and any previous keys still verify — that is your **zero-
downtime rotation** procedure.

---

## 2. Authentication bridges

SHABD speaks HMAC tokens natively. Most banks want LDAP / AD / SAML /
upstream JWT in front of that.

```python
from shabd_enterprise import LDAPAuthProvider, SSOTokenExchanger

ldap = LDAPAuthProvider(
    host="ldaps://ad.bank.internal:636",
    base_dn="dc=bank,dc=internal",
)
result = ldap.authenticate("amit.clerk", "******")
if result.ok:
    token = app.issue_token(result.subject, scopes=result.scopes)
```

For OIDC / upstream-JWT brokers, hand `SSOTokenExchanger` a callable
that verifies the upstream token (PyJWT, your IdP SDK, whatever) and
SHABD will return a local token in exchange.

---

## 3. Authorisation: RBAC and dual control

```python
from shabd_enterprise import RBACPolicyEngine, SeparationOfDutiesPolicy

rbac = RBACPolicyEngine()
rbac.add_rule("clerk",          allow=["open_account"])
rbac.add_rule("compliance",     allow_prefixes=["screen_*", "generate_*"])
rbac.add_rule("dealer",         allow=["book_repo", "book_ndsom_trade"])
rbac.add_rule("branch-manager", allow=["wire_transfer"])
rbac.add_rule("admin",          allow_prefixes=["*"])

SeparationOfDutiesPolicy(app, sensitive_spells=["wire_transfer"])

install_enterprise(app, rbac=rbac)
```

`scopes=[...]` on a spell is still enforced (built-in authz). RBAC
adds the **role-to-spell allow / deny matrix** on top, including
prefix-based rules (`finance_*`) and attribute requirements.

`SeparationOfDutiesPolicy` enforces dual control: a sensitive spell
must be called with a second token (`approver_token=`) whose subject
differs from the caller — exactly the kind of thing RBI / SEBI
auditors look for on wire transfers.

---

## 4. Audit chain: SQLite, encryption, X.509

The default `Grimoire` lives in memory. For production:

```python
from shabd_enterprise import SQLiteGrimoirePersistence, EncryptedGrimoireJSONL, X509Signer

# WAL-mode, append-only SQLite — your DBA team already backs this up.
store = SQLiteGrimoirePersistence("/var/lib/shabd/audit.db")

# Or encrypt-at-rest (needs cryptography lib).
enc = EncryptedGrimoireJSONL("/var/lib/shabd/audit.enc.jsonl",
                              key=os.urandom(32))

# Optional X.509 page signing for court use.
signer = X509Signer(open("priv.pem", "rb").read(),
                    open("cert.pem", "rb").read())
```

Install:

```python
install_enterprise(app, sqlite_store=store, x509_signer=signer)
```

Indexed lookup (compliance team will ask):

```python
pages = store.find_by_trace("95f8bccd…")
```

---

## 5. Transport hardening: mTLS

```python
from shabd_enterprise import MTLSConfig, install_mtls_on

cfg = MTLSConfig(
    server_cert="/etc/shabd/server.crt",
    server_key="/etc/shabd/server.key",
    client_ca="/etc/shabd/clients.ca.crt",
    allowed_client_cns=("trader-bot.bank.internal",
                        "ml-platform.bank.internal"),
)
install_mtls_on(app, cfg)
app.serve(host="0.0.0.0", port=8443,
          tls_cert=cfg.server_cert, tls_key=cfg.server_key)
```

The SSL context already verifies *that* a client cert is valid against
the configured CA. The hook adds CN-allowlisting on top.

---

## 6. Observability: OTLP, Kafka, Pushgateway

```python
from shabd_enterprise import OTLPSpanExporter, KafkaAuditStreamer, PrometheusPushGateway

otlp  = OTLPSpanExporter("http://otelcol.bank.internal:4318",
                         service_name="trading-ai")
kafka = KafkaAuditStreamer(bootstrap="kafka.bank.internal:9092",
                           topic="shabd.audit")
pg    = PrometheusPushGateway("http://pushgw.bank.internal:9091",
                              job_name="shabd")

install_enterprise(app, otlp=otlp, kafka=kafka)
# pg.push(app) from a cron at the end of a batch job.
```

Use OTLP for distributed traces, Kafka for SIEM ingestion, and the
Pushgateway for short-lived batch jobs that finish faster than
Prometheus can scrape them.

---

## 7. High availability

Two patterns:

```python
from shabd_enterprise import ClusterPeer, HAGrimoireCoordinator

# Pattern 1: peer-to-peer push (every node writes locally and
# forwards each Grimoire page to its peers).
cluster = ClusterPeer(
    peers=["https://shabd-2.bank.internal:8443"],
    hmac_secret=b"shared-cluster-key-...",
)

# Pattern 2: single writer with a heartbeat.
coord = HAGrimoireCoordinator(app, cluster)
install_enterprise(app, cluster=cluster, coordinator=coord)
```

`ClusterPeer` is enough for two- or three-node deployments. For
larger clusters you probably want a real consensus library (etcd /
Consul) backing the coordinator — the stub here is a placeholder, not
Raft.

---

## 8. Composing everything: `install_enterprise`

```python
install_enterprise(
    app,
    key_provider   = EnvKeyProvider(),
    rbac           = rbac,
    sod            = SeparationOfDutiesPolicy(app, sensitive_spells=["wire_transfer"]),
    sqlite_store   = SQLiteGrimoirePersistence("/var/lib/shabd/audit.db"),
    x509_signer    = signer,
    otlp           = otlp,
    kafka          = kafka,
    cluster        = cluster,
    coordinator    = coord,
    mtls           = mtls,
)
```

Skip any argument to skip that layer. The internal order is:

1. Key provider (TokenManager swap)
2. SQLite Grimoire (so loaded pages are in memory before signing wraps it)
3. X.509 signer
4. Cluster + coordinator (writer wrapping)
5. Kafka audit stream
6. RBAC (last in the policy chain so it sees the final spell list)
7. OTLP exporter
8. mTLS CN allow-list hook

---

## Reading list

* [grimoire.md](grimoire.md) — the audit chain in detail
* [security.md](security.md) — secrets, tokens, scopes, threat model
* [production-deployment.md](production-deployment.md) — Docker, K8s,
  systemd
* [runbook.md](runbook.md) — operator playbook
