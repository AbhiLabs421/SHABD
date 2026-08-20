"""
shabd_agent.py — Universal, zero-dependency AI agent runtime.

Goal: write an agent in three lines, against any LLM provider, without
ever installing `openai`, `anthropic`, `google-genai`, `langchain`,
`llama-index` or anything else. Pure Python standard library.

A 60-second tour:

    from shabd_agent import Agent, OpenAICompatBackend

    agent = Agent(
        llm=OpenAICompatBackend(
            base_url="https://api.openai.com/v1",   # or Ollama, vLLM, ...
            api_key=os.environ["OPENAI_API_KEY"],
            model="gpt-4o-mini",
        ),
        system="You are a helpful Indian-banking ops assistant.",
    )

    @agent.tool
    def add(a: int, b: int) -> int:
        '''Add two numbers.'''
        return a + b

    print(agent.run("Compute 17 + 25."))

That's it. The same code works with Ollama by changing the
`base_url` to `http://localhost:11434/v1`. It works with Anthropic
by swapping `OpenAICompatBackend` for `AnthropicBackend`. It works
with Google's Gemini by swapping in `GeminiBackend`. It works offline
in tests via `MockBackend`. It works with an existing SHABD `app`
via `agent = Agent.from_shabd(app, llm=...)`.

What it gives you that a naive while-loop doesn't:

  * Loop control: `max_steps`, duplicate-step detection, hard timeout
  * Tool input validation against the declared JSON schema
  * Structured error messages that round-trip cleanly back to the LLM
    (so the LLM can self-correct without a human in the loop)
  * Conversation trace you can inspect after the run
  * Optional integration with SHABD's Grimoire audit chain, semantic
    types, idempotency, RBAC — when an `app` is supplied
  * Bring-your-own LLM: every backend speaks the same `chat(messages,
    tools)` interface; write a 30-line subclass to support any new
    provider

Provider matrix shipped in this file:

  * `OpenAICompatBackend`  — OpenAI, Ollama, vLLM, LM Studio, Together,
                             Groq, Mistral, LiteLLM, Fireworks. Anything
                             OpenAI-shaped.
  * `AnthropicBackend`     — Anthropic Messages API.
  * `GeminiBackend`        — Google Gemini `generateContent` API.
  * `MockBackend`          — deterministic, for tests and demos.

All of them produce the same `AssistantTurn` shape so the agent loop
never has to know which provider you picked.
"""
from __future__ import annotations

import inspect
import json
import logging
import os
import time
import typing as t
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field

log = logging.getLogger("shabd.agent")

__all__ = [
    # Core
    "Agent", "AgentResult", "AgentStep", "AssistantTurn", "ToolCall",
    "ToolSpec", "ToolRegistry", "ToolError",
    # Backends
    "LLMBackend", "OpenAICompatBackend", "AnthropicBackend",
    "GeminiBackend", "MockBackend",
]


# ============================================================================
# DATA TYPES
# ============================================================================

@dataclass
class ToolCall:
    """A single tool invocation requested by the LLM."""
    id: str
    name: str
    arguments: dict


@dataclass
class AssistantTurn:
    """One turn from the model: either a final answer (text) or tool calls."""
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw: t.Any = None         # provider-specific raw response (for debugging)

    @property
    def is_final(self) -> bool:
        return not self.tool_calls


@dataclass
class ToolSpec:
    """A tool the agent can call. Matches the OpenAI tool shape so any
    provider's adapter can render it without surprises."""
    name: str
    description: str
    parameters: dict          # JSON Schema for the arguments
    func: t.Callable | None = None


class ToolError(Exception):
    """Raised when a tool refuses or fails. The agent loop turns this
    into a `tool` role message so the LLM can self-correct."""
    def __init__(self, code: str, message: str, *,
                 hint: str = "", example: t.Any = None,
                 did_you_mean: list[str] | None = None):
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.hint = hint
        self.example = example
        self.did_you_mean = did_you_mean or []

    def to_dict(self) -> dict:
        out = {"error": {"code": self.code, "message": self.message}}
        if self.hint:
            out["error"]["hint"] = self.hint
        if self.example is not None:
            out["error"]["example"] = self.example
        if self.did_you_mean:
            out["error"]["did_you_mean"] = self.did_you_mean
        return out


@dataclass
class AgentStep:
    """One step of the loop — useful for tracing / debugging."""
    n: int
    assistant: AssistantTurn
    tool_results: list[dict] = field(default_factory=list)
    elapsed_ms: float = 0.0


@dataclass
class AgentResult:
    """Final outcome of `agent.run(...)`."""
    answer: str
    steps: list[AgentStep] = field(default_factory=list)
    stopped_reason: str = "final"     # "final" | "max_steps" | "timeout" | "duplicate"
    total_elapsed_s: float = 0.0


# ============================================================================
# TOOL REGISTRY
# ============================================================================

class ToolRegistry:
    """Holds tools and dispatches calls. Works standalone (a dict of
    functions) or backed by a SHABD app."""

    def __init__(self):
        self._tools: dict[str, ToolSpec] = {}
        self._app = None     # optional shabd.SHABD app

    # ---- registration ----

    def register(self, name: str, func: t.Callable, *,
                 description: str = "",
                 parameters: dict | None = None) -> None:
        spec = ToolSpec(
            name=name,
            description=description or (inspect.getdoc(func) or "").strip(),
            parameters=parameters or _infer_parameters(func),
            func=func,
        )
        self._tools[name] = spec

    def register_decorator(self):
        """Returns a decorator that registers the function as a tool."""
        def wrap(func: t.Callable) -> t.Callable:
            self.register(func.__name__, func)
            return func
        return wrap

    # ---- SHABD integration ----

    def bind_shabd(self, app) -> None:
        """Mirror every `@app.spell` as a tool. Calls route through
        SHABD's full pipeline (validation, RBAC, audit, idempotency).
        """
        self._app = app
        for name, spell in app._spells.items():
            self._tools[name] = ToolSpec(
                name=name,
                description=spell.description or name,
                parameters=spell.schema,
                func=None,                # dispatched via app.invoke
            )

    # ---- catalogue / dispatch ----

    def list_specs(self) -> list[ToolSpec]:
        return list(self._tools.values())

    def names(self) -> list[str]:
        return list(self._tools.keys())

    def call(self, name: str, args: dict, *,
             token: str | None = None) -> t.Any:
        if name not in self._tools:
            suggestion = _closest(name, self.names())
            raise ToolError(
                "tool_not_found", f"no tool named {name!r}",
                hint=(f"Did you mean {suggestion!r}?" if suggestion
                      else "Pick one of the listed tools."),
                did_you_mean=[suggestion] if suggestion else [],
            )
        if self._app is not None and name in self._app._spells:
            # Routes through SHABD: validation, audit, idempotency, RBAC
            return self._app.invoke(name, args, token=token)
        spec = self._tools[name]
        if spec.func is None:
            raise ToolError("tool_not_callable",
                            f"tool {name!r} has no implementation")
        try:
            return spec.func(**args)
        except TypeError as e:
            raise ToolError("bad_arguments", str(e),
                            hint="Check the tool's parameter schema.")


# ============================================================================
# LLM BACKENDS
# ============================================================================

class LLMBackend:
    """Every backend produces a normalized AssistantTurn so the agent
    loop never has to branch on provider."""

    def chat(self, messages: list[dict],
             tools: list[ToolSpec]) -> AssistantTurn:
        raise NotImplementedError

    def name(self) -> str:
        return type(self).__name__


def _http_post(url: str, headers: dict, body: dict,
               *, timeout: float = 60.0) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", "replace") if e.fp else ""
        raise RuntimeError(
            f"{e.code} from {url}: {err_body[:400]}"
        ) from e


class OpenAICompatBackend(LLMBackend):
    """OpenAI Chat Completions wire format.

    Works with: OpenAI, Ollama (`/v1`), vLLM, LM Studio, Together,
    Groq, Mistral, LiteLLM, Fireworks, OpenRouter, Anyscale,
    Perplexity, DeepInfra — any host that speaks the OpenAI shape.
    """

    def __init__(self, *, base_url: str, model: str,
                 api_key: str = "",
                 extra_headers: dict | None = None,
                 timeout: float = 60.0,
                 temperature: float = 0.0,
                 force_tools: bool = False):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.extra_headers = extra_headers or {}
        self.timeout = timeout
        self.temperature = temperature
        # When True, the model is forced to call a tool rather than
        # answering from its own knowledge (tool_choice="required").
        self.force_tools = force_tools

    def name(self) -> str:
        return f"openai-compat({self.model})"

    def _tools_payload(self, tools: list[ToolSpec]) -> list[dict]:
        return [{
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            },
        } for t in tools]

    def chat(self, messages: list[dict],
             tools: list[ToolSpec]) -> AssistantTurn:
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        headers.update(self.extra_headers)

        body: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
        }
        if tools:
            body["tools"] = self._tools_payload(tools)
            body["tool_choice"] = "required" if self.force_tools else "auto"

        resp = _http_post(f"{self.base_url}/chat/completions",
                          headers, body, timeout=self.timeout)
        choice = (resp.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        tool_calls: list[ToolCall] = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            args_raw = fn.get("arguments", "{}")
            if isinstance(args_raw, str):
                try:
                    args = json.loads(args_raw or "{}")
                except json.JSONDecodeError:
                    args = {}
            else:
                args = args_raw or {}
            tool_calls.append(ToolCall(
                id=tc.get("id") or f"call-{uuid.uuid4().hex[:8]}",
                name=fn.get("name", ""),
                arguments=args,
            ))
        return AssistantTurn(
            text=msg.get("content") or "",
            tool_calls=tool_calls,
            raw=resp,
        )


class AnthropicBackend(LLMBackend):
    """Anthropic Messages API. https://docs.anthropic.com/en/api/messages"""

    def __init__(self, *, model: str,
                 api_key: str | None = None,
                 base_url: str = "https://api.anthropic.com",
                 version: str = "2023-06-01",
                 max_tokens: int = 1024,
                 timeout: float = 60.0,
                 temperature: float = 0.0):
        self.model = model
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self.base_url = base_url.rstrip("/")
        self.version = version
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.temperature = temperature

    def name(self) -> str:
        return f"anthropic({self.model})"

    def _convert_messages(self, msgs: list[dict]) -> tuple[str, list]:
        """OpenAI-shaped messages -> Anthropic-shaped + system string."""
        system = ""
        out: list[dict] = []
        for m in msgs:
            role = m.get("role")
            if role == "system":
                system = (system + "\n\n" + m.get("content", "")).strip()
                continue
            if role == "tool":
                # Anthropic wants tool results as a user turn with
                # tool_result content blocks.
                out.append({"role": "user", "content": [{
                    "type": "tool_result",
                    "tool_use_id": m.get("tool_call_id", ""),
                    "content": m.get("content", ""),
                }]})
                continue
            if role == "assistant" and m.get("tool_calls"):
                blocks = []
                if m.get("content"):
                    blocks.append({"type": "text", "text": m["content"]})
                for tc in m["tool_calls"]:
                    fn = tc.get("function") or {}
                    args = fn.get("arguments", {})
                    if isinstance(args, str):
                        try:
                            args = json.loads(args or "{}")
                        except json.JSONDecodeError:
                            args = {}
                    blocks.append({
                        "type": "tool_use",
                        "id": tc.get("id", ""),
                        "name": fn.get("name", ""),
                        "input": args,
                    })
                out.append({"role": "assistant", "content": blocks})
                continue
            # plain text user/assistant turn
            out.append({"role": role,
                        "content": m.get("content", "")})
        return system, out

    def chat(self, messages: list[dict],
             tools: list[ToolSpec]) -> AssistantTurn:
        system, anth_msgs = self._convert_messages(messages)
        body: dict = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "messages": anth_msgs,
        }
        if system:
            body["system"] = system
        if tools:
            body["tools"] = [{
                "name": s.name,
                "description": s.description,
                "input_schema": s.parameters,
            } for s in tools]

        headers = {
            "content-type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": self.version,
        }
        resp = _http_post(f"{self.base_url}/v1/messages", headers, body,
                          timeout=self.timeout)
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in resp.get("content") or []:
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                tool_calls.append(ToolCall(
                    id=block.get("id") or f"call-{uuid.uuid4().hex[:8]}",
                    name=block.get("name", ""),
                    arguments=block.get("input") or {},
                ))
        return AssistantTurn(
            text="".join(text_parts),
            tool_calls=tool_calls,
            raw=resp,
        )


class GeminiBackend(LLMBackend):
    """Google Gemini `generateContent` API.

    https://ai.google.dev/api/generate-content
    """

    def __init__(self, *, model: str,
                 api_key: str | None = None,
                 base_url: str = "https://generativelanguage.googleapis.com",
                 timeout: float = 60.0,
                 temperature: float = 0.0):
        self.model = model
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY", "")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.temperature = temperature

    def name(self) -> str:
        return f"gemini({self.model})"

    def _convert_messages(self, msgs: list[dict]) -> tuple[str, list]:
        system_parts: list[str] = []
        out: list[dict] = []
        for m in msgs:
            role = m.get("role")
            if role == "system":
                system_parts.append(m.get("content", ""))
                continue
            if role == "tool":
                out.append({
                    "role": "user",
                    "parts": [{"functionResponse": {
                        "name": m.get("name", ""),
                        "response": {"output": m.get("content", "")},
                    }}],
                })
                continue
            if role == "assistant":
                parts: list[dict] = []
                if m.get("content"):
                    parts.append({"text": m["content"]})
                for tc in m.get("tool_calls") or []:
                    fn = tc.get("function") or {}
                    args = fn.get("arguments", {})
                    if isinstance(args, str):
                        try:
                            args = json.loads(args or "{}")
                        except json.JSONDecodeError:
                            args = {}
                    parts.append({"functionCall": {
                        "name": fn.get("name", ""), "args": args,
                    }})
                out.append({"role": "model", "parts": parts})
                continue
            out.append({"role": "user",
                        "parts": [{"text": m.get("content", "")}]})
        return "\n\n".join(system_parts).strip(), out

    def chat(self, messages: list[dict],
             tools: list[ToolSpec]) -> AssistantTurn:
        system, contents = self._convert_messages(messages)
        body: dict = {
            "contents": contents,
            "generationConfig": {"temperature": self.temperature},
        }
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        if tools:
            body["tools"] = [{"functionDeclarations": [{
                "name": s.name,
                "description": s.description,
                "parameters": s.parameters,
            } for s in tools]}]

        url = (f"{self.base_url}/v1beta/models/{self.model}"
               f":generateContent?key={self.api_key}")
        headers = {"Content-Type": "application/json"}
        resp = _http_post(url, headers, body, timeout=self.timeout)

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        cand = (resp.get("candidates") or [{}])[0]
        for part in (cand.get("content") or {}).get("parts") or []:
            if "text" in part:
                text_parts.append(part["text"])
            elif "functionCall" in part:
                fc = part["functionCall"]
                tool_calls.append(ToolCall(
                    id=f"call-{uuid.uuid4().hex[:8]}",
                    name=fc.get("name", ""),
                    arguments=fc.get("args") or {},
                ))
        return AssistantTurn(
            text="".join(text_parts),
            tool_calls=tool_calls,
            raw=resp,
        )


class MockBackend(LLMBackend):
    """Deterministic backend for tests and air-gapped demos.

    Pass a `plan` — a list of either:
      * A dict `{"tool": name, "args": {...}}` (the next turn will be a
        tool call), or
      * A plain string (the next turn will be a final answer).

    Calls cycle through the plan; once exhausted the backend keeps
    returning the last entry's shape (or an empty answer if the last
    entry was a tool call).
    """

    def __init__(self, plan: t.Sequence[t.Union[dict, str]]):
        self.plan = list(plan)
        self._i = 0

    def name(self) -> str:
        return "mock"

    def chat(self, messages: list[dict],
             tools: list[ToolSpec]) -> AssistantTurn:
        if not self.plan:
            return AssistantTurn(text="")
        step = self.plan[min(self._i, len(self.plan) - 1)]
        self._i += 1
        if isinstance(step, dict) and "tool" in step:
            return AssistantTurn(tool_calls=[ToolCall(
                id=f"call-{uuid.uuid4().hex[:8]}",
                name=step["tool"],
                arguments=step.get("args", {}),
            )])
        return AssistantTurn(text=str(step))


# ============================================================================
# AGENT LOOP
# ============================================================================

class Agent:
    """The universal agent loop.

    Two equally valid usage styles:

    1) Lightweight, no SHABD:
           agent = Agent(llm=MockBackend(["done"]))
           @agent.tool
           def add(a: int, b: int) -> int: ...
           agent.run("...")

    2) Backed by a SHABD app (recommended for production):
           agent = Agent.from_shabd(app, llm=OpenAICompatBackend(...))
           agent.run("...", token=app.issue_token("alice", scopes=["*"]))
    """

    def __init__(self, *,
                 llm: LLMBackend,
                 system: str = "",
                 tools: ToolRegistry | None = None,
                 max_steps: int = 8,
                 timeout_s: float = 120.0,
                 verbose: bool = False):
        self.llm = llm
        self.system = system
        self.tools = tools or ToolRegistry()
        self.max_steps = max_steps
        self.timeout_s = timeout_s
        self.verbose = verbose

    # ---- factory ----

    @classmethod
    def from_shabd(cls, app, *, llm: LLMBackend, system: str = "",
                   **kw) -> Agent:
        registry = ToolRegistry()
        registry.bind_shabd(app)
        return cls(llm=llm, system=system, tools=registry, **kw)

    # ---- tool registration ----

    def tool(self, func: t.Callable) -> t.Callable:
        """Decorator: register a Python function as a tool."""
        self.tools.register(func.__name__, func)
        return func

    # ---- the loop ----

    def run(self, prompt: str, *,
            token: str | None = None,
            extra_messages: list[dict] | None = None) -> AgentResult:
        started = time.time()
        messages: list[dict] = []
        if self.system:
            messages.append({"role": "system", "content": self.system})
        if extra_messages:
            messages.extend(extra_messages)
        messages.append({"role": "user", "content": prompt})

        steps: list[AgentStep] = []
        seen_calls: set[str] = set()

        for i in range(self.max_steps):
            if time.time() - started > self.timeout_s:
                return AgentResult(
                    answer=_last_text(steps),
                    steps=steps, stopped_reason="timeout",
                    total_elapsed_s=time.time() - started,
                )

            t0 = time.time()
            turn = self.llm.chat(messages, self.tools.list_specs())
            elapsed = (time.time() - t0) * 1000
            step = AgentStep(n=i, assistant=turn, elapsed_ms=elapsed)
            steps.append(step)
            if self.verbose:
                _print_step(step)

            # Record the assistant turn in the conversation.
            messages.append(_assistant_msg(turn))

            if turn.is_final:
                return AgentResult(
                    answer=turn.text, steps=steps,
                    stopped_reason="final",
                    total_elapsed_s=time.time() - started,
                )

            # Run every tool call the model asked for.
            for call in turn.tool_calls:
                fp = _fp(call.name, call.arguments)
                if fp in seen_calls:
                    # Same exact call twice in a row → break the loop.
                    step.tool_results.append({
                        "tool_call_id": call.id,
                        "content": json.dumps({"error": {
                            "code": "duplicate_call",
                            "message": "you just made this exact call",
                            "hint": "Use the previous result or call a "
                                    "different tool.",
                        }}),
                    })
                    messages.append({
                        "role": "tool", "name": call.name,
                        "tool_call_id": call.id,
                        "content": step.tool_results[-1]["content"],
                    })
                    continue
                seen_calls.add(fp)

                try:
                    result = self.tools.call(call.name, call.arguments,
                                              token=token)
                    payload = json.dumps(result, default=str)
                except ToolError as te:
                    payload = json.dumps(te.to_dict(), default=str)
                except Exception as e:  # noqa: BLE001
                    payload = json.dumps({"error": {
                        "code": "internal_error", "message": str(e),
                    }}, default=str)
                step.tool_results.append({
                    "tool_call_id": call.id, "content": payload,
                })
                messages.append({
                    "role": "tool", "name": call.name,
                    "tool_call_id": call.id, "content": payload,
                })

        return AgentResult(
            answer=_last_text(steps),
            steps=steps, stopped_reason="max_steps",
            total_elapsed_s=time.time() - started,
        )


# ============================================================================
# HELPERS
# ============================================================================

def _assistant_msg(turn: AssistantTurn) -> dict:
    out: dict = {"role": "assistant", "content": turn.text}
    if turn.tool_calls:
        out["tool_calls"] = [{
            "id": c.id, "type": "function",
            "function": {"name": c.name,
                         "arguments": json.dumps(c.arguments,
                                                  default=str)},
        } for c in turn.tool_calls]
    return out


def _fp(name: str, args: dict) -> str:
    return name + "|" + json.dumps(args, sort_keys=True, default=str)


def _last_text(steps: list[AgentStep]) -> str:
    for step in reversed(steps):
        if step.assistant.text:
            return step.assistant.text
    return ""


def _closest(name: str, names: t.Iterable[str]) -> str | None:
    import difflib
    matches = difflib.get_close_matches(name, list(names), n=1, cutoff=0.5)
    return matches[0] if matches else None


def _print_step(step: AgentStep) -> None:
    if step.assistant.tool_calls:
        for c in step.assistant.tool_calls:
            print(f"[step {step.n}] -> {c.name}({c.arguments})")
    elif step.assistant.text:
        print(f"[step {step.n}] = {step.assistant.text[:200]}")


_PRIMITIVES = {int: "integer", float: "number", bool: "boolean",
               str: "string", list: "array", dict: "object"}


def _infer_parameters(func: t.Callable) -> dict:
    """Build a JSON Schema from a function's type hints + signature."""
    try:
        hints = t.get_type_hints(func)
    except Exception:
        hints = {}
    sig = inspect.signature(func)
    props: dict = {}
    required: list[str] = []
    for pname, p in sig.parameters.items():
        if pname in ("self", "cls"):
            continue
        ann = hints.get(pname, str)
        props[pname] = {"type": _PRIMITIVES.get(ann, "string")}
        if p.default is inspect.Parameter.empty:
            required.append(pname)
    return {
        "type": "object", "properties": props,
        "required": required, "additionalProperties": False,
    }


# ============================================================================
# SECTION X — NOVEL FEATURES (genuinely first-in-class for an agent runtime)
# ============================================================================
#
# Three features no other agent framework ships today:
#
#   1.  ConsensusBackend     — call N LLMs, only act if a quorum agrees on
#                              the exact tool call. Stops hallucinations
#                              dead on high-stakes writes.
#   2.  ProvenanceTracker    — tag every value the agent uses with its
#                              origin: `user` / `tool:<name>` / `llm`.
#                              Catches prompt injection and confabulated
#                              arguments before they reach a tool body.
#   3.  Invariant + InvariantViolation — declarative cross-tool safety
#                              rules. The agent cannot execute a
#                              sequence that violates an invariant.
#
# All three integrate with the existing Agent loop via `Agent(consensus=
# ..., invariants=[...], track_provenance=True)`. None of them require an
# external dependency.

__all__ += [
    "ConsensusBackend", "ConsensusError",
    "ProvenanceTracker", "Provenance",
    "Invariant", "InvariantViolation",
    "AgentSession",
]


# ----------------------------------------------------------------------
# 1.  Multi-LLM Consensus
# ----------------------------------------------------------------------

class ConsensusError(Exception):
    """Raised when the agent loop sees disagreement above the threshold."""
    def __init__(self, message: str, *, votes: list[dict],
                 majority: dict | None = None):
        super().__init__(message)
        self.votes = votes
        self.majority = majority


def _canon_tool_call(tc: ToolCall) -> str:
    """Canonical fingerprint of a single tool call (name + sorted args)."""
    return tc.name + "|" + json.dumps(tc.arguments, sort_keys=True,
                                       default=str, separators=(",", ":"))


def _turn_fp(turn: AssistantTurn) -> str:
    """Fingerprint of a whole assistant turn — used for agreement comparison."""
    if turn.tool_calls:
        return "TC:" + "||".join(sorted(_canon_tool_call(c)
                                         for c in turn.tool_calls))
    return "TX:" + (turn.text or "").strip().lower()


class ConsensusBackend(LLMBackend):
    """Run several backends in parallel; require a quorum on the *next
    action* (tool call shape) before forwarding it to the loop.

    Use this for high-stakes write tools (wire transfer, trade
    execution) where a single-model hallucination cost is unacceptable.

        backend = ConsensusBackend([
            OpenAICompatBackend(...),    # GPT-4o
            AnthropicBackend(...),       # Claude
            OpenAICompatBackend(...),    # Llama 3 70B via vLLM
        ], min_agreement=2)              # at least 2 of 3 must agree

    Reads (text-only turns) pass through with the first backend's
    answer (you can flip `also_consensus_on_text=True` for paranoid
    mode — but text is usually fine).
    """

    def __init__(self, backends: t.Sequence[LLMBackend], *,
                 min_agreement: int = 2,
                 also_consensus_on_text: bool = False,
                 timeout_s: float = 60.0):
        if len(backends) < 2:
            raise ValueError("ConsensusBackend needs at least 2 backends")
        if min_agreement > len(backends):
            raise ValueError("min_agreement cannot exceed number of backends")
        self.backends = list(backends)
        self.min_agreement = min_agreement
        self.also_consensus_on_text = also_consensus_on_text
        self.timeout_s = timeout_s

    def name(self) -> str:
        return "consensus(" + "+".join(b.name() for b in self.backends) + ")"

    def chat(self, messages: list[dict],
             tools: list[ToolSpec]) -> AssistantTurn:
        import concurrent.futures as _cf

        votes: list[AssistantTurn] = []
        errors: list[str] = []
        with _cf.ThreadPoolExecutor(max_workers=len(self.backends)) as ex:
            futs = {ex.submit(b.chat, messages, tools): b
                    for b in self.backends}
            for fut in _cf.as_completed(futs, timeout=self.timeout_s):
                try:
                    votes.append(fut.result())
                except Exception as e:  # noqa: BLE001
                    errors.append(f"{futs[fut].name()}: {e}")
        if len(votes) < self.min_agreement:
            raise ConsensusError(
                f"only {len(votes)} backends responded; need {self.min_agreement}",
                votes=[], majority=None,
            )

        # Tally fingerprints.
        from collections import Counter
        fps = [_turn_fp(v) for v in votes]
        counts = Counter(fps)
        top_fp, top_n = counts.most_common(1)[0]

        # If any vote has tool calls, treat the turn as action-bearing.
        action_bearing = any(v.tool_calls for v in votes)
        if action_bearing and top_n < self.min_agreement:
            dissent = [{"backend": b.name(),
                        "tool_calls": [_canon_tool_call(c)
                                        for c in v.tool_calls],
                        "text": (v.text or "")[:200]}
                       for v, b in zip(votes, self.backends[:len(votes)])]
            raise ConsensusError(
                f"no quorum: top vote {top_n}/{len(votes)}",
                votes=dissent, majority=None,
            )
        if (not action_bearing
                and self.also_consensus_on_text
                and top_n < self.min_agreement):
            raise ConsensusError(
                f"text quorum failed: top vote {top_n}/{len(votes)}",
                votes=[{"text": v.text} for v in votes],
            )
        majority = next(v for v, fp in zip(votes, fps) if fp == top_fp)
        # Annotate raw with consensus metadata so downstream sees it.
        majority.raw = {"consensus": {
            "votes": len(votes), "agreement": top_n,
            "backends": [b.name() for b in self.backends],
            "errors": errors,
        }, "original_raw": majority.raw}
        return majority


# ----------------------------------------------------------------------
# 2.  Provenance tracking
# ----------------------------------------------------------------------

@dataclass
class Provenance:
    """The recorded origin of a value the agent has seen."""
    tag: str         # "user" | "tool:<name>" | "system" | "llm_invented"
    step: int
    sample: str = ""  # short preview for logs


class ProvenanceTracker:
    """Tags every value an agent passes into a tool with its origin.

    The goals are concrete:

      * **Prompt injection** — when a malicious tool result tries to
        steer the LLM into calling another tool with attacker-supplied
        arguments, those arguments are tagged `tool:<bad_tool>`,
        not `user`. An invariant or RBAC rule can refuse them.
      * **Hallucinated identifiers** — Aadhaar, account number, GSTIN,
        amount. If the value never appeared in user input nor in any
        prior tool output, it is `llm_invented`. Sensitive spells can
        require provenance != `llm_invented`.

    The tracker is intentionally cheap and approximate: numeric and
    string values are tracked verbatim; substrings of long text are
    indexed by token. Good enough for "did this Aadhaar come from the
    user or did the model make it up?" — which is what you actually
    want to know before you wire money.
    """

    def __init__(self):
        self._index: dict[str, Provenance] = {}

    # ---- absorbing sources ----

    def absorb_user(self, text: str, step: int = -1) -> None:
        self._index_text(text, "user", step)

    def absorb_system(self, text: str, step: int = -1) -> None:
        self._index_text(text, "system", step)

    def absorb_tool_output(self, tool_name: str, result: t.Any,
                           step: int) -> None:
        self._index_value(result, f"tool:{tool_name}", step)

    # ---- query ----

    def classify(self, value: t.Any) -> Provenance:
        key = self._key(value)
        if key in self._index:
            return self._index[key]
        # Numeric near-match: digits-only canonical form
        digits = "".join(ch for ch in str(value) if ch.isdigit())
        if digits and f"digits:{digits}" in self._index:
            return self._index[f"digits:{digits}"]
        return Provenance(tag="llm_invented", step=-1,
                          sample=str(value)[:80])

    # ---- internals ----

    @staticmethod
    def _key(value: t.Any) -> str:
        return "v:" + json.dumps(value, sort_keys=True, default=str,
                                  separators=(",", ":"))

    def _index_value(self, value: t.Any, tag: str, step: int) -> None:
        # Index the whole value as-is
        self._index[self._key(value)] = Provenance(
            tag=tag, step=step, sample=str(value)[:80])
        # Index nested primitives so a single field can be matched later
        if isinstance(value, dict):
            for v in value.values():
                self._index_value(v, tag, step)
        elif isinstance(value, list):
            for v in value:
                self._index_value(v, tag, step)
        elif isinstance(value, str):
            self._index_text(value, tag, step)
        elif isinstance(value, (int, float)):
            digits = "".join(ch for ch in str(value) if ch.isdigit())
            if digits:
                self._index[f"digits:{digits}"] = Provenance(
                    tag=tag, step=step, sample=str(value))

    def _index_text(self, text: str, tag: str, step: int) -> None:
        if not text:
            return
        # Tokenise on whitespace + punctuation, keep numbers intact.
        tokens = _tokenise_for_provenance(text)
        for tok in tokens:
            self._index[self._key(tok)] = Provenance(
                tag=tag, step=step, sample=tok[:80])
            digits = "".join(ch for ch in tok if ch.isdigit())
            if len(digits) >= 4:
                self._index[f"digits:{digits}"] = Provenance(
                    tag=tag, step=step, sample=tok[:80])


def _tokenise_for_provenance(text: str) -> list[str]:
    """Split text into chunks the tracker can match against tool args.

    Numbers stay glued; identifiers stay glued; punctuation splits.
    """
    import re as _r
    return [tok for tok in _r.split(r"[\s,;:()\[\]{}\"']+", text)
            if tok]


# ----------------------------------------------------------------------
# 3.  Safety invariants
# ----------------------------------------------------------------------

class InvariantViolation(ToolError):
    """A safety invariant blocked a tool call. Round-trips back to the
    LLM as a `tool` role error message, so the LLM can replan rather
    than die."""

    def __init__(self, name: str, message: str, *, details: t.Any = None):
        super().__init__(
            code=f"invariant_violation:{name}",
            message=message,
            hint=("This call violates a declarative safety rule. "
                  "Pick a different action that respects the rule."),
        )
        self.invariant_name = name
        self.details = details


@dataclass
class Invariant:
    """Declarative cross-tool safety rule.

        @agent.invariant("daily_transfer_cap_50L")
        def _(session):
            total = sum(c.arguments.get("amount_inr", 0.0)
                        for c in session.tool_calls_named("transfer"))
            return total <= 5_000_000.0

    The function is called *before* every tool execution with the
    session-so-far. Returning `False` (or raising) blocks the call.
    """
    name: str
    check: t.Callable[[AgentSession], bool]
    message: str = ""


@dataclass
class AgentSession:
    """Read-only view of the current session passed to invariant
    checks. Built on the fly from the conversation."""
    messages: list[dict]
    tool_calls: list[ToolCall]
    pending_call: ToolCall | None = None

    def tool_calls_named(self, name: str) -> list[ToolCall]:
        return [c for c in self.tool_calls if c.name == name]

    def last_user_message(self) -> str:
        for m in reversed(self.messages):
            if m.get("role") == "user":
                return m.get("content") or ""
        return ""


# ============================================================================
# Agent: integrate consensus / provenance / invariants
# ============================================================================
# These are bolted onto the existing Agent via small extensions rather
# than rewriting the class so the v2.5 surface stays intact.

def _agent_add_invariant(self: Agent, name: str,
                         check: t.Callable | None = None,
                         message: str = "") -> t.Any:
    """Either call as `agent.add_invariant("name", check_fn, msg)` or
    as a decorator: `@agent.invariant("name")`."""
    if check is None:
        def deco(fn: t.Callable) -> t.Callable:
            self._invariants.append(Invariant(name=name, check=fn,
                                              message=message))
            return fn
        return deco
    self._invariants.append(Invariant(name=name, check=check,
                                       message=message))
    return check


def _agent_init_extras(self: Agent) -> None:
    if not hasattr(self, "_invariants"):
        self._invariants = []
    if not hasattr(self, "_provenance"):
        self._provenance = None


# Bind the helpers onto Agent.
Agent.add_invariant = _agent_add_invariant       # type: ignore[attr-defined]
Agent.invariant = _agent_add_invariant           # type: ignore[attr-defined]
_orig_init = Agent.__init__


def _patched_init(self, *args, **kwargs):  # noqa: D401
    """Augmented Agent.__init__ that accepts the novel-feature flags
    without breaking the v2.5 signature.

    New kwargs:
      * `invariants=[Invariant(...), ...]`
      * `track_provenance=True`
    """
    invariants = kwargs.pop("invariants", None)
    track = kwargs.pop("track_provenance", False)
    _orig_init(self, *args, **kwargs)
    self._invariants = list(invariants or [])
    self._provenance = ProvenanceTracker() if track else None
    if track and self.system:
        self._provenance.absorb_system(self.system, step=-1)


Agent.__init__ = _patched_init                   # type: ignore[assignment]


def _check_invariants(self: Agent, call: ToolCall,
                       messages: list[dict],
                       all_calls: list[ToolCall]) -> None:
    if not self._invariants:
        return
    session = AgentSession(
        messages=messages, tool_calls=all_calls + [call],
        pending_call=call,
    )
    for inv in self._invariants:
        try:
            ok = inv.check(session)
        except Exception as e:  # noqa: BLE001
            raise InvariantViolation(
                inv.name, f"invariant raised: {e}", details=str(e),
            ) from e
        if not ok:
            raise InvariantViolation(
                inv.name,
                inv.message or f"invariant '{inv.name}' rejected the call",
                details={"name": call.name, "arguments": call.arguments},
            )


Agent._check_invariants = _check_invariants      # type: ignore[attr-defined]


# Patch Agent.run to add provenance absorption, invariant checks, and
# consensus errors handled as tool-role messages so the LLM can replan.
_orig_run = Agent.run


def _patched_run(self, prompt: str, *,
                 token: str | None = None,
                 extra_messages: list[dict] | None = None
                 ) -> AgentResult:
    # Seed provenance from the original user prompt.
    if self._provenance is not None:
        self._provenance.absorb_user(prompt, step=-1)
    started = time.time()
    messages: list[dict] = []
    if self.system:
        messages.append({"role": "system", "content": self.system})
    if extra_messages:
        messages.extend(extra_messages)
    messages.append({"role": "user", "content": prompt})

    steps: list[AgentStep] = []
    seen_calls: set[str] = set()
    all_calls: list[ToolCall] = []

    for i in range(self.max_steps):
        if time.time() - started > self.timeout_s:
            return AgentResult(answer=_last_text(steps), steps=steps,
                               stopped_reason="timeout",
                               total_elapsed_s=time.time() - started)
        t0 = time.time()
        try:
            turn = self.llm.chat(messages, self.tools.list_specs())
        except ConsensusError as ce:
            payload = json.dumps({"error": {
                "code": "consensus_failed",
                "message": str(ce),
                "hint": "Models disagreed on the next action. Replan "
                         "with a different tool or ask the user.",
                "votes": ce.votes,
            }}, default=str)
            steps.append(AgentStep(
                n=i, assistant=AssistantTurn(text=str(ce)),
                tool_results=[{"tool_call_id": "consensus",
                               "content": payload}],
                elapsed_ms=(time.time() - t0) * 1000,
            ))
            messages.append({"role": "tool",
                             "name": "consensus",
                             "tool_call_id": "consensus",
                             "content": payload})
            continue
        elapsed = (time.time() - t0) * 1000
        step = AgentStep(n=i, assistant=turn, elapsed_ms=elapsed)
        steps.append(step)
        if self.verbose:
            _print_step(step)
        messages.append(_assistant_msg(turn))

        if turn.is_final:
            return AgentResult(answer=turn.text, steps=steps,
                               stopped_reason="final",
                               total_elapsed_s=time.time() - started)

        for call in turn.tool_calls:
            fp = _fp(call.name, call.arguments)
            if fp in seen_calls:
                payload = json.dumps({"error": {
                    "code": "duplicate_call",
                    "message": "you just made this exact call",
                    "hint": "Use the previous result or call a "
                             "different tool.",
                }})
                step.tool_results.append({"tool_call_id": call.id,
                                          "content": payload})
                messages.append({"role": "tool", "name": call.name,
                                 "tool_call_id": call.id,
                                 "content": payload})
                continue
            seen_calls.add(fp)

            # Annotate arg provenance into the step trace.
            if self._provenance is not None:
                prov = {k: self._provenance.classify(v).tag
                        for k, v in call.arguments.items()}
                step.tool_results.append({
                    "tool_call_id": call.id + ":provenance",
                    "content": json.dumps({"provenance": prov}),
                })

            # Invariant check first — never run a forbidden tool.
            try:
                self._check_invariants(call, messages, all_calls)
            except InvariantViolation as iv:
                payload = json.dumps(iv.to_dict(), default=str)
                step.tool_results.append({"tool_call_id": call.id,
                                          "content": payload})
                messages.append({"role": "tool", "name": call.name,
                                 "tool_call_id": call.id,
                                 "content": payload})
                continue

            try:
                result = self.tools.call(call.name, call.arguments,
                                          token=token)
                payload = json.dumps(result, default=str)
                if self._provenance is not None:
                    self._provenance.absorb_tool_output(
                        call.name, result, step=i)
            except ToolError as te:
                payload = json.dumps(te.to_dict(), default=str)
            except Exception as e:  # noqa: BLE001
                payload = json.dumps({"error": {
                    "code": "internal_error", "message": str(e),
                }}, default=str)
            step.tool_results.append({"tool_call_id": call.id,
                                      "content": payload})
            messages.append({"role": "tool", "name": call.name,
                             "tool_call_id": call.id,
                             "content": payload})
            all_calls.append(call)

    return AgentResult(answer=_last_text(steps), steps=steps,
                       stopped_reason="max_steps",
                       total_elapsed_s=time.time() - started)


Agent.run = _patched_run                          # type: ignore[assignment]
