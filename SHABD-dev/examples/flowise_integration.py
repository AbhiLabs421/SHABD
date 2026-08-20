"""
Flowise + SHABD integration — turn every SHABD spell into a Flowise
"Custom Tool" node that an LLM agent can call.

There are two ways to wire SHABD into Flowise:

(1) HTTP tool (simplest)
    In Flowise, create a "Custom Tool" with the URL
        http://your-shabd:8765/spells/<spell-name>
    Body: the JSON the spell expects (Flowise will substitute the LLM's
    args). Headers:
        Authorization: Bearer <SHABD token>
        Idempotency-Key: <uuid per logical action>
    The LLM agent then sees one tool per spell, with the right schema.

(2) OpenAPI import
    Flowise can import an OpenAPI spec. SHABD already serves
        GET /openapi.json
    so a single import call gets all your spells, their parameters, and
    their descriptions registered as tools. This is the fastest way to
    onboard a new agent.

This file does *not* require Flowise to be installed. It walks through
both flows using SHABDClient so you can paste the same shapes into a
Flowise "HTTP" or "OpenAPI" tool node.

    python examples/flowise_integration.py            # serve
    python examples/flowise_integration.py --client   # walkthrough
"""
from __future__ import annotations

import json
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shabd import SHABD

app = SHABD("flowise-demo", secret=os.environ.get("SHABD_SECRET", "x" * 32),
            require_auth=False)


@app.spell
def add(a: int, b: int) -> int:
    """Add two numbers. Use this when the user asks for a sum."""
    return a + b


@app.spell
def search_docs(query: str, limit: int = 10) -> dict:
    """Search a knowledge base. Returns dummy hits in this demo."""
    return {"hits": [{"title": f"Doc about {query}", "score": 0.9}],
            "query": query, "limit": limit}


@app.spell
def lookup_kyc(customer_id: str) -> dict:
    """Look up KYC status for a customer."""
    return {"customer_id": customer_id,
            "kyc_status": "verified",
            "level": "Full-KYC"}


def _client_demo() -> None:
    from shabd_client import SHABDClient

    base = "http://localhost:8765"
    c = SHABDClient(base)

    # ---- Approach 1: pull the manifest and convert to Flowise schema ----
    manifest = c.manifest()
    flowise_tools = []
    for s in manifest["spells"]:
        flowise_tools.append({
            "name": s["name"],
            "description": s["description"],
            "schema": s["input_schema"],
            "url": f"{base}/spells/{s['name']}",
            "method": "POST",
            "headers": {
                "Content-Type": "application/json",
                # Add an Authorization header if your SHABD requires auth.
            },
        })
    print("Paste each of these into a Flowise 'Custom Tool' node:\n")
    print(json.dumps(flowise_tools, indent=2, default=str))

    # ---- Approach 2: serve an OpenAPI spec, import it once into Flowise ----
    spec_url = f"{base}/openapi.json"
    print(f"\nOR import the OpenAPI spec in one shot: {spec_url}")

    # ---- Approach 3: call a spell with an idempotency key like Flowise will ----
    print("\nExample call shape (Idempotency-Key auto-managed by SHABDClient):")
    ide = f"flow-{uuid.uuid4().hex[:8]}"
    print("  args:", {"a": 7, "b": 5}, "idempotency_key:", ide)
    print("  result:", c.cast("add", {"a": 7, "b": 5},
                              idempotency_key=ide))


if __name__ == "__main__":
    if "--client" in sys.argv:
        _client_demo()
    else:
        print("Open another shell and run "
              "`python examples/flowise_integration.py --client`")
        app.serve(port=8765)
