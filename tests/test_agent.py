"""
Tests for shabd_agent — universal, zero-dependency agent runtime.

Run:
    python tests/test_agent.py
"""
from __future__ import annotations

import http.server
import json
import os
import socketserver
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shabd import SHABD  # noqa: E402
from shabd_agent import (  # noqa: E402
    Agent,
    AnthropicBackend,
    GeminiBackend,
    MockBackend,
    OpenAICompatBackend,
    ToolError,
    ToolRegistry,
)


# ---------------------------------------------------------------------------
# 1) Tool registry — registration, lookup, did_you_mean
# ---------------------------------------------------------------------------
class RegistryTests(unittest.TestCase):
    def test_register_and_call(self):
        r = ToolRegistry()

        def add(a: int, b: int) -> int:
            return a + b

        r.register("add", add)
        self.assertEqual(r.call("add", {"a": 2, "b": 3}), 5)

    def test_unknown_tool_suggests_close_match(self):
        r = ToolRegistry()
        r.register("search_docs", lambda query: [])
        try:
            r.call("serach_docs", {"query": "x"})
            self.fail("expected ToolError")
        except ToolError as e:
            self.assertEqual(e.code, "tool_not_found")
            self.assertEqual(e.did_you_mean, ["search_docs"])

    def test_bad_arguments_returns_structured_error(self):
        r = ToolRegistry()

        def add(a: int, b: int) -> int:
            return a + b

        r.register("add", add)
        with self.assertRaises(ToolError):
            r.call("add", {"a": 1})   # missing b

    def test_inferred_parameters_schema(self):
        r = ToolRegistry()

        def f(name: str, count: int = 1) -> str:
            return name * count

        r.register("f", f)
        spec = r.list_specs()[0]
        self.assertEqual(spec.parameters["properties"]["name"]["type"],
                         "string")
        self.assertEqual(spec.parameters["properties"]["count"]["type"],
                         "integer")
        self.assertEqual(spec.parameters["required"], ["name"])


# ---------------------------------------------------------------------------
# 2) MockBackend — happy path, multi-step, exhausted plan
# ---------------------------------------------------------------------------
class MockAgentTests(unittest.TestCase):
    def test_single_tool_then_final(self):
        calls = []

        agent = Agent(llm=MockBackend(plan=[
            {"tool": "add", "args": {"a": 7, "b": 5}},
            "The answer is 12.",
        ]))

        @agent.tool
        def add(a: int, b: int) -> int:
            calls.append((a, b))
            return a + b

        result = agent.run("7 + 5?")
        self.assertEqual(result.answer, "The answer is 12.")
        self.assertEqual(result.stopped_reason, "final")
        self.assertEqual(calls, [(7, 5)])
        self.assertEqual(len(result.steps), 2)

    def test_max_steps_stops(self):
        agent = Agent(
            llm=MockBackend(plan=[{"tool": "noop", "args": {}}]),
            max_steps=3,
        )

        @agent.tool
        def noop() -> str:
            return "done"

        result = agent.run("loop")
        self.assertEqual(result.stopped_reason, "max_steps")

    def test_duplicate_call_does_not_re_execute(self):
        runs = []
        agent = Agent(llm=MockBackend(plan=[
            {"tool": "echo", "args": {"x": "hi"}},
            {"tool": "echo", "args": {"x": "hi"}},   # exact duplicate
            "stopped duplicate",
        ]), max_steps=5)

        @agent.tool
        def echo(x: str) -> str:
            runs.append(x)
            return x

        result = agent.run("repeat")
        self.assertEqual(runs.count("hi"), 1)
        self.assertEqual(result.answer, "stopped duplicate")

    def test_did_you_mean_round_trips_to_llm(self):
        agent = Agent(llm=MockBackend(plan=[
            {"tool": "transfer", "args": {"x": 1}},      # typo
            "Sorry, I'll use transfer_money next time.",
        ]))

        @agent.tool
        def transfer_money(amount: float) -> str:
            return "ok"

        result = agent.run("send money")
        last_tool_msg = next(
            r for r in result.steps[0].tool_results if r
        )
        decoded = json.loads(last_tool_msg["content"])
        self.assertEqual(decoded["error"]["code"], "tool_not_found")
        self.assertEqual(decoded["error"]["did_you_mean"], ["transfer_money"])


# ---------------------------------------------------------------------------
# 3) SHABD-backed agent — bind_shabd routes through validation + audit
# ---------------------------------------------------------------------------
class ShabdBoundAgentTests(unittest.TestCase):
    def test_calls_go_through_shabd_invoke(self):
        app = SHABD("agent-bound", secret="x" * 32, require_auth=False)

        @app.spell
        def add(a: int, b: int) -> int:
            return a + b

        agent = Agent.from_shabd(
            app,
            llm=MockBackend(plan=[
                {"tool": "add", "args": {"a": 9, "b": 11}},
                "20",
            ]),
        )
        result = agent.run("9 + 11?")
        self.assertEqual(result.answer, "20")
        # The call landed in the Grimoire chain — proof the route was via SHABD.
        pages = app.grimoire.pages()
        self.assertTrue(any(p["spell"] == "add" for p in pages))
        self.assertTrue(app.grimoire.verify()["ok"])


# ---------------------------------------------------------------------------
# 4) Real HTTP backend — OpenAICompatBackend against a local fake server
# ---------------------------------------------------------------------------
def _start_fake_openai(port: int, responses) -> threading.Thread:
    """Spin up a tiny HTTP server that replays canned OpenAI-shaped
    responses in order."""

    class Handler(http.server.BaseHTTPRequestHandler):
        i = 0

        def log_message(self, *a, **k):
            pass

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("content-length") or "0")
            self.rfile.read(length)
            resp = responses[min(Handler.i, len(responses) - 1)]
            Handler.i += 1
            body = json.dumps(resp).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    socketserver.ThreadingTCPServer.allow_reuse_address = True
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


class OpenAICompatHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = _start_fake_openai(0, responses=[
            # 1st call -> ask to use 'add'
            {"choices": [{"message": {
                "role": "assistant", "content": None,
                "tool_calls": [{
                    "id": "c1", "type": "function",
                    "function": {"name": "add",
                                 "arguments": json.dumps({"a": 4,
                                                          "b": 6})},
                }],
            }}]},
            # 2nd call -> final answer
            {"choices": [{"message": {
                "role": "assistant", "content": "It is 10.",
            }}]},
        ])

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_end_to_end_against_fake_openai(self):
        agent = Agent(llm=OpenAICompatBackend(
            base_url=f"http://127.0.0.1:{self.srv.server_address[1]}/v1",
            api_key="sk-test", model="gpt-x",
        ))

        @agent.tool
        def add(a: int, b: int) -> int:
            return a + b

        result = agent.run("4 + 6?")
        self.assertEqual(result.answer, "It is 10.")
        self.assertEqual(result.stopped_reason, "final")


# ---------------------------------------------------------------------------
# 5) Anthropic + Gemini message conversion — pure unit tests
# ---------------------------------------------------------------------------
class AnthropicConversionTests(unittest.TestCase):
    def test_system_extracted_and_tools_rendered(self):
        b = AnthropicBackend(model="m", api_key="x")
        sys_str, msgs = b._convert_messages([
            {"role": "system", "content": "you help."},
            {"role": "user", "content": "hi"},
        ])
        self.assertEqual(sys_str, "you help.")
        self.assertEqual(msgs[0]["role"], "user")

    def test_tool_call_assistant_becomes_blocks(self):
        b = AnthropicBackend(model="m", api_key="x")
        _, msgs = b._convert_messages([
            {"role": "assistant", "content": "thinking",
             "tool_calls": [{"id": "c1", "type": "function",
                             "function": {"name": "x",
                                          "arguments": "{\"a\":1}"}}]},
        ])
        self.assertEqual(msgs[0]["role"], "assistant")
        kinds = [b.get("type") for b in msgs[0]["content"]]
        self.assertIn("tool_use", kinds)
        self.assertIn("text", kinds)


class GeminiConversionTests(unittest.TestCase):
    def test_assistant_with_tool_call_renders_functionCall(self):
        b = GeminiBackend(model="m", api_key="x")
        _, contents = b._convert_messages([
            {"role": "assistant", "content": "",
             "tool_calls": [{"id": "c1", "type": "function",
                             "function": {"name": "ping",
                                          "arguments": "{}"}}]},
        ])
        self.assertEqual(contents[0]["role"], "model")
        self.assertEqual(contents[0]["parts"][0]["functionCall"]["name"],
                         "ping")


def main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for cls in (RegistryTests, MockAgentTests, ShabdBoundAgentTests,
                OpenAICompatHTTPTests, AnthropicConversionTests,
                GeminiConversionTests):
        suite.addTests(loader.loadTestsFromTestCase(cls))
    runner = unittest.TextTestRunner(verbosity=2, stream=sys.stdout)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
