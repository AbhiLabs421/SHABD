"""
shabd_stdio/server.py — the MCP stdio server, fully SEPARATE from the web UI.

The old setup made the stdio server open only "after logging into shabd_ui".
That coupling is gone: this is its own process that speaks the Model Context
Protocol over stdin/stdout, for Claude Desktop and other MCP clients. It does
NOT need the web UI or a login — it shares only the same spells and the same
stable secret (so its Grimoire chain verifies alongside the services).

Run it standalone:

    python shabd_stdio/server.py

Wire it into Claude Desktop (claude_desktop_config.json):

    {
      "mcpServers": {
        "shabd": {
          "command": "python",
          "args": ["D:/bhaiya/SHABD-dev/shabd_stdio/server.py"]
        }
      }
    }
"""
from __future__ import annotations

import pathlib
import sys

# make the core library + shared demo spells importable
_ROOT = pathlib.Path(__file__).resolve().parent.parent
_CORE = _ROOT / "backend" / "shabd_core"
for _p in (str(_CORE),):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from shabd import SHABD  # noqa: E402
from stable_secret import load_secret  # noqa: E402
import demo_spells  # noqa: E402

app = SHABD(
    "shabd-stdio",
    secret=load_secret(),
    require_auth=False,
    grimoire_log_path=str(_ROOT / "shared" / "data" / "stdio-audit.jsonl"),
)
demo_spells.register(app)

if __name__ == "__main__":
    # Blocks, talking MCP over stdin/stdout until the client disconnects.
    app.mcp_stdio()
