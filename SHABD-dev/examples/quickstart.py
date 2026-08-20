"""
The smallest possible SHABD server.

    python examples/quickstart.py
    # then open http://localhost:8765/dashboard

Or, to expose this to Claude Desktop over MCP:

    python examples/quickstart.py --mcp
"""
# Allow running straight from the repo (no install needed).
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shabd import SHABD

app = SHABD("quickstart", secret="change-me-please-32-bytes-or-more", require_auth=False)


@app.spell
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


@app.spell
def greet(name: str = "World") -> str:
    """Greet someone by name."""
    return f"Hello, {name}!"


if __name__ == "__main__":
    if "--mcp" in sys.argv:
        app.mcp_stdio()
    else:
        app.serve(port=8765)
