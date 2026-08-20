"""
SHABD test suite — runnable with stdlib only:

    python tests/test_shabd.py

No pytest, no extras. Mirrors the spirit of `shabd.py` itself: one file,
zero dependencies, easy to audit.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request

# Make `shabd` importable when running from repo root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shabd import (  # noqa: E402
    GSTIN,
    SHABD,
    URL,
    Aadhaar,
    AuthError,
    Email,
    Grimoire,
    IndianPhone,
    Money,
    SpellNotFoundError,
    ValidationError,
    __version__,
)


def _new_app(name: str = "test", **kw) -> SHABD:
    return SHABD(name, secret="x" * 32, require_auth=False, **kw)


# ---------------------------------------------------------------------------
# 1. Core spell lifecycle
# ---------------------------------------------------------------------------
class CoreTests(unittest.TestCase):
    def test_version_bumped(self):
        major, minor, *_ = __version__.split(".")
        self.assertEqual(major, "2")
        self.assertGreaterEqual(int(minor), 1)

    def test_register_and_invoke(self):
        app = _new_app()

        @app.spell
        def add(a: int, b: int) -> int:
            return a + b

        self.assertEqual(app.invoke("add", {"a": 2, "b": 3}), 5)

    def test_register_duplicate_raises(self):
        app = _new_app()

        @app.spell
        def echo(x: str) -> str:
            return x

        with self.assertRaises(ValueError):
            @app.spell(name="echo")
            def echo2(y: str) -> str:
                return y

    def test_default_values_in_schema(self):
        app = _new_app()

        @app.spell
        def greet(name: str = "World") -> str:
            return f"Hello, {name}!"

        schema = app._spells["greet"].schema
        self.assertEqual(schema["properties"]["name"]["default"], "World")
        self.assertEqual(schema["required"], [])
        self.assertEqual(app.invoke("greet", {}), "Hello, World!")

    def test_async_spell(self):
        app = _new_app()

        @app.spell
        async def slow(x: int) -> int:
            return x * 2

        self.assertEqual(app.invoke("slow", {"x": 21}), 42)


# ---------------------------------------------------------------------------
# 2. Validation errors carry AI-native hints
# ---------------------------------------------------------------------------
class ValidationTests(unittest.TestCase):
    def test_wrong_type_raises(self):
        app = _new_app()

        @app.spell
        def add(a: int, b: int) -> int:
            return a + b

        with self.assertRaises(ValidationError):
            app.invoke("add", {"a": "oops", "b": 1})

    def test_missing_required(self):
        app = _new_app()

        @app.spell
        def add(a: int, b: int) -> int:
            return a + b

        with self.assertRaises(ValidationError):
            app.invoke("add", {"a": 1})


# ---------------------------------------------------------------------------
# 3. AI-Native errors: did_you_mean + hint
# ---------------------------------------------------------------------------
class AINativeErrorTests(unittest.TestCase):
    def test_unknown_spell_suggests_closest(self):
        app = _new_app()

        @app.spell
        def search_docs(query: str) -> str:
            return ""

        try:
            app.invoke("serach_docs", {"query": "x"})
            self.fail("expected SpellNotFoundError")
        except SpellNotFoundError as e:
            d = e.to_dict()["error"]
            self.assertEqual(d["did_you_mean"], ["search_docs"])
            self.assertIn("search_docs", d["hint"])

    def test_validation_error_includes_example(self):
        app = _new_app()

        @app.spell
        def kyc(email: Email) -> dict:
            return {"ok": True}

        try:
            app.invoke("kyc", {"email": "not-an-email"})
            self.fail("expected ValidationError")
        except ValidationError as e:
            d = e.to_dict()["error"]
            self.assertEqual(d["example"], "alice@example.com")
            self.assertIn("user@domain.tld", d["hint"])


# ---------------------------------------------------------------------------
# 4. Semantic types — schema + validation + PII flag
# ---------------------------------------------------------------------------
class SemanticTypeTests(unittest.TestCase):
    def test_email_validates_in_constructor(self):
        Email("alice@example.com")
        with self.assertRaises(ValidationError):
            Email("not an email")

    def test_indian_phone(self):
        IndianPhone("+919876543210")
        IndianPhone("9876543210")
        with self.assertRaises(ValidationError):
            IndianPhone("12345")

    def test_gstin(self):
        GSTIN("27AAPFU0939F1ZV")
        with self.assertRaises(ValidationError):
            GSTIN("not-a-gstin")

    def test_aadhaar(self):
        Aadhaar("123456789012")
        with self.assertRaises(ValidationError):
            Aadhaar("abcd")

    def test_money(self):
        Money("1500.00 INR")
        Money("99 USD")
        with self.assertRaises(ValidationError):
            Money("100 rupees")

    def test_url(self):
        URL("https://example.com/path?q=1")
        with self.assertRaises(ValidationError):
            URL("ftp://example.com")

    def test_schema_marks_pii(self):
        app = _new_app()

        @app.spell
        def kyc(email: Email, aadhaar: Aadhaar, gstin: GSTIN) -> dict:
            return {"ok": True}

        s = app._spells["kyc"].schema
        self.assertTrue(s["properties"]["email"]["x-pii"])
        self.assertTrue(s["properties"]["aadhaar"]["x-pii"])
        self.assertFalse(s["properties"]["gstin"]["x-pii"])
        self.assertEqual(s["properties"]["email"]["x-semantic"], "email")

    def test_http_request_validates_pattern(self):
        """Schema-driven pattern check fires even when the value is a plain string."""
        app = _new_app()

        @app.spell
        def send(email: Email) -> str:
            return "queued"

        with self.assertRaises(ValidationError):
            app.invoke("send", {"email": "not-an-email"})


# ---------------------------------------------------------------------------
# 5. Grimoire — hash chain integrity, tamper-evidence, PII redaction
# ---------------------------------------------------------------------------
class GrimoireTests(unittest.TestCase):
    def test_chain_grows_with_calls(self):
        app = _new_app()

        @app.spell
        def add(a: int, b: int) -> int:
            return a + b

        for i in range(5):
            app.invoke("add", {"a": i, "b": 1})

        self.assertEqual(len(app.grimoire._pages), 5)
        v = app.grimoire.verify()
        self.assertTrue(v["ok"], v)
        self.assertEqual(v["pages"], 5)

    def test_failed_calls_are_recorded(self):
        app = _new_app()

        @app.spell
        def boom() -> str:
            raise RuntimeError("kaboom")

        try:
            app.invoke("boom", {})
        except Exception:
            pass
        pages = app.grimoire.pages()
        self.assertEqual(len(pages), 1)
        self.assertFalse(pages[0]["ok"])

    def test_tamper_breaks_chain(self):
        app = _new_app()

        @app.spell
        def add(a: int, b: int) -> int:
            return a + b

        for i in range(3):
            app.invoke("add", {"a": i, "b": 1})

        # Tamper: edit a past page's args_hash
        app.grimoire._pages[1]["args_hash"] = "0" * 64
        v = app.grimoire.verify()
        self.assertFalse(v["ok"])
        self.assertEqual(v["reason"], "hash mismatch")
        self.assertEqual(v["at_seq"], 1)

    def test_forged_signature_detected(self):
        app = _new_app()

        @app.spell
        def echo(x: str) -> str:
            return x

        app.invoke("echo", {"x": "a"})
        app.invoke("echo", {"x": "b"})

        # Build a new page that's hash-consistent but signed with wrong key.
        page = app.grimoire._pages[1]
        page["sig"] = "0" * 64
        v = app.grimoire.verify()
        self.assertFalse(v["ok"])
        self.assertEqual(v["reason"], "bad signature")

    def test_pii_redacted_before_hashing(self):
        """The on-chain page must hash redacted args, not raw PII."""
        app = _new_app()

        @app.spell
        def kyc(email: Email, aadhaar: Aadhaar) -> dict:
            return {"verified": True}

        app.invoke("kyc", {
            "email": "sensitive@private.com",
            "aadhaar": "987654321012",
        })
        # Recompute hash with RAW args — it must NOT match the stored hash.
        raw_hash = Grimoire._sha({
            "email": "sensitive@private.com",
            "aadhaar": "987654321012",
        })
        stored = app.grimoire.pages()[0]["args_hash"]
        self.assertNotEqual(raw_hash, stored)

    def test_head_advances(self):
        app = _new_app()

        @app.spell
        def echo(x: str) -> str:
            return x

        h0 = app.grimoire.head()
        app.invoke("echo", {"x": "a"})
        h1 = app.grimoire.head()
        app.invoke("echo", {"x": "b"})
        h2 = app.grimoire.head()
        self.assertNotEqual(h0, h1)
        self.assertNotEqual(h1, h2)


# ---------------------------------------------------------------------------
# 6. Auth & scopes
# ---------------------------------------------------------------------------
class AuthTests(unittest.TestCase):
    def test_token_required_when_auth_on(self):
        app = SHABD("a", secret="z" * 32, require_auth=True)

        @app.spell
        def secret_op() -> str:
            return "shh"

        with self.assertRaises(AuthError):
            app.invoke("secret_op", {})

    def test_token_grants_access(self):
        app = SHABD("a", secret="z" * 32, require_auth=True)

        @app.spell
        def secret_op() -> str:
            return "shh"

        tok = app.issue_token("alice", scopes=["*"])
        self.assertEqual(app.invoke("secret_op", {}, token=tok), "shh")


# ---------------------------------------------------------------------------
# 7. MCP tool surface (no real client; we just check the manifest format)
# ---------------------------------------------------------------------------
class MCPTests(unittest.TestCase):
    def test_manifest_contains_spells(self):
        app = _new_app()

        @app.spell
        def f(x: int) -> int:
            return x

        m = app.manifest()
        names = [s["name"] for s in m["spells"]]
        self.assertIn("f", names)


# ---------------------------------------------------------------------------
# 8. HTTP server — start, hit /health, /manifest, /spells, /grimoire
# ---------------------------------------------------------------------------
class HTTPTests(unittest.TestCase):
    PORT = 18765

    @classmethod
    def setUpClass(cls):
        cls.app = SHABD("http", secret="h" * 32, require_auth=False)

        @cls.app.spell
        def add(a: int, b: int) -> int:
            return a + b

        @cls.app.spell
        def kyc(email: Email) -> dict:
            return {"domain": str(email).split("@")[1]}

        cls.thread = threading.Thread(
            target=cls.app.serve,
            kwargs={"host": "127.0.0.1", "port": cls.PORT},
            daemon=True,
        )
        cls.thread.start()
        # Wait for server
        for _ in range(40):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{cls.PORT}/health", timeout=1)
                return
            except Exception:
                time.sleep(0.1)
        raise RuntimeError("server did not come up")

    def _get_json(self, path: str) -> dict:
        with urllib.request.urlopen(f"http://127.0.0.1:{self.PORT}{path}") as r:
            return json.loads(r.read())

    def _post_json(self, path: str, body: dict, expect: int = 200) -> dict:
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.PORT}{path}",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, expect)
            return json.loads(e.read())

    def test_health(self):
        self.assertEqual(self._get_json("/health")["status"], "ok")

    def test_manifest_lists_spells(self):
        m = self._get_json("/manifest")
        names = {s["name"] for s in m["spells"]}
        self.assertEqual(names, {"add", "kyc"})

    def test_spell_call_over_http(self):
        r = self._post_json("/spells/add", {"a": 4, "b": 5})
        self.assertEqual(r["result"], 9)

    def test_unknown_spell_returns_didyoumean(self):
        r = self._post_json("/spells/adddd", {"a": 1, "b": 2}, expect=404)
        self.assertIn("did_you_mean", r["error"])
        self.assertEqual(r["error"]["did_you_mean"], ["add"])

    def test_grimoire_endpoints(self):
        # Make at least one call so the chain is non-empty
        self._post_json("/spells/add", {"a": 1, "b": 1})
        v = self._get_json("/grimoire/verify")
        self.assertTrue(v["ok"])
        h = self._get_json("/grimoire/head")
        self.assertIn("head", h)
        p = self._get_json("/grimoire/pages?limit=10")
        self.assertIn("pages", p)


def main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for cls in (CoreTests, ValidationTests, AINativeErrorTests,
                SemanticTypeTests, GrimoireTests, AuthTests, MCPTests,
                HTTPTests):
        suite.addTests(loader.loadTestsFromTestCase(cls))
    runner = unittest.TextTestRunner(verbosity=2, stream=sys.stdout)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
