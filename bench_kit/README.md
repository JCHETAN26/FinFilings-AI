# Phase 1 real SEC benchmark

This command runs the real `pkg/index` writer, delta/PFOR compression, mmap reader, and BM25 search. It has no generated-corpus fallback, simulated indexing, mocked search, or precomputed timing results. `pkg/index` tests still use explicitly labeled deterministic fixtures for correctness.

## Reproduce

Run from the repository root with Go 1.24+ and Python 3. Set `SEC_USER_AGENT` to a research/project identifier and a contact email before downloading.

```bash
python3 -m unittest discover -s bench_kit -p 'test_*.py'
bash scripts/verify-phase1.sh
python3 bench_kit/prepare_sec.py
GOCACHE=/tmp/finfilings-go-cache go build -o /tmp/finfilings-phase1-bench ./bench_kit
/tmp/finfilings-phase1-bench -workers 50 -requests 1000000 -rounds 10 -open-runs 10
```

Preparation downloads four public SEC 10-K HTML documents, one serial request per second, and streams visible text into non-overlapping passages of at most 256 whitespace-delimited words. HTML visibility and feed-boundary behavior have a regression test. Hidden inline XBRL, script/style/head content, and explicitly hidden elements are excluded. Financial tables are retained as visible text. This is a simple passage extractor, not a semantic section parser: `SectionItem` explicitly says the item is unclassified. No documents are repeated to inflate the corpus.

Raw filings, the JSONL corpus, and download/checksum provenance live in ignored `data/sec/`. Filings are cached locally. A failed or suspicious download aborts preparation instead of falling back to fixture data. The benchmark itself has no network dependency once JSONL exists. `--corpus` accepts any conforming SECChunk JSONL; provenance is required to substantiate real-SEC claims. The saved run uses the checksummed corpus in [results/corpus-provenance.json](results/corpus-provenance.json).

## Measurements

- **Indexing throughput:** timer starts before `NewWriter` and ends after `Close`; includes streaming JSONL reads/decoding, bounded parallel analysis, PFOR compression, disk segment flushes, fsync, and manifest publication. Downloads and HTML extraction are excluded. Reports passages/s and **text MiB/s**, not compressed output throughput. The corpus is never held in a collection in RAM. Input records are capped at 4 MiB and queued records are bounded.
- **Validated mmap initialization:** repeated complete `index.Open` calls with `Close` between them. Includes manifest reads, `syscall.Mmap`, CRC checks, and segment validation. This is end-to-end open time, not isolated syscall latency. The files were just built, so the filesystem cache is warm. No privileged cache eviction is performed. Ten observations make the reported open p99 equal to the maximum; open p50 and every raw duration are also saved.
- **Query load:** exactly 50 persistent worker goroutines consume a channel of capacity 50. Index admission also allows 50 searches. There is a fixed 18-query financial workload, including one intentionally absent term; this is a hand-authored workload, not relevance judgments. Every measured result is checked against a serial baseline, including IDs, scores, and copied metadata, and must obey its declared hit/empty expectation. Every worker warms all queries before load.
- **Latency:** `search_latency` measures `Search`, including index admission. `dispatch_attempt_to_return_latency` additionally includes producer backpressure and the job queue. Validation, observation collection, and CSV writes are outside individual Search latency. Overall QPS includes their cost. Request context construction is outside Search latency but inside total run throughput. This is a saturated fixed-work/closed-loop test, not an open-loop arrival-rate model, HTTP benchmark, or SLO certification. Workers configured is distinct from simultaneous active API calls; actual peaks are recorded.
- **Raw evidence:** one CSV row per request, including failures. All latency observations are included in nearest-rank percentiles; there is no sampling. Two exact latency arrays use 16 bytes/request, capped at one million requests per round (about 15.3 MiB). Those are measurement buffers, not the document corpus. Collector queues are bounded. CSV storage can backpressure workers, and its cost is included in aggregate QPS. CSV files remain local and are gitignored; JSON summaries and provenance are reviewable.
- **Stability:** every request completes, without search errors, changed results, or unexpected hit/empty status. Failing checks cause a nonzero exit and a failed report. Memory snapshots report Go heap, GC allocation/pause counters, Linux process RSS when available, and goroutines. RSS includes Go, mmap pages, and benchmark buffers. This does not establish real-data relevance, sustained production availability, or multi-tenant isolation.

Each round builds a separate immutable on-disk index under `indexes/phase1-bench/`; this is repetition across independent benchmark trials, not duplicate passages within one index. Results have unique directories and include input/source hashes, environment, limits, raw open durations, and per-query request counts. Input hashes are rechecked after the run. CPU frequency and scheduling were not isolated on the desktop.

## Recorded run

See [RESULTS.md](RESULTS.md) for the measured ten-round run and its limitations. Phase 2 has not begun. No ranx/FAISS/k6 results are claimed by this Go-only baseline.

## Phase 2

The real BGE/PQ8/Go-RRF/MiniLM pipeline and `ranx` evaluation are implemented. See [Phase 2 results and reproduction](../docs/build/phase2.md). The Phase 1 benchmark and its recorded dataset/workload remain separate from the hybrid quality and CPU inference measurements.

## Phase 3 audit evaluation

`eval_agent.py` runs ten real-model multi-hop financial prompts. It checks immutable ticker/year/section constraints, two-retry bounds, exact SEC excerpt grounding, chunk IDs, accessions and inline links. Two prompts deliberately request unavailable evidence. Results report full coverage and gaps separately from structural/provenance pass status. Use `python3 scripts/verify-phase3.py` with the Phase 2 dependencies, artifacts and authenticated Claude CLI to keep both real retrieval services alive throughout the run. See [Phase 3 reproduction and limitations](../docs/build/phase3.md).

## Phase 4 — finite HTTP load and consolidated report

`load/main.go` uses a fixed-size Go worker pool (default 50) against the full hybrid `/search` and BGE/PQ8 `/dense` endpoints in separate scenarios. Each scenario warms up five requests, then records 200 attempts using the 100 genuine Phase 2 queries twice. Each request uses a new TCP connection; durations include network connection, queueing, full response and semantic shape validation. The Go pool, HTTP response sizes, query catalog, sample counts and client deadlines are bounded.

Run `python3 scripts/verify-phase4.py` with the documented Python inference dependencies and the `models/bin/finfilings` and `models/bin/loadtest` binaries built. The supervisor keeps real services alive, checks their corpus fingerprints, records terminal logs and one-second RSS samples, and shuts them down after measurement. It records the service admission configuration without bypassing its limits. The optimized Python service admits at most 64 active inference requests, dispatches one native job at a time, and uses an HTTP guard of 128; excess application work receives 429 with Retry-After. All failures remain in the results.

`python3 bench_kit/consolidate.py` independently checks every CSV observation, recomputes nearest-rank percentiles, checks success/failure/status counts, and writes the master [RESULTS.md](RESULTS.md). Both all-attempt and successful-only latency distributions and QPS are reported. The original Phase 1 report is preserved in [PHASE1_RESULTS.md](PHASE1_RESULTS.md). These finite closed-loop tests are not a production SLO or an open-loop capacity/soak benchmark.
