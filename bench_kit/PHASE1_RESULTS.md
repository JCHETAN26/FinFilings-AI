# Real SEC Phase 1 benchmark results

Measured on 2026-10-30 on the connected Linux host: AMD Ryzen 7 5800H with Radeon Graphics, 16 logical CPUs, `go1.27.1`, GOMAXPROCS=16. Non-isolated desktop; warm filesystem cache.

Corpus: **702 non-overlapping passages from four actual SEC 10-K filings**, 182,667 indexed tokens, three immutable mmap segments totaling 796,880 bytes per index. The four source filings are AAPL (filed 2023), MSFT (2024), NVDA (2024), and AMZN (2024, fiscal 2023). Text throughput counts passage Text bytes, excluding JSON metadata. No generated documents or duplicated passages were indexed.

**Observed stability: all 10,000,000 searches completed with zero search errors, result mismatches, or unexpected hit/empty responses.** Every round used 50 persistent query workers and 50 index admission slots, with an 18-query workload and top-k=10. Observed peaks were 30–47 simultaneous Search API calls; configured workers does not mean 50 calls stayed active continuously on this small corpus. Post-load goroutines returned to 1 in every round.

| Round | Indexing passages/s | Validated mmap Open p50 (ms) | Completed queries/s | Search p50 (ms) | Search p99 (ms) | Errors / mismatches |
|---|---:|---:|---:|---:|---:|---:|
| 1 | 13,149 | 0.257 | 853,124 | 0.012 | 0.047 | 0 / 0 |
| 2 | 21,428 | 0.256 | 843,316 | 0.012 | 0.047 | 0 / 0 |
| 3 | 11,614 | 0.292 | 839,721 | 0.012 | 0.047 | 0 / 0 |
| 4 | 21,568 | 0.251 | 840,911 | 0.012 | 0.047 | 0 / 0 |
| 5 | 21,913 | 0.254 | 840,735 | 0.012 | 0.047 | 0 / 0 |
| 6 | 20,337 | 0.251 | 823,054 | 0.012 | 0.048 | 0 / 0 |
| 7 | 20,424 | 0.253 | 701,627 | 0.012 | 0.048 | 0 / 0 |
| 8 | 18,422 | 0.260 | 853,663 | 0.012 | 0.048 | 0 / 0 |
| 9 | 20,367 | 0.255 | 779,662 | 0.012 | 0.048 | 0 / 0 |
| 10 | 17,385 | 0.265 | 756,405 | 0.012 | 0.048 | 0 / 0 |

Across rounds:

- Indexing median: **20,352 passages/s**; range 11,614–21,913. Each timer includes input decoding, analysis, compression, flushes and durable publication. Independent builds took 32.0–60.4 ms.
- Validated mmap initialization: round medians **0.251–0.292 ms**, worst individual Open **0.751 ms**. This includes CRC and structural validation, rather than only syscall time. Ten opens per round; raw durations are saved.
- Search latency: p50 **0.012 ms**, p95 **0.039–0.040 ms**, p99 **0.047–0.048 ms**. Worst individual search: **5.56 ms**. These exclude dispatcher queue wait and observation writing, but include index admission.
- Dispatcher-attempt-to-return p99: **0.386–0.567 ms**, including producer backpressure and queue wait. End-to-end throughput, including result validation and observation overhead: **810,125 completed searches/s** weighted across the ten measured rounds.
- Post-load process RSS: **42.0–45.0 MiB**. This includes benchmark latency arrays, runtime, and mapped pages. Per-round GC/heap/allocation snapshots are in the JSON. No production memory-capacity estimate is inferred from four filings.
- Total timed query-load duration: **12.34 seconds** across the ten rounds. This is a finite stability check, not a long-running soak or production availability test. The corpus fits in cache, and the workload is not an HTTP/open-loop load test. No production latency SLO was specified or certified.

## Evidence and reproduction

- [JSON report](results/phase1-3051556664/report.json): all limits, per-query counts, input/source hashes, raw Open durations, memory/GC data, observed concurrency, and latency summaries.
- [Console output](results/sec50-console.txt) and [source corpus provenance](results/corpus-provenance.json).
- [CSV integrity check](results/csv-integrity.txt): independently reread all one million first-round records, checked unique IDs/statuses and recomputed the exact p99 against the JSON.
- Raw traces remain locally at `bench_kit/results/phase1-3051556664/requests-1.csv` through `requests-10.csv`, with one row for every request. These large traces are gitignored.
- [Source audit](results/validation/phase1-audit.txt), [Go verification](results/validation/go-checks.txt), and [HTML extraction tests](results/validation/preparation-tests.txt). Unit, race, scalar-only tests, and go vet passed before this timed run. No race-instrumented timings are reported.
- The recorded source fingerprint matches the current Go/assembly/Python files, checked after the run. Corpus SHA-256: `806cf2fc59f4ed352541b0f12ebbd2eba880d1ed44dc65475f7026556b8012ad`.

Run the commands in [README.md](README.md). The command for this result was:

```bash
/tmp/finfilings-phase1-bench -workers 50 -requests 1000000 -rounds 10 -open-runs 10
```

No Phase 2 vector, embedding, RRF, or agent code has started. This evidence validates the implemented sparse-index path on the recorded corpus and workload; it does not establish relevance quality, large-corpus performance, or tenant isolation.
