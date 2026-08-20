"""
shabd_client.py — Thin client for SHABD-backed agents.

Single file, zero dependencies. Drop it next to `shabd.py` (or `pip install`
it) and you have a complete agent SDK that talks to any SHABD server.

    from shabd_client import SHABDClient

    client = SHABDClient("http://localhost:8765", token=TOKEN)

    # 1) Direct casting
    client.cast("add", {"a": 2, "b": 3})        # -> 5

    # 2) For an LLM (OpenAI / Anthropic / Ollama tools format)
    tools = client.tools_for_openai()
    # pass tools=tools to chat.completions.create(...)

    # 3) Banking-grade safe retry
    client.cast("transfer", body, idempotency_key="uuid-12345")

    # 4) Audit verification
    client.grimoire_verify()    # -> {"ok": True, "pages": ..., "head": ...}

What this client does for you, automatically:
  * Bearer auth
  * W3C `traceparent` propagation (in + out) for distributed tracing
  * `Idempotency-Key` header for safe retries on writes
  * Network retries with exponential backoff for transient errors
  * Surfaces SHABD's structured error envelope (`hint`, `did_you_mean`,
    `example`) as a Python exception with those fields preserved
"""
from __future__ import annotations

import json
import secrets
import time
import typing as t
import urllib.error
import urllib.parse
import urllib.request

__all__ = ["SHABDClient", "SHABDClientError"]


class SHABDClientError(Exception):
    """Raised when a SHABD call fails. Carries the structured error envelope."""

    def __init__(self, code: str, message: str, *,
                 hint: str | None = None,
                 did_you_mean: list | None = None,
                 example: t.Any = None,
                 status: int = 500,
                 trace_id: str | None = None):
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.hint = hint
        self.did_you_mean = did_you_mean
        self.example = example
        self.status = status
        self.trace_id = trace_id


def _new_trace_id() -> str:
    return secrets.token_hex(16)


def _new_span_id() -> str:
    return secrets.token_hex(8)


class SHABDClient:
    """Synchronous SHABD client (stdlib-only). For async, run in a thread pool."""

    def __init__(self, base_url: str, *,
                 token: str | None = None,
                 timeout: float = 30.0,
                 retries: int = 2,
                 user_agent: str = "shabd-client/2.2"):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.retries = max(0, int(retries))
        self.user_agent = user_agent

    # ------------------------------------------------------------------ http

    def _request(self, method: str, path: str, *,
                 body: t.Any = None,
                 idempotency_key: str | None = None,
                 traceparent: str | None = None,
                 expect_json: bool = True) -> tuple[int, t.Any, dict]:
        url = f"{self.base_url}{path}"
        headers = {
            "user-agent": self.user_agent,
            "accept": "application/json",
        }
        if self.token:
            headers["authorization"] = f"Bearer {self.token}"
        if traceparent is None:
            traceparent = f"00-{_new_trace_id()}-{_new_span_id()}-01"
        headers["traceparent"] = traceparent
        if idempotency_key:
            headers["idempotency-key"] = idempotency_key

        data = None
        if body is not None:
            headers["content-type"] = "application/json"
            data = json.dumps(body, default=str).encode()

        req = urllib.request.Request(url, data=data, method=method,
                                     headers=headers)

        last_exc: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                    status = resp.getcode()
                    out = json.loads(raw) if (raw and expect_json) else raw
                    return status, out, dict(resp.headers)
            except urllib.error.HTTPError as e:
                raw = e.read() or b""
                try:
                    payload = json.loads(raw) if raw else {}
                except json.JSONDecodeError:
                    payload = {"error": {"code": "non_json", "message": raw.decode("utf-8", "replace")}}
                # 5xx and 429 retry; 4xx don't
                if e.code in (429, 502, 503, 504) and attempt < self.retries:
                    time.sleep(0.2 * (2 ** attempt))
                    continue
                return e.code, payload, dict(e.headers or {})
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last_exc = e
                if attempt < self.retries:
                    time.sleep(0.2 * (2 ** attempt))
                    continue
                raise SHABDClientError(
                    "network_error", str(e), status=0,
                ) from e
        # Should not reach here.
        raise SHABDClientError("network_error", str(last_exc), status=0)

    # ---------------------------------------------------------- public API

    def health(self) -> dict:
        _, body, _ = self._request("GET", "/healthz")
        return body

    def ready(self) -> bool:
        status, _, _ = self._request("GET", "/readyz")
        return status == 200

    def manifest(self) -> dict:
        _, body, _ = self._request("GET", "/manifest")
        return body

    def cast(self, spell: str, args: dict | None = None, *,
             idempotency_key: str | None = None,
             traceparent: str | None = None) -> t.Any:
        """Invoke a spell. Returns the raw result or raises SHABDClientError."""
        status, body, _ = self._request(
            "POST", f"/spells/{spell}",
            body=args or {},
            idempotency_key=idempotency_key,
            traceparent=traceparent,
        )
        if status >= 400 or (isinstance(body, dict) and body.get("error")):
            err = (body or {}).get("error", {}) if isinstance(body, dict) else {}
            raise SHABDClientError(
                err.get("code", "unknown"),
                err.get("message", "spell call failed"),
                hint=err.get("hint"),
                did_you_mean=err.get("did_you_mean"),
                example=err.get("example"),
                status=status,
                trace_id=(body or {}).get("trace_id") if isinstance(body, dict) else None,
            )
        return body.get("result") if isinstance(body, dict) else body

    def grimoire_verify(self) -> dict:
        _, body, _ = self._request("GET", "/grimoire/verify")
        return body

    def grimoire_head(self) -> dict:
        _, body, _ = self._request("GET", "/grimoire/head")
        return body

    # ---------------------------- LLM tool-format helpers (OpenAI/Anthropic)

    def tools_for_openai(self) -> list[dict]:
        """Manifest in OpenAI / Ollama chat-completions `tools=[...]` format."""
        m = self.manifest()
        out = []
        for s in m.get("spells", []):
            out.append({
                "type": "function",
                "function": {
                    "name": s["name"],
                    "description": s.get("description", ""),
                    "parameters": s.get("input_schema", {}),
                },
            })
        return out

    def tools_for_anthropic(self) -> list[dict]:
        """Manifest in Anthropic Messages API `tools=[...]` format."""
        m = self.manifest()
        return [{
            "name": s["name"],
            "description": s.get("description", ""),
            "input_schema": s.get("input_schema", {}),
        } for s in m.get("spells", [])]

    # ---------------------- Convenience: dispatch a model's tool_calls

    def dispatch_openai_tool_calls(self, tool_calls: t.Iterable[dict]
                                   ) -> list[dict]:
        """
        Call each OpenAI-style tool_call and return the messages to feed
        back into the model.

            messages.extend(client.dispatch_openai_tool_calls(resp.tool_calls))
        """
        out = []
        for call in tool_calls:
            name = call["function"]["name"]
            args = call["function"].get("arguments", "{}")
            if isinstance(args, str):
                args = json.loads(args or "{}")
            try:
                result = self.cast(name, args)
                out.append({
                    "role": "tool",
                    "tool_call_id": call.get("id"),
                    "name": name,
                    "content": json.dumps(result, default=str),
                })
            except SHABDClientError as e:
                out.append({
                    "role": "tool",
                    "tool_call_id": call.get("id"),
                    "name": name,
                    "content": json.dumps({
                        "error": e.code,
                        "message": e.message,
                        "hint": e.hint,
                        "did_you_mean": e.did_you_mean,
                        "example": e.example,
                    }, default=str),
                })
        return out
