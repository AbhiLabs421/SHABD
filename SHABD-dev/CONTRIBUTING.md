# Contributing to SHABD

Thanks for your interest in improving SHABD! This project aims to stay small, readable, and dependency-free, so contributions are reviewed with those principles in mind.

## Core principles

Please keep these in mind before opening a pull request:

1. **Single file.** The entire framework lives in `shabd.py`. New features should fit this model rather than splitting into multiple modules.
2. **Zero required dependencies.** The standard library only. Anything extra must be an *optional* plugin that fails gracefully when the package is absent (see `RedisPlugin` for the pattern).
3. **Readable over clever.** Someone should be able to read the relevant section and understand it without external docs.
4. **Backward compatible.** Both `SHABD` and `Conjure` names must keep working. Don't break existing decorators or method signatures.

## How to contribute

1. Fork the repository and create a branch for your change.
2. Set up local dev:
   ```bash
   make install              # installs ruff + mypy
   pre-commit install        # optional, runs ruff on every commit
   ```
3. Make your change in `shabd.py` (and add a test under `tests/` and an
   example under `examples/` if relevant).
4. Run the full test suite:
   ```bash
   make test                 # 49 stdlib-only tests
   make lint                 # ruff
   make comparison           # live SHABD-vs-FastMCP matrix (optional)
   ```
5. Open a pull request describing what you changed and why. The PR
   template will guide you through the checklist.

## Local dev cheat-sheet

| Command         | What it does                                       |
|-----------------|----------------------------------------------------|
| `make install`  | Install dev tools (ruff, mypy, build, twine)       |
| `make test`     | Run all 49 tests (no extras)                       |
| `make lint`     | Run ruff                                           |
| `make demo`     | Launch the demo server at http://localhost:8765   |
| `make docker`   | Build the production container                     |
| `make compose`  | SHABD + Prometheus + Grafana                       |
| `make bench`    | Local throughput benchmark                         |
| `make publish`  | Tag + build + upload to PyPI                       |

## Reporting security issues

Please **don't** open a public GitHub issue for security problems —
email **ipsabhi423@gmail.com** with the subject `SECURITY: SHABD`. See
[SECURITY.md](SECURITY.md) for the full process and threat model.

## Reporting bugs

Open an issue with:

- What you expected to happen
- What actually happened
- A minimal code snippet that reproduces the problem
- Your Python version and operating system

## Feature requests

Open an issue describing the use case. Because SHABD is deliberately small, not every feature will be accepted into core — but well-scoped, broadly useful additions are welcome.

## Questions

Email **ipsabhi423@gmail.com** or open a discussion on GitHub.
