"""
shabd_core — the shared SHABD library, packaged for the microservices.

The original modules (shabd.py, shabd_notary.py, shabd_users.py, shabd_agent.py,
shabd_orchestrator.py) import each other as flat top-level modules
(e.g. `import shabd_agent`, `from shabd import SHABD`). To keep those imports
working unchanged, we put this package's own directory on sys.path. Services can
then simply do:

    from shabd_core import SHABD, load_secret
    # or the original style:  from shabd import SHABD
"""
from __future__ import annotations

import os as _os
import sys as _sys

_HERE = _os.path.dirname(__file__)
if _HERE not in _sys.path:
    _sys.path.insert(0, _HERE)

# Re-export the common surface so services get one clean import.
from shabd import SHABD  # noqa: E402
from stable_secret import load_secret, secret_file_path  # noqa: E402

__all__ = ["SHABD", "load_secret", "secret_file_path"]
