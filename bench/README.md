# Benchmark

A small benchmark that measures SHABD's *overhead* on a no-op spell. It
is not a model of any particular real workload — replace the `noop`
spell with your own to measure something realistic.

## Run it

```bash
python bench/run.py                # default: 5,000 calls / 50 workers
BENCH_N=20000 BENCH_C=200 python bench/run.py
```

## Sample numbers (laptop, Python 3.12)

| Metric          | Value           |
|-----------------|-----------------|
| Throughput      | ~4,000–6,000 req/s |
| p50 latency     | ~5 ms            |
| p95 latency     | ~15 ms           |

These are bytes-on-the-wire numbers via `urllib.request`, single-process.
Real workloads:

* Add `max_concurrent` per spell to protect downstreams.
* Run behind nginx / Envoy with HTTP keep-alive (huge throughput win).
* For very high QPS, run multiple SHABD processes behind a reverse
  proxy — the audit chain stays per-process, so plan log shipping.

## What to take away

SHABD is not a perf-first server — it's a *production-shaped* one. Its
job is to add auth, validation, rate limiting, an audit chain, traces,
and metrics around your spell body. The overhead above is the price for
that. If your spell body takes longer than ~5 ms (most do), SHABD's
overhead is in the noise.
