"""Tests for shabd_config — the single production control surface.

Easy    : mini-YAML parser, scalar typing, secret resolution.
Medium  : ProductionConfig builds identity/cache providers from config.
Hard    : builtin vs external selection; the shipped config.example.yaml
          parses identically under the mini-parser and PyYAML.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shabd_config import (  # noqa: E402
    ConfigError,
    ProductionConfig,
    _mini_yaml,
    load_config,
    resolve_secret,
)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SAMPLE = """
shabd:
  name: test-plane
  secret_source:
    provider: inline
    value: 00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff
identity:
  provider: builtin
  builtin:
    issuer: https://x.internal
    token_alg: HS256
    password_policy:
      min_len: 6
cache:
  provider: smriti
  smriti:
    host: 127.0.0.1
    port: 6390
server:
  cors:
    allow_origins: []
  security_headers: true
"""


class MiniYamlTests(unittest.TestCase):
    def test_nested_maps(self):
        d = _mini_yaml(SAMPLE)
        self.assertEqual(d["shabd"]["name"], "test-plane")
        self.assertEqual(d["identity"]["builtin"]["issuer"], "https://x.internal")
        self.assertEqual(d["identity"]["builtin"]["password_policy"]["min_len"], 6)

    def test_scalar_types(self):
        d = _mini_yaml("a: 5\nb: 1.5\nc: true\nd: false\ne: null\nf: hello\n")
        self.assertEqual(d["a"], 5)
        self.assertEqual(d["b"], 1.5)
        self.assertIs(d["c"], True)
        self.assertIs(d["d"], False)
        self.assertIsNone(d["e"])
        self.assertEqual(d["f"], "hello")

    def test_inline_empty_list(self):
        d = _mini_yaml("server:\n  cors:\n    allow_origins: []\n")
        self.assertEqual(d["server"]["cors"]["allow_origins"], [])

    def test_block_list(self):
        d = _mini_yaml("roles:\n  - admin\n  - user\n  - auditor\n")
        self.assertEqual(d["roles"], ["admin", "user", "auditor"])

    def test_comments_ignored(self):
        d = _mini_yaml("# heading\na: 1  # trailing\nb: keep # x\n")
        self.assertEqual(d["a"], 1)
        self.assertEqual(d["b"], "keep")

    def test_hash_inside_quotes_preserved(self):
        d = _mini_yaml('a: "v#1"\n')
        self.assertEqual(d["a"], "v#1")


class SecretTests(unittest.TestCase):
    def test_env_secret(self):
        os.environ["TEST_SECRET_X"] = "s" * 40
        b = resolve_secret({"provider": "env", "key": "TEST_SECRET_X"})
        self.assertEqual(b, b"s" * 40)

    def test_missing_env_raises(self):
        os.environ.pop("NOPE_SECRET", None)
        with self.assertRaises(ConfigError):
            resolve_secret({"provider": "env", "key": "NOPE_SECRET"})

    def test_hex_decoded(self):
        b = resolve_secret({"provider": "inline", "value": "00ff" * 8})
        self.assertEqual(len(b), 16)  # 32 hex chars -> 16 bytes

    def test_file_secret(self):
        path = os.path.join(tempfile.mkdtemp(), "secret.key")
        with open(path, "w") as fh:
            fh.write("plainsecretvalue1234567890")
        self.assertEqual(resolve_secret({"provider": "file", "path": path}),
                         b"plainsecretvalue1234567890")


class ProductionConfigTests(unittest.TestCase):
    def setUp(self):
        self.pc = ProductionConfig(_mini_yaml(SAMPLE))

    def test_secret_from_inline_hex(self):
        self.assertEqual(len(self.pc.secret()), 32)

    def test_summary(self):
        s = self.pc.summary()
        self.assertEqual(s["identity"], "builtin")
        self.assertEqual(s["cache"], "smriti")

    def test_build_identity_builtin(self):
        prov = self.pc.build_identity()
        prov.praman.create_user("zed", "secret1")
        r = prov.password_login("zed", "secret1")
        self.assertEqual(prov.verify(r["access_token"])["sub"], "zed")

    def test_build_cache_returns_smriti_plugin(self):
        cache = self.pc.build_cache()
        self.assertEqual(cache.__class__.__name__, "SmritiCache")

    def test_keycloak_selection(self):
        pc = ProductionConfig({"identity": {
            "provider": "keycloak",
            "keycloak": {"server_url": "https://kc", "realm": "r",
                         "client_id": "c"}},
            "shabd": {"secret_source": {"provider": "inline",
                                        "value": "x" * 32}}})
        prov = pc.build_identity()
        self.assertEqual(prov.__class__.__name__, "ExternalKeycloakProvider")


class ExampleConfigTests(unittest.TestCase):
    def test_example_parses_and_matches_pyyaml(self):
        path = os.path.join(REPO, "config.example.yaml")
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        mini = _mini_yaml(text)
        # structural sanity
        self.assertEqual(mini["identity"]["provider"], "builtin")
        self.assertEqual(mini["cache"]["smriti"]["port"], 6390)
        self.assertEqual(mini["server"]["cors"]["allow_origins"], [])
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML not installed")
        ref = yaml.safe_load(text)
        # the keys we rely on match between both parsers
        self.assertEqual(mini["identity"]["builtin"]["token_alg"],
                         ref["identity"]["builtin"]["token_alg"])
        self.assertEqual(mini["cache"]["provider"], ref["cache"]["provider"])
        self.assertEqual(mini["persistence"]["provider"],
                         ref["persistence"]["provider"])

    def test_load_config_uses_pyyaml_when_present(self):
        path = os.path.join(REPO, "config.example.yaml")
        cfg = load_config(path)
        self.assertEqual(cfg["shabd"]["name"], "ccil-control-plane")


class ServerRunnerTests(unittest.TestCase):
    def setUp(self):
        self.pc = ProductionConfig(_mini_yaml(SAMPLE))

    def test_identity_server_built_for_builtin(self):
        srv = self.pc.identity_server(port=0)
        self.assertEqual(srv.__class__.__name__, "PramanServer")

    def test_cache_server_built_for_smriti(self):
        srv = self.pc.cache_server(port=0)
        self.assertEqual(srv.__class__.__name__, "SmritiServer")

    def test_no_identity_server_for_keycloak(self):
        pc = ProductionConfig({
            "identity": {"provider": "keycloak",
                         "keycloak": {"server_url": "x", "realm": "r",
                                      "client_id": "c"}},
            "shabd": {"secret_source": {"provider": "inline",
                                        "value": "x" * 32}}})
        self.assertIsNone(pc.identity_server())

    def test_identity_server_actually_serves(self):
        import json as _json
        import urllib.request
        srv = self.pc.identity_server(port=0).start_background()
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{srv.port}/healthz", timeout=3) as r:
                body = _json.loads(r.read().decode())
            self.assertEqual(body["service"], "praman")
        finally:
            srv.shutdown()


def main() -> int:
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    ok = unittest.TextTestRunner(verbosity=2, stream=sys.stdout).run(suite)
    return 0 if ok.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
