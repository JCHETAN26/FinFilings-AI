# Morgan Stanley titled-heading boundary investigation — parser v3 (November 1, 2026)

**Status: independently verified by Codex (November 1, 2026) on the fresh replay in [`verified/`](verified/replay-summary.json).** Evidence classes are kept apart:

| Class | Files | Produced by | Standing |
|---|---|---|---|
| Developer run r1 | [replay-summary.json](replay-summary.json), [replay.log](replay.log) | Claude Code (earlier tooling) | developer evidence, preserved unmodified |
| Developer run r2 | [r2/](r2/replay-summary.json) | Claude Code (post-review tooling) | developer evidence |
| **Verified replay** | [verified/replay-summary.json](verified/replay-summary.json), [verification/replay-codex.log](verification/replay-codex.log) | **Codex**, fresh download and fresh output paths | independent |
| Codex checks | [verification/](verification/codex-independent-checks.json), incl. [public-snapshot-check.json](verification/public-snapshot-check.json) | Codex | independent |

All three replays agree on the SHA-256 of both passage streams (v2 and v3), section counts and Item 7 boundary. The active expanded corpus, indexes, embeddings, model artifacts, frozen suite and audit evidence were not modified. The broad agent suite was **not rerun** after the parser change (offline recheck only, section 9). Codex's public-snapshot check for the parser checkpoint passed ([public-snapshot-check.json](verification/public-snapshot-check.json), 2026-11-01 19:36 UTC): 29 modified or non-ignored untracked files (314,372 bytes) scanned, no sensitive-pattern matches and no forbidden local artifacts. It is a pattern scan and inventory review, not a universal secret-detection guarantee, and it excludes its own record; raw SEC downloads, exported indexes and model artifacts remain ignored.

## 0. Independently verified outcome

* **Source:** Codex downloaded the document afresh through the CLI ([acquisition-codex.json](verification/acquisition-codex.json)): 9,714,819 bytes, SHA-256 `3002143b544ab01805ac755efc857f8f62e59fe7a80c6d6fecdf64be560278b2` — identical to the developer download and **not** the archived 9,714,832 bytes / `57698077…12df`. The old raw HTML no longer exists; the archived hash is not claimed verified.
* **Only this is claimed about equivalence:** the 361 stored, indexed passages (Items 1A, 7, 8) produced by the v2 replay match the frozen corpus in full text and metadata (361/361). **No claim is made that the full visible text of the two raw files is the same.**
* **Words (Codex replay):** Item 1A 10,672 / 42 chunks (both parsers, identical); Item 8 49,101 / 192 (both, identical); Item 7 v2 **32,278** / 127 → v3 **20,497** / 81, i.e. **11,781 words removed**, all of which are Item 7A (Quantitative and Qualitative Disclosures about Risk) text. v3 closes Item 7 at the proper Item 7A heading. There is **no MD&A coverage gain, and no "lost 3,000 MD&A words" were recovered**; Item 7A is not relabelled as Item 7 and the reference Item 15 content is not relabelled as Item 8.
* **IDs:** 361 v2 passages, 315 v3, 46 IDs only in v2, exactly one changed ID (`000089542126000086#item7#passage-000080`); 14 exported files (7 per variant) rehashed; shard-router reopen shows file descriptors 6→6 and goroutines 3→3, search after close rejected, hit metadata valid after close.
* **Original four filings:** identical v2/v3 output.
* **Tests (Codex):** 40 application and 50 bench-contract Python tests pass ([python](verification/python-tests.log), [bench](verification/benchmark-contract-tests.log)); `go vet ./...` clean ([log](verification/go-vet.log)); `go test -race ./...` with `FINFILINGS_MS_HTML` pointing at the real 9.7 MB file passes ([log](verification/go-race-real.log); packages other than `cmd/parserreplay` and `pkg/ingest` were served from the Go test cache); the real-source test passes directly with `-v` ([log](verification/real-morgan-test.log)). Machine-readable summary: [codex-independent-checks.json](verification/codex-independent-checks.json).
* **Default parser:** new ingestions use v3 (`deterministic-canonical-heading-v3-go`). **The active 49,452-passage corpus and indexes still contain the v2 Morgan Stanley Item 7; adopting v3 there requires a separately versioned corpus rebuild and re-benchmark, which has not been done.**
* **HPE 2024 critic gap** from the broad suite is unrelated to this work and remains unresolved and unchanged.

## 1. Diagnosis from real source

The known gap in `docs/build/edgar-ingest.md` said Morgan Stanley's titled Item 7 "stops at a Cybersecurity subheading inside MD&A, so about 3,000 MD&A words are not emitted". **That description is misleading and is corrected here: no MD&A text was missing, and v3 does not increase MD&A or "recover" 3,000 MD&A words.** The real document (and Codex's independent raw inspection: Item 7A at page 58, Item 8 at page 78) shows the following structure:

| Visible-word offset | Standalone line | What it is |
|---:|---|---|
| 8,889 | `Risk Factors` | Item 1A (unchanged) |
| 19,561 | `Cybersecurity` | A 16-word **Item 1C pointer**: "For a discussion of cybersecurity, see 'Quantitative and Qualitative Disclosures about Risk—Operational Risk—Cybersecurity.'" Genuine forward boundary after Item 1A |
| 19,585 | `Management's Discussion and Analysis …` | Item 7 starts |
| **40,082** | **`Quantitative and Qualitative Disclosures about Risk`** | **MS's Item 7A heading.** The 7A title regex only matched "…about *Market* Risk", so v2 did not see it |
| 40,597–54,260 | running head `Risk Disclosures` (19 pages) | The whole 7A part |
| **51,863** | `Cybersecurity` (followed by `Risk management and strategy`, `Governance`, …) | An **interior subsection of the 7A part** (Operational Risk), on page 74 under the `Risk Disclosures` running head |
| 54,826 | `Financial Statements and Supplementary Data` | Item 8 |

Two independent defects therefore combined:

1. **Missed Item 7A** — the "about Risk" title was unrecognised, so v2 Item 7 ran **11,781 words** into the risk part (40,082 → 51,863), i.e. it already contained the next true section.
2. **Backward inferred title as a boundary** — the interior `Cybersecurity` line was accepted as Item 1C and ended Item 7 at 51,863, dropping the remaining **2,963 words** (51,863 → 54,826). That is the "~3,000 words" in the old note. They are the tail of the risk part, not MD&A.

Fixing only defect 2 would have enlarged Item 7 to 35,241 words by absorbing the whole 14,744-word risk part (a leak of the next section into Item 7). Fixing only defect 1 would have left the same bug for any filer whose MD&A contains a subheading that resembles an earlier item title. v3 fixes both.

## 2. The fix (`deterministic-canonical-heading-v3-go`)

In titled mode only (`pkg/ingest/parser.go`):

* **Order rule.** An *inferred* standalone title is a boundary only if its item is later in canonical 10-K order than the most recent accepted heading. Backward or repeated titles (interior subheadings such as `Cybersecurity`, running heads, a tail index of items) are ignored and counted in the new `ignored_titled_headings` report field.
* **Explicit headings are never filtered.** Out-of-order `Item N` headings keep their v2 meaning.
* **7A title variant for standalone titles only.** `Quantitative and Qualitative Disclosures about Risk` is accepted as Item 7A. Explicit `Item 7A. …` headings and item-heading parses are byte-for-byte unchanged (tested).
* Unchanged: all parser bounds, the 1A/7/8-in-order requirement, and no emission before all three sections validate. `start_word` was added to each section's report entry so boundaries can be located.
* `ParserOptions.LegacyTitledHeadings` reproduces v2 exactly for baseline replays; its reports carry `deterministic-canonical-heading-v2-go`. Historical v2 reports are unchanged.

Section identity is preserved, as the strict section-tagging requirements demand: Item 7A is not mislabelled as Item 7, Item 15 financial statements are not relabelled as Item 8, and no reference stub is extended. Item 7A text is **not emitted** (as for every filer whose explicit Item 7A heading was recognised — the corpus indexes only Items 1A, 7 and 8).

## 3. Source acquisition — status and a hash mismatch

* Fetched with the existing paced EDGAR client (`parserreplay fetch`): the single recorded URL only, **1 request/second**, 120 s timeout, ≤2 retries, 16 MiB cap, contact header read from `SEC_USER_AGENT` in the environment and never written to argv, logs or this repository. Two downloads were made (the first was discarded by the exact-hash check).
* **SEC now serves 9,714,819 bytes, SHA-256 `3002143b…78b2`, not the recorded 9,714,832 bytes / `57698077…12df`.** Both downloads were identical to each other, so the difference is stable on the SEC side, not transient. The cause is unknown (13 bytes).
* Per the exact-hash requirement the file was **not** accepted as the corpus bytes. It is kept only under `data/parser-replay-20261101/ms-20251231.sec-current-NOT-CORPUS-BYTES.htm` (ignored) and every report says `matches_recorded_filing: false`.
* **What was established, and what was not.** The v2 replay on the current SEC bytes emits 361 Morgan Stanley passages whose IDs, section tags and text SHA-256 values are all identical to the corresponding records in `data/sec-scale-v2-evaluation-20261031/chunks.jsonl`, and the parser's whole-document visible-word count equals the recorded 109,364. That shows the **selected, indexed passages (Items 1A, 7, 8) match**. It does **not** show that all visible text is identical: text outside those sections is never emitted or compared, and a word count is not a content comparison. The original raw HTML of the ingested copy is unavailable, so the nature of the 13-byte difference cannot be reconstructed. No statement here claims the old raw hash was verified.

## 4. Replay results (identical bytes, both parsers)

Artifacts (ignored, exclusive-create fresh directories; the tool requests mode 0444 but this exFAT volume does not enforce permission bits, so immutability here rests on the recorded per-file SHA-256 values in `replay-summary.json`, not on file modes): `indexes/parser-boundaries-20261101/ms-v2-legacy/` and `…/ms-v3/` (each: `passages.jsonl`, `parse-report.json`, quarter-shard `segments/` with `CORPUS_SHA256`). Machine-readable record: [replay-summary.json](replay-summary.json); console log: [replay.log](replay.log). This is the **first developer run** (earlier tooling that held passage text in memory); it is preserved unmodified. After the review fixes below the replay was rerun into fresh paths (`indexes/parser-boundaries-20261101-r2/`, [r2/replay-summary.json](r2/replay-summary.json), [r2/replay.log](r2/replay.log)); both runs agree on every passage-stream SHA-256, section count, anchor and corpus comparison. The r2 summary additionally records a streamed per-section text hash. Neither run is independent verification.

| Section | v2 (historical) words / chunks | v3 words / chunks | Start word | Result |
|---|---:|---:|---:|---|
| Item 1A | 10,672 / 42 | 10,672 / 42 | 8,889 | **identical** (all 42 chunk IDs and texts) |
| Item 7 | 32,278 / 127 | **20,497 / 81** | 19,585 | shortened by **11,781 words** |
| Item 8 | 49,101 / 192 | 49,101 / 192 | 54,826 | **identical** (all 192 chunk IDs and texts) |
| Total emitted | 361 chunks | 315 chunks | | 46 fewer |

* v2 passages SHA-256 `0b036069…ebbb`; v3 `84df9e8c…0fc`.
* **Item 7 chunk stability:** the first 80 chunks (`…#item7#passage-000000`–`000079`) are identical. `…#passage-000080` changes (v3's last, partial chunk ends at the new boundary); `…000081`–`000126` (46 IDs) exist only in v2. Chunks are 256-word windows from the section heading, so only the section tail moves. No stability is claimed beyond this prefix, and Item 7 ID/text content differs from the active corpus at and after chunk 80.
* **Removed-from-Item-7 text** (starts at document word 40,082): begins "Quantitative and Qualitative Disclosures about Risk Risk Management Overview Risk is an inherent part of our businesses and activities. We believe effective risk management is vital to the success of …" and ends "… third-party risk management (supplier and affiliate risk oversight and assessment), among others." The retained Item 7 now ends "… subject to its current SCB requirement of 4.3% until October 1, 2027. We continue to monitor developments related to the open proposal. 57 December 2025 Form 10-K Table of Contents". v3 Item 7 text is exactly a prefix of v2 Item 7 text.
* **Unemitted words between Item 7's end and Item 8's heading:** 2,963 under v2 (the lost tail) → 14,744 under v3 (the whole 7A part, now outside the three indexed sections). The Cybersecurity subsection is inside that range.
* Heading candidates 22 → 17; `ignored_titled_headings` = 6 (the interior `Cybersecurity` plus five backward end-matter titles: Unresolved Staff Comments, Properties, Legal Proceedings, Mine Safety Disclosures, Market for Registrant's Common Equity).
* **Consequence to review:** a query filtered to `Item 7` can no longer retrieve MS's risk-management text (VaR, Operational Risk, Cybersecurity); that text is not indexed in any section, as for other filers with a recognised Item 7A. The old note's "recovered MD&A words" framing does not apply: v3 trades 11,781 words that never belonged to MD&A for correct section identity.

### Reopen through the existing mmap shard router (both variants)

`index.OpenShardRouter` opened each export: 361 docs (v2) and 315 docs (v3), one 2026_Q1 shard. Section-filtered searches for MS 2026: `Item 7`, `Item 1A`, `Item 8` queries returned hits that all matched the filter; the exact `Item 7A` filter returned **0 hits** in both variants (7A is not an indexed section). After `Close()`: search is rejected, hit metadata stays valid, and open file descriptors (6→6) and goroutines (3→3) return to baseline; the parser work directory was empty after each parse. The query built from the removed text returns v2 chunk `…item7#passage-000080` first and no chunk numbered ≥ 81 in v3. These are correctness probes; **no performance or retrieval-quality numbers are claimed**.

### Original four filings (real local HTML, SHA-256 matched to `data/sec/provenance.json`)

AAPL 2023, MSFT 2024, AMZN 2024, NVDA 2024: item-heading mode in both; **parser-v2-legacy and v3 produce byte-identical passage streams for all four**. (Their active-corpus text is the older unclassified chunking, so the comparison is v2-vs-v3 on the same bytes, not against the corpus.) NVDA's 32-word Item 8 reference stub is unchanged and untouched by this work.

## 5. Tests added

`pkg/ingest/parser_boundaries_test.go` (synthetic fixtures): interior Cybersecurity subheading in titled MD&A; MS-shaped risk part closing Item 7 with nothing leaking into any section; repeated running heads; forward boundaries still ending sections (the Item 1C pointer case and Item 9); explicit backward headings still honoured; inferred vs explicit `Cybersecurity` distinguished; item mode byte-identical under v2/v3 and explicit `Item 7A … about Risk` unchanged; validation failure emits nothing. Disabling the order rule, disabling the 7A variant, or applying the order rule to explicit headings each makes tests fail (checked by mutation during development). `pkg/ingest/replay_real_test.go` is the opt-in real-source test. `cmd/parserreplay/fetch_test.go` (local httptest servers only) covers exclusive fixture publication, refusal to overwrite an existing original or drift fixture, hash-mismatch handling, partial-file cleanup, contact redaction and rate/identity checks; `cmd/parserreplay/replay_test.go` covers the disk-backed, bounded-read diff, passage-offset validation, fresh artifacts, shard-router reopen/cleanup, streamed corpus comparison and the overwrite refusal.

## 6. Review fixes (developer revision, before verification)

* Replay keeps only per-passage metadata (ID, section, word count, text hash, file offset/length) in memory; passage text stays in `passages.jsonl` and is read back one passage at a time (≤ 1 MiB, ID-checked). Section hashes and word counts are computed while streaming; boundary anchors read a handful of passages. Tests assert there is no text field, that reads stay ≤ 8 whether the risk part is 2,000 or 60,000 words, and that unchanged sections need zero text reads. The corpus comparison reads the corpus with a bounded line reader.
* No mutable package-level variables in the new Go code (`msFiling` is now a value-returning function; the rest are constants).
* A fetch refuses to run if either the recorded-name file or the `…NOT-CORPUS-BYTES.htm` drift file already exists (before any request), and publication is exclusive-create with re-hash and partial-file removal, so an existing fixture cannot be overwritten. Replay refuses an existing summary before writing artifacts.
* Debug code removed; errors from marshalling, closes, directory reads, and cleanup are checked or joined; temporary files are closed.

## 7. Reproduce

Environment for the opt-in real-source tests and replay: `GOCACHE=/tmp/finfilings-go-cache`; `SEC_USER_AGENT="<name> <contact email>"` (only for `fetch`; set it in the environment, never as an argument); `FINFILINGS_MS_HTML=<ABSOLUTE path to the document>` (only for the opt-in `RealMorganStanley` test; `go test` runs from the package directory, so a relative path would not resolve).

```bash
export GOCACHE=/tmp/finfilings-go-cache
go run ./cmd/parserreplay fetch -rate 1 -allow-drift      # refuses if a fixture already exists; choose a fresh -dir rather than deleting
export FINFILINGS_MS_HTML="$PWD/data/parser-replay-20261101/ms-20251231.sec-current-NOT-CORPUS-BYTES.htm"   # run from the repository root so $PWD is absolute
go test ./pkg/ingest -run RealMorganStanley -v
go run ./cmd/parserreplay replay -source "$FINFILINGS_MS_HTML" -index-dir indexes/parser-boundaries-FRESH -results-dir bench_kit/results/parser-boundaries-FRESH
go vet ./... && go test ./... && go test -race ./...
```

`-allow-drift` is required while SEC serves bytes that differ from the recorded filing. Use fresh `-index-dir`/`-results-dir` values for every replay.

## 8. Limitations and open items

* Only one titled-mode filing exists in the corpus, and the raw bytes of the other 147 parser-v2 filings were not kept, so **v3 could not be re-checked against them**. Item-mode behaviour is unchanged by construction and tested synthetically and on the four local originals; a titled-mode filing other than MS has never been observed.
* The raw document SHA-256 does not match the recorded one (section 3). Only the indexed passages were compared; the full raw/text difference cannot be reconstructed because the original HTML was not kept. A reviewer who can obtain the original 9,714,832-byte copy should rerun the replay on it; no outcome is predicted here.
* The active corpus and indexes still contain the v2 Morgan Stanley Item 7. Adopting v3 requires a new, versioned corpus and index build and a new validation-suite version; neither was started. `broad-validation-v1` and its results are untouched.
* The order rule treats a backward inferred title as non-boundary even in a document that is genuinely laid out out of canonical order; such a document was already rejected by the 1A < 7 < 8 requirement, and no counter-example is known.
* Explicit `Item 7A. … about Risk` headings (non-"Market") are still not recognised in item mode; this is preserved deliberately because changing it would alter accepted layouts that cannot be re-verified here.

## 9. Historical broad-suite recheck (offline only)

[broad-historical-recheck.json](verification/broad-historical-recheck.json) / [log](verification/broad-historical-recheck.log): the existing `broad-validation-20261101/run-01` was re-verified offline with the lenient historical verifier. It still passes **30/30 cases, 172 citations**, with zero global problems and zero evaluator-summary discrepancies; suite, corpus, ingestion-report, original-provenance, vector-index (5/5), reranker (2/2) and sparse-index (28/28) hashes all recompute and match. **Code drift is reported, as expected and only for `pkg/ingest/parser.go`** (52 recorded code files unchanged); the strict-code option was not used. This is a recheck of saved results; **the broad agent was not rerun after the parser change.**
