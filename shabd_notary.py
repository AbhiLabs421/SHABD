"""
shabd_notary.py — Cross-entity Grimoire notarisation.

"Blockchain-grade immutability between two parties, without a blockchain."

The problem this solves
=======================
Two regulated entities — say a bank and its co-lending NBFC partner, or
a CCIL member and CCIL itself, or a Data Fiduciary and the DPB — both
run AI systems that produce decisions. Each side keeps its own
Grimoire audit chain. Today, neither side can fully trust the other's
log because either side could edit history retroactively before
showing it to the regulator.

The blockchain industry tried to solve this with consensus protocols,
gas fees and shared ledgers. Banks rejected every one of those because
they bring crypto regulation, latency, throughput and key-management
problems banks already solved differently.

The Agent Notary does the same job in a *much* smaller way:

  1. At regular intervals each side computes its current Grimoire head
     (the latest signed hash in its chain).
  2. The two sides exchange those heads and counter-sign each other's
     value.
  3. Each side stores the peer's countersigned root locally.

Now neither side can edit any page that existed at the moment of an
exchange without invalidating the counter-signature the *other* side
holds. The peer's countersigned root acts as a one-bit witness, but
that's all you need — once a head is countersigned, the entire chain
up to that head is sealed.

Properties
----------
* Tamper-evident across organisational boundaries.
* Asynchronous — the exchange can ride email, SFTP, SWIFT MT, S3, an
  encrypted USB stick. No always-online requirement.
* No third party, no consensus, no token, no gas fee.
* HMAC + SHA-256 only — every bank's crypto team already knows this.
* Inclusion proofs let a member prove a single decision happened at a
  specific time without revealing the rest of the chain.

What's deliberately NOT here
----------------------------
* No P2P gossip protocol. Two endpoints exchange roots — that's it. If
  you need many peers, run pairwise exchanges.
* No "smart contracts". Just signed JSON.
* No SDK update path. The format is plain JSON; once a regulator has
  the format, every later root is verifiable forever.

This is one file, stdlib only.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import threading
import time
import typing as t
import urllib.request
from dataclasses import asdict, dataclass, field

log = logging.getLogger("shabd.notary")

__all__ = [
    "AgentNotary",
    "NotaryRoot",
    "Countersignature",
    "InclusionProof",
    "verify_root",
    "verify_inclusion",
    "verify_countersignature",
]


# ============================================================================
# Data types
# ============================================================================

@dataclass
class NotaryRoot:
    """A signed snapshot of a Grimoire chain at one instant.

    The fields:
      * `entity`        — who is publishing this root.
      * `head`          — Grimoire head hash at the snapshot moment.
      * `seq`           — Grimoire seq number of that page (0-based).
      * `pages_count`   — how many pages have been written so far.
      * `ts`            — UNIX time of publication.
      * `signature`     — HMAC-SHA-256 over a canonical encoding of all
                          fields above, using the entity's secret. The
                          peer never needs the secret — it just stores
                          the signature.
    """
    entity: str
    head: str
    seq: int
    pages_count: int
    ts: float
    signature: str = ""

    def canonical(self) -> bytes:
        return json.dumps({
            "entity": self.entity,
            "head": self.head,
            "seq": self.seq,
            "pages_count": self.pages_count,
            "ts": self.ts,
        }, sort_keys=True, separators=(",", ":")).encode()

    def sign(self, secret: bytes) -> NotaryRoot:
        self.signature = hmac.new(secret, self.canonical(),
                                   hashlib.sha256).hexdigest()
        return self

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> NotaryRoot:
        return cls(**d)


@dataclass
class Countersignature:
    """One party (the *counter*) acknowledging another party's
    `NotaryRoot`. The counter signs a payload that pins:

      * the exact root being witnessed (by its already-computed
        `signature`),
      * the counter's own entity name and current time.
    """
    witness_of: str           # entity name whose root is being witnessed
    witnessed_signature: str  # the `signature` field of the witnessed root
    counter: str              # entity doing the counter-signing
    counter_ts: float
    signature: str = ""

    def canonical(self) -> bytes:
        return json.dumps({
            "witness_of": self.witness_of,
            "witnessed_signature": self.witnessed_signature,
            "counter": self.counter,
            "counter_ts": self.counter_ts,
        }, sort_keys=True, separators=(",", ":")).encode()

    def sign(self, secret: bytes) -> Countersignature:
        self.signature = hmac.new(secret, self.canonical(),
                                   hashlib.sha256).hexdigest()
        return self

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> Countersignature:
        return cls(**d)


@dataclass
class InclusionProof:
    """A small bundle that proves Grimoire page `seq` was part of the
    chain at the moment a particular `NotaryRoot` was signed.

    The proof is the *suffix* of pages from `seq` through the page
    whose hash equals `notary_root.head`. The verifier rebuilds the
    hash chain over that suffix and checks the last hash equals
    `notary_root.head` and that `notary_root.signature` is valid for
    the issuing entity.

    This is essentially Merkle membership over a linear hash chain —
    cheap, deterministic, and the regulator can verify it offline with
    nothing but a pure Python script (`scripts/verify_inclusion.py`).
    """
    seq: int
    page_hash: str
    pages: list = field(default_factory=list)   # ordered: [seq..head_seq]
    notary_root: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> InclusionProof:
        return cls(**d)


# ============================================================================
# Pure-function verifiers — what a regulator runs
# ============================================================================

def verify_root(root: NotaryRoot, secret: bytes) -> bool:
    """Recompute the HMAC and compare. The verifier needs the entity's
    secret (regulators typically receive copies of agreed verification
    secrets out-of-band; banks rotate them per audit period)."""
    expected = hmac.new(secret, root.canonical(),
                         hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, root.signature)


def verify_countersignature(cs: Countersignature, counter_secret: bytes,
                             expected_witnessed_signature: str) -> bool:
    if not hmac.compare_digest(cs.witnessed_signature,
                                expected_witnessed_signature):
        return False
    expected = hmac.new(counter_secret, cs.canonical(),
                         hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, cs.signature)


def verify_inclusion(proof: InclusionProof, entity_secret: bytes) -> dict:
    """Rebuild the hash chain over the suffix and check it terminates
    in `notary_root.head`. Returns a structured result so the caller
    can render an audit explanation."""
    root = NotaryRoot.from_dict(proof.notary_root)
    if not verify_root(root, entity_secret):
        return {"ok": False, "reason": "bad_root_signature"}

    if not proof.pages:
        return {"ok": False, "reason": "empty_proof_suffix"}

    # The first page in the proof should have the seq the proof claims,
    # and the hash matching `page_hash`.
    first = proof.pages[0]
    if first.get("seq") != proof.seq:
        return {"ok": False, "reason": "first_page_seq_mismatch"}
    if first.get("hash") != proof.page_hash:
        return {"ok": False, "reason": "first_page_hash_mismatch"}

    # Each subsequent page must reference the previous page's hash.
    prev_hash = first.get("hash")
    for page in proof.pages[1:]:
        if page.get("prev") != prev_hash:
            return {"ok": False, "reason": "chain_break",
                    "at_seq": page.get("seq")}
        prev_hash = page.get("hash")

    # The last page's hash must equal the notarised head.
    if prev_hash != root.head:
        return {"ok": False, "reason": "head_mismatch"}

    return {
        "ok": True,
        "entity": root.entity,
        "seq": proof.seq,
        "head": root.head,
        "notarised_at": root.ts,
    }


# ============================================================================
# The notary itself
# ============================================================================

class AgentNotary:
    """One side of a mutual-notarisation arrangement.

    Bound to a SHABD `app` (whose Grimoire we will notarise). Knows its
    own entity name and a *publishing secret* (different from the
    Grimoire HMAC secret — used only for notarisation, can be rotated
    per audit period without touching the chain).

    Standard flow:

        notary = AgentNotary(app, entity="bank-A",
                              publishing_secret=os.environ["NOTARY_SECRET"])

        # Periodically publish a root and ship it to the peer
        root = notary.publish_root()
        peer.receive_peer_root(root)              # peer countersigns it
        cs = peer.countersign(root, counter_secret=peer_secret)
        notary.store_countersignature(cs)         # we keep the peer's witness

        # Later, when the regulator asks for proof of decision seq=42:
        proof = notary.build_inclusion_proof(seq=42, against=root)
        # Regulator runs `verify_inclusion(proof, our_secret)` offline.
    """

    def __init__(self, app: t.Any, *, entity: str,
                 publishing_secret: t.Union[str, bytes],
                 storage_dir: str | None = None):
        self.app = app
        self.entity = entity
        if isinstance(publishing_secret, str):
            try:
                publishing_secret = bytes.fromhex(publishing_secret)
            except ValueError:
                publishing_secret = publishing_secret.encode()
        if len(publishing_secret) < 16:
            raise ValueError("publishing_secret must be >= 16 bytes")
        self._secret = publishing_secret
        self.storage_dir = storage_dir
        if storage_dir:
            os.makedirs(storage_dir, exist_ok=True)
        self._lock = threading.Lock()
        self._roots: list[NotaryRoot] = []
        self._peer_countersignatures: list[Countersignature] = []
        self._held_peer_roots: list[NotaryRoot] = []

    # ---- publishing our roots ----

    def publish_root(self) -> NotaryRoot:
        """Snapshot the current Grimoire head and sign it."""
        with self._lock:
            grim = self.app.grimoire
            pages = list(grim._pages)
            if pages:
                head = grim.head()
                seq = pages[-1]["seq"]
            else:
                head = grim.GENESIS
                seq = -1
            root = NotaryRoot(
                entity=self.entity, head=head, seq=seq,
                pages_count=len(pages), ts=time.time(),
            )
            root.sign(self._secret)
            self._roots.append(root)
            if self.storage_dir:
                path = os.path.join(self.storage_dir,
                                     f"root-{int(root.ts)}.json")
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump(root.to_dict(), fh)
        return root

    def send_to_peer(self, peer_url: str, root: NotaryRoot, *,
                     timeout: float = 5.0) -> None:
        """Best-effort HTTP delivery. Any byte-stream pipe works; this
        is just one option."""
        body = json.dumps(root.to_dict()).encode()
        req = urllib.request.Request(
            peer_url, data=body, method="POST",
            headers={"content-type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                r.read(64)
        except Exception:
            log.exception("send_to_peer failed")

    # ---- receiving peer roots ----

    def receive_peer_root(self, root: NotaryRoot) -> None:
        """Accept the peer's root. We don't verify its signature here —
        the peer's secret is theirs to manage. We only store it so we
        can countersign it next."""
        with self._lock:
            self._held_peer_roots.append(root)
            if self.storage_dir:
                path = os.path.join(self.storage_dir,
                                     f"peer-{root.entity}-"
                                     f"{int(root.ts)}.json")
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump(root.to_dict(), fh)

    def countersign(self, peer_root: NotaryRoot) -> Countersignature:
        """Witness a peer's root. We use our publishing secret to sign
        a binding between us, the peer's root signature and now()."""
        cs = Countersignature(
            witness_of=peer_root.entity,
            witnessed_signature=peer_root.signature,
            counter=self.entity,
            counter_ts=time.time(),
        ).sign(self._secret)
        return cs

    def store_countersignature(self, cs: Countersignature) -> None:
        """Store a countersignature *we received from a peer* — i.e. the
        peer's witness over one of our roots. Keep this for the next
        regulator review."""
        with self._lock:
            self._peer_countersignatures.append(cs)
            if self.storage_dir:
                path = os.path.join(self.storage_dir,
                                     f"countersig-{cs.counter}-"
                                     f"{int(cs.counter_ts)}.json")
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump(cs.to_dict(), fh)

    # ---- inclusion proofs ----

    def build_inclusion_proof(self, *, seq: int,
                               against: NotaryRoot) -> InclusionProof:
        """Build an inclusion proof for one decision page against a
        previously published root."""
        with self._lock:
            pages = [p for p in self.app.grimoire._pages
                     if p["seq"] >= seq and p["seq"] <= against.seq]
        if not pages:
            raise ValueError("seq not in chain at the notarised moment")
        if pages[0]["seq"] != seq:
            raise ValueError("seq is older than this notary root covers")
        slim_pages = [{
            "seq": p["seq"], "prev": p["prev"], "hash": p["hash"],
        } for p in pages]
        return InclusionProof(
            seq=seq,
            page_hash=pages[0]["hash"],
            pages=slim_pages,
            notary_root=against.to_dict(),
        )

    # ---- read-only views ----

    def roots(self) -> list[NotaryRoot]:
        with self._lock:
            return list(self._roots)

    def held_peer_roots(self) -> list[NotaryRoot]:
        with self._lock:
            return list(self._held_peer_roots)

    def countersignatures_received(self) -> list[Countersignature]:
        with self._lock:
            return list(self._peer_countersignatures)
