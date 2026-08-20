# MCP Integration

The Model Context Protocol (MCP) is the way modern AI clients —
Claude Desktop, IDE plugins, agent runtimes — discover and call tools.
SHABD speaks it natively.

## With Claude Desktop

In your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "shabd": {
      "command": "python",
      "args": ["/full/path/to/your_server.py", "--mcp"]
    }
  }
}
```

In your server file:

```python
import sys
from shabd import SHABD

app = SHABD("my-tools", secret="…")

@app.spell
def add(a: int, b: int) -> int:
    return a + b

if __name__ == "__main__":
    if "--mcp" in sys.argv:
        app.mcp_stdio()
    else:
        app.serve(port=8765)
```

Claude Desktop will discover your spells, resources, and prompts. Restart
Claude after editing `claude_desktop_config.json`.

## With Ollama (or any OpenAI-compatible endpoint)

```python
import json, urllib.request

# 1. Pull the tool definitions from a running SHABD HTTP server.
manifest = json.loads(urllib.request.urlopen("http://localhost:8765/manifest").read())

tools = [
    {"type": "function", "function": {
        "name": s["name"],
        "description": s["description"],
        "parameters": s["input_schema"],
    }}
    for s in manifest["spells"]
]

# 2. Pass `tools=[...]` to the model.
# 3. When the model returns a tool_call, POST to /spells/<name>.
```

A complete runnable example is in
[`examples/ollama_demo.py`](../examples/ollama_demo.py).

## Cross-language

Because SHABD also exposes a clean HTTP/JSON surface plus an OpenAPI spec
at `/openapi.json`, **any** language can call SHABD spells — Node, Go,
.NET, Java, Rust — using its native HTTP client. You don't need to write
an MCP client to talk to SHABD; you only need an HTTP client.

## Proxying other MCP servers

`MCPClient` can connect to an external MCP server and register its tools
locally on your SHABD app:

```python
from shabd import SHABD, MCPClient

app = SHABD("hub")

client = MCPClient("external", command=["node", "their-mcp-server.js"])
client.connect()
client.register_on(app)   # all external tools now show up as local spells
```

The proxied tools inherit SHABD's auth, rate limiting, caching, and audit
chain — useful for putting a controlled, audited façade in front of an
untrusted MCP server.

## What the MCP surface includes

| MCP concept | SHABD decorator |
|---|---|
| `tools` | `@app.spell` |
| `resources` | `@app.resource("/uri/{var}")` |
| `prompts` | `@app.prompt("name")` |
| `images` | return `SpellImage(...)` |
| `files`  | return `SpellFile(...)` |
