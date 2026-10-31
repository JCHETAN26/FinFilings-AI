# Phase 1: compressed inverted index (BM25) for SEC filing chunks

Scope: **Phase 1 only.** This is a disk-backed, memory-mapped sparse inverted index with BM25 retrieval over SEC 10-K/10-Q chunks. It has no UI, no vector index, and no agent code. **It is not an end-to-end RAG system**: search returns chunk metadata, not passage text (see [Passage text](#passage-text-and-rag)).

Module: `github.com/JCHETAN26/FinFillings-AI` (`go.mod`: `go 1.24`; built and tested with go1.27.1).

Raw evidence (copied verbatim, with environment details and caveats):
- [`phase1-validation-raw.txt`](phase1-validation-raw.txt): fmt, vet (4 configs), test, race, purego, stress, fuzz, verbose test list
- [`phase1-bench-raw.txt`](phase1-bench-raw.txt): `-benchmem -count 5` output for every benchmark
- [`phase1-simd-objdump.txt`](phase1-simd-objdump.txt): disassembly of all four SSE2 kernels

> **Fixture caveat.** All index-level correctness, compression, and timing data comes from **1,000 deterministic synthetic test-fixture chunks**: 61–206 tokens each, drawn from a ~720-word Zipf(1.1) vocabulary, with planted marker and topic terms. Nothing has been measured on real EDGAR filings. None of these numbers describe SEC retrieval quality, production latency, or corpus-scale behavior.

## Files

| File | Contents |
|---|---|
| `pkg/model/chunk.go` | `SECChunk` (ID, Ticker, CIK, FormType, FilingYear, SectionItem, Text, Tokens), `ChunkMeta`, `FormType`, `Validate`; documents the Tokens and storage contract |
| `pkg/index/errors.go` | Sentinel errors as **typed constants** (no package-level variables) |
| `pkg/index/compress.go` | Delta and strict-delta coding (`next` validated before arithmetic); PFOR block codec with real exceptions; scalar bit packing |
| `pkg/index/bitpack.go` | `packBits`/`unpackBits` dispatch: SIMD prefix + scalar tail |
| `pkg/index/bitpack_amd64.{go,s}` | **SSE2 assembly kernels for widths 8 and 16** (`amd64 && !purego`) |
| `pkg/index/bitpack_generic.go` | No-SIMD build (`!amd64 \|\| purego`) |
| `pkg/index/postings.go` | Posting-list encoding and block iterator |
| `pkg/index/tokenize.go` | Allocation-free analyzer shared by ingest and query |
| `pkg/index/segment.go` | Segment file format, streaming writer, validating parser |
| `pkg/index/manifest.go` | Manifest: the atomic publish point |
| `pkg/index/mmap.go`, `mmap_other.go` | `syscall.Mmap` loading and ownership rules; stub elsewhere |
| `pkg/index/lock_unix.go`, `lock_other.go` | Exclusive directory writer lock (`flock`; `O_EXCL` fallback) |
| `pkg/index/writer.go` | `Writer`: streaming ingest, bounded pool, per-doc analyzer budget, buffer limit, lock lifecycle, `CleanAbandoned` |
| `pkg/index/bm25.go` | BM25, pooled scratch, bounded top-k heap |
| `pkg/index/index.go` | `Index`: `Open`, `Search` (admission slots), `Stats`, `Close` |
| `pkg/index/*_test.go` | Unit, retrieval, corruption, race, admission, lock, SIMD, fuzz tests and benchmarks |

`pkg/index/memory_profile_test.go` (an opt-in fixture memory diagnostic) and `scripts/verify-phase1.sh` exist in the tree but were not part of this change set.

## API

```go
w, err := index.NewWriter(dir, index.WriterOptions{MaxBufferBytes: 64 << 20}) // takes dir lock
err = w.Ingest(ctx, chunks)    // chunks <-chan model.SECChunk; or w.Add(ctx, c)
err = w.Close()                // flush + publish MANIFEST atomically; releases lock

ix, err := index.Open(dir, index.Options{MaxConcurrentSearches: 8})
hits, err := ix.Search(ctx, "goodwill impairment", 10) // []Hit{Score, Chunk ChunkMeta}
err = ix.Close()

removed, err := index.CleanAbandoned(dir) // after a crash; see Crash recovery
```

## Design

### Ingestion and memory bounds

- **Bounded pipeline.** `Ingest` runs a dispatcher, `Workers` analysis goroutines, and an in-order committer. They are connected by bounded channels: jobs have capacity `QueueDepth`, and results and the in-flight semaphore have capacity `Workers+QueueDepth`. Commits follow input order, so parallel ingest is byte-identical to sequential `Add`; `TestIngestIsDeterministic` checks this. The corpus is never retained.
- **Per-document analyzer budget (review item 1).**
  - Each analyzer receives the configured `MaxBufferBytes` as its budget.
  - Every *new distinct term* is charged **before** it is inserted. The charge is `analyzerTermBytes + 2·len(term)` = 248 B + 2·len, which covers the analysis side (analyzer map entry estimate, string header and tf slot with 2× append slack) and the commit side (`termOverheadBytes` plus the initial posting arrays). Stored-field bytes are charged too.
  - When the next term would exceed the budget, analysis stops at once with `ErrDocTooLarge`. Memory therefore stays bounded by the budget even for a pathological document.
  - Measured on the regression input: a 200,000-distinct-term document under a 1 MiB budget is rejected after materializing **3,980** terms (`TestAnalyzerRejectsManyDistinctTermsEarly`).
  - Repeated tokens cost nothing extra: a 400,000-token document with 2 distinct terms passes a 16 KiB budget.
  - The charge is provably ≥ the commit cost of the same document (`TestAnalyzerChargeCoversCommitCost`). Any document that passes analysis therefore fits an empty segment buffer.
- **Oversized pooled storage is discarded.**
  - An analyzer whose map grew past 16,384 entries gets a fresh map, because Go maps do not shrink on `clear`.
  - Pooled `analyzedDoc`s with term arrays above 16,384 entries, or stored buffers above 4 KiB, are dropped instead of being reused (`TestPooledStorageIsBounded`).
- **Segment buffer.**
  - Posting arrays are grown explicitly, so their bytes are accounted exactly.
  - Per-term map overhead is a 112 B estimate, plus the term bytes.
  - Before each commit, if the document would push the buffer past `MaxBufferBytes` or `MaxDocsPerSegment`, the buffer is flushed to a new segment first.
  - **Not accounted:** Go map internals, `append` slack in per-document arrays, replaced arrays awaiting GC, and flush scratch (about 48 B per term of the largest flushed segment).
- **Sticky errors.** After any Add/Ingest/Flush error, including cancellation, later calls return `ErrWriterFailed`.

### Writer lock and crash recovery (review item 3)

- `NewWriter` creates the directory if needed, then takes an **exclusive, non-blocking `flock(LOCK_EX|LOCK_NB)`** on `.writer.lock`. The lock is atomic, and because `flock` belongs to the open file description, two Writers in the **same process** also exclude each other. A second writer gets `ErrWriterLocked`; 16 goroutines racing `NewWriter` produce exactly one winner (tested).
- Only while holding the lock does `NewWriter` check the directory:
  - a published `MANIFEST` gives `ErrIndexExists`;
  - any `seg-*.ffseg`, `.seg-*.tmp` or `.manifest-*.tmp` gives **`ErrAbandonedBuild`**.

  Existing segment files are **never overwritten**. Each flush also re-checks its target name with `Lstat` before renaming (`TestFlushNeverOverwritesSegment`). On any `NewWriter` error the lock is released.
- **Release.** The lock is released by `Close` (success or failure) and by `Abort`.
- **Close after a failure.** Close removes the Writer's own unpublished segments **before** releasing the lock. If it left them for a later `Abort`, that `Abort` could delete files with the same names created by a newer writer that took the lock in the meantime. `Abort` after `Close` is a no-op.
- **Manifest edge case.** If the manifest rename succeeded but the directory fsync failed, the index is published and its segments are kept.
- A Writer that is never closed or aborted holds the lock until process exit.

**Crash recovery:**
- **Process dies before publish.** The kernel drops the `flock` automatically, so no stale-lock handling is needed. Segment and temp files remain without a manifest, and the next `NewWriter` fails with `ErrAbandonedBuild` (with a file count and example name). Recovery: call `CleanAbandoned(dir)`. It takes the lock, refuses if a manifest exists or a writer is active, removes only the files listed above, and fsyncs the directory. Then rebuild. `TestCrashLeftoversAreRefusedThenCleaned` simulates this by dropping the lock without cleanup.
- **Process dies after the manifest rename.** The index is complete. Segments and the manifest are fsynced before rename; the directory fsync follows the rename, so a crash in that window may lose the manifest on some filesystems. That case then looks like an abandoned build.
- **Platforms without flock** (the `lock_other.go` fallback, which is untested on this machine). The lock is an `O_EXCL` file that persists after a crash. It must be removed manually after confirming no writer is running.
- **NFS and other network filesystems.** `flock` semantics there are filesystem-dependent and not supported.
- The `.writer.lock` file stays on disk after release. Its presence alone means nothing, and `Open` ignores it.

### Compression and SIMD (review items 4, 5)

- Doc IDs use `DeltaEncodeStrict`, which stores gap − 1 continuing across blocks. Term frequencies are stored as tf − 1.
- **Strict delta overflow (item 5).** `next` must be ≤ `MaxStrictNext` = 2³², and anything larger is rejected with `ErrInvalidArgument` **before any arithmetic**. With next ≤ 2³² and gap < 2³², `next+gap` cannot wrap a uint64. next = 2³² admits only empty input. Regression tests cover `MaxUint64`, `MaxUint64−1`, `2³²+1`, `2⁴⁰`, the 2³² boundary, and the largest valid sums.
- **PFOR block:** `[n][width][exceptions] packed-low-bits {pos, uvarint(high)}*`. The encoder picks the exact-cost-minimizing width, and values that need more bits become patched exceptions. The decoder validates every field and never reads out of bounds.
- **Bit-packing implementations: exactly what is vectorized.**

  | Build | Width 8 | Width 16 | All other widths (0–7, 9–15, 17–32) |
  |---|---|---|---|
  | `GOARCH=amd64`, no `purego` tag | **SSE2** for ⌊n/16⌋·16 values, scalar tail | **SSE2** for ⌊n/8⌋·8 values, scalar tail | scalar |
  | `-tags purego`, or any non-amd64 GOARCH (arm64, …) | scalar | scalar | scalar |

  - SSE2 is in the amd64 baseline (GOAMD64=v1), so there is no runtime CPU detection and no dependency.
  - **Pack 8:** `PAND` mask, then `PACKSSDW`, then `PACKUSWB`; 16 values per iteration.
  - **Pack 16:** `PSLLD`/`PSRAD` 16 to sign-extend, then `PACKSSDW`; 8 values per iteration.
  - **Unpack 8:** `PUNPCKLBW`/`PUNPCKHBW` and `PUNPCKLWD`/`PUNPCKHWD` with a zero register; 16 values per iteration.
  - **Unpack 16:** `PUNPCKLWD`/`PUNPCKHWD`; 8 values per iteration.
  - All loads and stores are unaligned `MOVDQU`.
  - Both `AppendPFOR` (packing) and `DecodePFOR` (unpacking) go through the dispatcher. Widths 8 and 16 are byte multiples, so the scalar tail always starts on a byte boundary and the output is byte-identical to scalar.
  - There are **no** AVX2/AVX-512/NEON kernels, and no SIMD for the delta, exception, or BM25 stages.
- **Verification:**
  - `TestBitPackDispatchMatchesReference` compares against an **independent bit-at-a-time reference** for every width 0–32 × every length 0–300. Inputs have bits above the width set, misaligned source and destination, and a non-empty dst prefix.
  - `TestSSE2KernelsDirect` calls each kernel directly.
  - `TestPFORUsesByteAlignedWidths` confirms PFOR actually selects widths 8 and 16 (with and without exceptions).
  - `TestSSE2KernelsDisassembly` builds an unstripped test binary, runs `go tool objdump`, and requires the SSE2 instructions above in each kernel.
  - The whole suite also passes with `-tags purego` (scalar only).

### Segment format (v1, little endian)

```
"FFSEGv01" | postings | termBytes | termEntries (32 B each) | docLens (u32) |
stored (5 uvarint-len strings + uvarint year per doc) | storedIdx (u64 × docs+1) |
footer 136 B: version, flags, docCount, termCount, totalTokens, 6×(off,len),
              CRC-32C(all preceding), reserved, "FFSEGEND"
```

**Open-time validation** covers:
- magic numbers, CRC-32C, version, flags and reserved fields;
- section contiguity and sizes;
- strictly sorted terms, and df ∈ [1, docCount];
- contiguous posting ranges that cover the postings section exactly;
- Σ docLens = totalTokens;
- monotonic stored offsets.

Posting payloads are validated lazily during decode and return `ErrCorrupt`. Segments are written to a temp file, fsynced, and renamed. The manifest (text with CRC-32C) is written by temp file, fsync, rename, then directory fsync.

### mmap ownership

- Each mapping (`PROT_READ`, `MAP_SHARED`; the fd is closed right after mapping) is owned by one segment of one `Index`.
- Mapped slices never escape the package. `Hit` strings are copied and stay valid after `Close`.
- Segments are immutable. External truncation causes SIGBUS, which is outside the contract.

### Search, admission, and Close (review item 2)

- **Admission.** `Options.MaxConcurrentSearches` (default `GOMAXPROCS`, max 4096) sizes a buffered-channel semaphore.
  - `Search` takes a slot before doing anything else. While queued it returns `ctx.Err()` if its context ends, or `ErrClosed` if `Close` is called.
  - At most N searches hold a scoring scratch at once. Peak scoring memory is about N × (12 B × docs in the largest segment) + O(query terms × segments). Idle scratches cached by the `sync.Pool` are released by GC.
- **Close protocol.**
  1. Close closes `done`, once. This wakes every queued caller, which returns `ErrClosed` without touching segments.
  2. Close takes the write lock. Admitted searches hold the read lock for their whole duration, so Close waits for them.
  3. Close unmaps and marks the index closed.

  Queued callers never hold the lock and Close never waits on a slot, so they cannot deadlock. Close is idempotent.
- **Tests:**
  - exactly 3 admitted at peak among 24 callers with 3 slots;
  - deadline and cancellation while queued, with no slot leak;
  - Close wakes 8 queued callers while every slot is held;
  - Close under mixed admitted and queued load (2 slots, 16 goroutines);
  - the original Close-during-search test.

  All pass 10–20× under `-race`.
- **Scoring.**
  - Global IDF and avgdl across segments, so results are identical for 1, 11 and 100 segments.
  - Term-at-a-time float64 accumulator, reset in O(touched) and restored on error paths.
  - Bounded min-heap top-k with `k ≤ MaxTopK` (default 1000).
  - Deterministic ties by (segment, doc).

### Tokens semantics (review item 6)

`SECChunk.Tokens`, when non-empty, **replaces `Text` as the analyzer input**, and `Text` is then ignored. Tokens are **not** indexed verbatim: each element goes through the same analyzer as queries, so one element can produce zero, one or several terms. For example, `"10-K"` becomes `10`, `k`, and `"---"` produces nothing. Verbatim terms would be unreachable, because queries are always analyzed. `TestTokensAreAnalyzerNormalized` pins this: the indexed terms are exactly `10,7a,item,k,revenue`, and queries `"10-K"`, `"item 7a"` and `"REVENUE"` match.

### Passage text and RAG

`Text` and `Tokens` are **not stored**. `Search` returns `ChunkMeta` only: ID, ticker, CIK, form type, year and section. To use this index for RAG, a caller needs a **separate, disk-backed passage store keyed by chunk ID**. That store is not part of Phase 1, so this build does **not** provide end-to-end RAG.

## Reproducible commands

```sh
gofmt -l .
go vet ./... && go vet -tags purego ./... && GOARCH=arm64 go vet ./... && GOOS=windows go vet ./...
go test -count=1 ./...
go test -count=1 -race ./...
go test -count=1 -tags purego ./...
go test -race -count=10 -run 'Admission|Queued|Close|Concurrent|Competing|Crash|FailedClose|Ingest' ./pkg/index/
go test -run '^$' -bench . -benchmem -benchtime 1s -count 5 ./pkg/index/
go test -c -o /tmp/ffindex-final.test ./pkg/index/ && go tool objdump -s 'index\.(un)?pack(8|16)SSE2' /tmp/ffindex-final.test
go test -run '^$' -fuzz '^FuzzDecodePFOR$'    -fuzztime 15s ./pkg/index/   # likewise FuzzPFORRoundtrip, FuzzParseSegment
```

## Measured results (2026-10-30)

Environment: go1.27.1 linux/amd64, GOAMD64=v1, AMD Ryzen 7 5800H (16 logical CPUs), non-isolated desktop. Full raw output is in the files linked above.

**Validation:**
- `gofmt` clean.
- `go vet` clean on amd64, amd64+purego, arm64 and windows.
- `go test`, `go test -race` and `go test -tags purego` all pass: 58 top-level tests pass, plus 1 opt-in diagnostic that is skipped by default.
- The concurrency stress run (`-race -count=10`) passes.
- Fuzzing (15 s per target on the final code) found no failures: 3.10M, 2.93M and 1.56M executions.

**Fixture results (synthetic, not SEC):**
- Compression: 63,082 postings in 58,489 B, against a raw baseline of 504,656 B (u32 doc + u32 tf). That is ratio **0.116**, or 7.42 bits/posting.
- Retrieval checks:
  - all 1,000 marker queries return exactly their chunk;
  - rankings and scores match a brute-force BM25 oracle within 1e-9 for 1-, 11- and 100-segment indexes;
  - the planted-topic query returns exactly the 100 planted documents.

**SIMD vs scalar on matched input** (128 values, ns/op, min–max of 5 runs, 0 allocs):

| Operation | Width | SSE2 (dispatch) | Scalar | Speedup (median) |
|---|---|---|---|---|
| pack | 8 | 15.61–15.77 | 155.9–163.1 | ≈10.1× |
| unpack | 8 | 14.56–15.10 | 153.8–154.5 | ≈10.4× |
| pack | 16 | 17.67–18.01 | 217.6–226.5 | ≈12.5× |
| unpack | 16 | 14.80–14.97 | 229.3–230.2 | ≈15.5× |
| pack (control, no kernel) | 12 | 169.0–176.5 | 184.5–195.7 | — (both scalar) |
| unpack (control, no kernel) | 12 | 202.1–207.2 | 197.2–208.2 | — (both scalar) |
| full `DecodePFOR` block | 8 / 16 / 12 | 16.9–17.8 / 17.1–18.1 / — | — / — / 205.1–211.0 | — |

The width-12 rows run scalar code in both columns. The ~9% gap between them in the pack case (medians 171.0 vs 188.0 ns) is run-to-run and code-layout noise, not a SIMD effect.

**Other benchmarks** (ns/op ranges):

| Benchmark | ns/op | allocs/op |
|---|---|---|
| DeltaRoundtrip (128) | 128.5–131.7 | 0 |
| PFOREncode dense / small / exceptions (widths 0/6/4, scalar) | 171.7–173.6 / 321.0–345.9 / 594.7–596.9 | 0 |
| PFORDecode dense / small / exceptions (widths 0/6/4, scalar) | 9.85–10.03 / 146.4–154.1 / 161.2–168.0 | 0 |
| PostingIter (100k synthetic postings) | 304,975–319,839 | 0 |
| IngestFixtures (1,000 chunks incl. fsync) | 36.0–41.5 ms (~7.34 MB/op) | ~102,200 |
| SearchFixtures unknown / rare / topic | 279.4–293.8 / 513.3–545.6 / 2,828–2,849 | 0 / 6 / 51 |
| SearchFixtures multi-unique-2 / mixed-3 / mixed-6 / common-3 | 3,415–3,582 / 10,698–11,153 / 18,953–20,033 / 20,805–21,085 | 51 |
| SearchFixturesParallel (default slots = 16) | 2,849–3,576 | 51 |

How to read these:
- **Observed before/after timing.** Compared with the pre-review run, the unknown-term query went from 186–200 to 279–294 ns, and the rare-term query from 391–415 to 513–546 ns. The increase is consistent with admission overhead, but the runs also include other review changes and desktop variability; this is not an isolated causal measurement of the slot channel and `select`.
- **Fixture search barely uses the SIMD widths.** Fixture postings are small and mostly choose widths below 8, so the kernels do not noticeably change search time on fixtures. The kernel speedups above are isolated per-block numbers, not end-to-end gains.
- **Allocations.** For k = 10, the 51 allocs/op are the result slice plus 5 metadata strings per hit, copied out of the mapping.

## Limitations and unresolved items

1. **SIMD coverage is partial.** SIMD covers only widths 8 and 16, on amd64 only, in packing and unpacking. Every other width, the delta and exception stages, and other architectures are scalar, and there are no AVX2/AVX-512/NEON kernels. The real-corpus width distribution is unknown, so the end-to-end SIMD benefit on SEC data is unmeasured.
2. **No passage store; not end-to-end RAG.** Text is not stored.
3. **No segment merging.** Search fans out over every segment.
4. **Append-only, one-shot build.** There are no deletes, updates, or incremental commits. Duplicate chunk IDs are not detected.
5. **Memory accounting is conservative but partial.** See Ingestion for what is not accounted. The analyzer charge is deliberately pessimistic (~250 B + 2·len per distinct term), so `MaxBufferBytes` must comfortably exceed the largest expected document. For example, a 16 KiB limit rejects typical ~100-term fixture chunks.
6. **Admission overhead is not isolated.** Tiny-query benchmarks increased by about 0.1 µs across review versions, with other changes and desktop variability also present. There is no fairness or priority beyond Go channel semantics.
7. **Lock portability.** The non-flock fallback lock is stale after a crash and has not been tested on such a platform. Network filesystems are unsupported. Crash consistency also relies on the filesystem honoring fsync and rename.
8. **Open cost is O(file size)** because of the CRC check. SIGBUS can occur if a mapped file is modified externally. mmap is only implemented for linux/darwin/*bsd.
9. **Basic query engine and analyzer.** Retrieval is term-at-a-time with no WAND/block-max skipping. There are no phrase queries or metadata filters, and the analyzer does no stemming.
10. **Limited real-data evidence.** The initial microbenchmarks below use generated fixtures. A subsequent [real SEC baseline](../../bench_kit/RESULTS.md) measures four actual filings and 50 query workers. That small, warm-cache corpus does not establish production scale or relevance quality.
11. `pkg/model` has no test file of its own; `Validate` is tested from `pkg/index`.

## Subsequent source audit and real SEC load

The portable bit-packing adapter now has working scalar bodies in both internal entry points, replacing unreachable panic bodies; a scalar-only regression covers widths 0–32. No placeholder/mock functions remain in the audited Phase 1 production source. See [audit evidence](../../bench_kit/results/validation/phase1-audit.txt) and the [real SEC benchmark](../../bench_kit/RESULTS.md) for the subsequent 10-million-search run. The earlier raw microbenchmark outputs remain historical measurements.
