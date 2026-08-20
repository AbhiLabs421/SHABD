#!/usr/bin/env python3
"""
Local throughput / latency benchmark for SHABD.

What it does:
    1. Starts a SHABD server on an ephemeral port.
    2. Registers a trivial spell.
    3. Pounds it with concurrent requests via SHABDClient.
    4. Prints requests/sec and latency percentiles.

This is a *micro* benchmark — it measures SHABD's overhead, not the
spell body. For a realistic measurement, replace `noop` with your own
spell.

Usage:
    python bench/run.py                # 5000 calls, 50 workers
    BENCH_N=20000 BENCH_C=200 python bench/run.py
"""
from __future__ import annotations

import os
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shabd import SHABD  # noqa: E402
from shabd_client import SHABDClient  # noqa: E402

N = int(os.environ.get("BENCH_N", "5000"))
C = int(os.environ.get("BENCH_C", "50"))
PORT = int(os.environ.get("BENCH_PORT", "19191"))


def make_server() -> threading.Thread:
    app = SHABD("bench", secret="x" * 32, require_auth=False)

    @app.spell
    def noop() -> dict:
        return {"ok": True}

    @app.spell
    def echo(x: str) -> str:
        return x

    t = threading.Thread(
        target=app.serve,
        kwargs={"host": "127.0.0.1", "port": PORT,
                "shutdown_grace_s": 1.0},
        daemon=True,
    )
    t.start()
    return t


def wait_for_server(client: SHABDClient) -> None:
    for _ in range(50):
        try:
            if client.ready():
                return
        except Exception:
            time.sleep(0.1)
    raise RuntimeError("server did not come up")


def main() -> int:
    print(f"BENCH: {N} calls / {C} workers / SHABD :{PORT}")
    make_server()
    client = SHABDClient(f"http://127.0.0.1:{PORT}", retries=0, timeout=5.0)
    wait_for_server(client)

    latencies = []
    errors = 0
    t0 = time.time()

    def one() -> float:
        s = time.time()
        client.cast("noop", {})
        return (time.time() - s) * 1000

    with ThreadPoolExecutor(max_workers=C) as ex:
        futures = [ex.submit(one) for _ in range(N)]
        for fut in as_completed(futures):
            try:
                latencies.append(fut.result())
            except Exception:
                errors += 1

    elapsed = time.time() - t0
    latencies.sort()

    def pct(p: float) -> float:
        if not latencies:
            return float("nan")
        i = min(len(latencies) - 1, int(len(latencies) * p))
        return latencies[i]

    print("\nResults:")
    print(f"  total      : {len(latencies)} ok, {errors} errors in {elapsed:.2f}s")
    print(f"  throughput : {len(latencies) / elapsed:,.0f} req/s")
    print(f"  latency p50: {pct(0.50):.2f} ms")
    print(f"  latency p95: {pct(0.95):.2f} ms")
    print(f"  latency p99: {pct(0.99):.2f} ms")
    print(f"  latency max: {max(latencies):.2f} ms" if latencies else "")
    print(f"  latency avg: {statistics.fmean(latencies):.2f} ms"
          if latencies else "")
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
