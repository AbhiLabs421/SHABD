"""
run_all.py — dev launcher for all SHABD microservices in one terminal.

    python backend/run_all.py

Starts users(8003), spells(8001), notary(8002), agent(8004) and the
gateway(8000) as separate child processes. Ctrl+C stops them all.
In production you'd run each with its own `uvicorn ... --workers N` and a
process manager; this is just for local development.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVICES = [
    ("users", "services/users_service/main.py"),
    ("spells", "services/spells_service/main.py"),
    ("notary", "services/notary_service/main.py"),
    ("agent", "services/agent_service/main.py"),
    ("gateway", "services/gateway/main.py"),
]

procs: list[tuple[str, subprocess.Popen]] = []


def _stop(*_a):
    for name, p in procs:
        if p.poll() is None:
            p.terminate()
    time.sleep(1)
    for name, p in procs:
        if p.poll() is None:
            p.kill()
    sys.exit(0)


def main() -> None:
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    env = os.environ.copy()
    for name, rel in SERVICES:
        p = subprocess.Popen([sys.executable, str(HERE / rel)], env=env)
        procs.append((name, p))
        print(f"  started {name:8s} pid={p.pid}")
        time.sleep(0.8)  # let ports bind in order (gateway last)
    print("\n  All services up. Gateway: http://127.0.0.1:8000/api/health")
    print("  Ctrl+C to stop everything.\n")
    while True:
        time.sleep(2)
        for name, p in procs:
            if p.poll() is not None:
                print(f"  ⚠  {name} exited with code {p.returncode}")


if __name__ == "__main__":
    main()
