"""
shabd_packs — revenue-generating vertical packs for SHABD.

Each pack is a small, focused module that bolts onto an existing SHABD
app and immediately provides spells worth selling:

    from shabd import SHABD
    from shabd_packs import sanctions, regtech, pretrade, ccil

    app = SHABD("acme-bank", secret=os.environ["SHABD_SECRET"])

    sanctions.install(app, lists=("OFAC", "UN", "RBI"))
    regtech.install(app, regulator="RBI")
    pretrade.install(app, limits={"NIFTY": 10_000_000})
    ccil.install(app, member_id="MBR-0042")

That's it — you now have ready-to-call spells with the right audit
metadata, semantic types, and rate limits already baked in.

Each pack is designed so a sales pitch can point at "this single
import is your RBI-digital-lending-guidelines compliance layer".
"""
from . import (
    algo_lifecycle,
    aml,
    ccil,
    dpdpa,
    pretrade,
    reconciliation,
    reconstruction,
    regtech,
    sanctions,
    surveillance,
)

__all__ = [
    "sanctions", "regtech", "pretrade", "ccil",
    "aml", "dpdpa", "surveillance", "reconciliation", "algo_lifecycle",
    "reconstruction",
]
