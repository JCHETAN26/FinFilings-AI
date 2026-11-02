# Agent latency: fast mode — measured results (November 2, 2026)

Scope: the user-authorized, bounded latency optimization in [`docs/build/agent-latency.md`](../../../docs/build/agent-latency.md). Everything below is
taken from the saved raw artifacts in this directory (never edited) and from their independent offline verification. **Not a production, throughput,
tail-latency (p99), cost or SLA result, and not a human-judged answer-accuracy result.** Verification and release gates passed; Git publication is recorded in history and the UI launch command is available. These benchmark figures are not new live UI measurements or a deployment.

## Conditions

Local single machine (CPU only; details in [`verification/environment.json`](verification/environment.json)), model `claude-haiku-5-5` through the existing
tool-disabled Claude CLI provider, 4 inference workers × 2 CPU threads, at most 2 concurrent model calls, at most 3 concurrent subqueries, at most 2 retries
per subquery, 5 contexts per search, 2 retained passages, at most 12 published quotes, critic threshold unchanged, one active audit at a time (one UI audit
maximum). Corpus, indexes and models unchanged throughout (corpus version `v3`, 49,406 passages / 152 enrolled filings; **mixed parser lineage**: only Morgan Stanley was
reparsed with parser v3, the other filings were not, so this is not an "all 152 filings parser-v3" corpus). Retrieval artifacts are unchanged in every run. The
code hashes are **not** identical in every run: failed `paired-run-01` ran different code (its `artifact-hashes.json` starts `cdbe31ed3480`). For the accepted-candidate
batches `paired-run-02`, `full-run-01` and `full-run-02-remaining` the `artifact-hashes.json` files are byte-identical (`82e66a0ea350…`; 59 code files compared strictly, no drift). Per-audit model-call maximum is 11 in both modes. Provider latency varies
between calls; no result cache exists.

## 1. Preregistered paired gate: PASSED (`paired-run-02`)

Plan: [`verification/pre-run-plan.json`](verification/pre-run-plan.json). Six frozen cases × 2 repeats × 2 modes = 24 live runs; alternating AB/BA order.
All 24 passed the strict independent verifier ([`paired-run-02/latency/paired-latency-verification.json`](paired-run-02/latency/paired-latency-verification.json)).

* **Gate (reduction of medians over the 8 primary supported runs per mode: `audit04`, `audit07`, `broad25`, `broad21`, × 2 repeats):**
  legacy **29.71211002200289 s** → fast **23.503144278001855 s** = **20.8970878857 %** reduction against the preregistered ≥ 15 %. The gate uses the
  reduction of medians, not the median of per-pair percentages (descriptive 21.99 % over supported pairs, 18.91 % over all pairs; order effect:
  legacy-first pairs 25.7 %, fast-first pairs 14.0 %).
* Over all 12 runs per mode: 59 → 49 structured CLI invocations; 37 retrievals and 13 retries in each mode; 10 legacy final syntheses removed;
  fast routes: 10 `host_quotes` + 2 `none`; 18/18 available subqueries cited in both modes; expected-absent scopes withheld in every run; 98 citations
  (53 legacy + 45 fast) over 57 unique source passages.
* **CLI invocations are not provider turns.** One structured invocation can contain several CLI-reported turns: 59 invocations = 118 turns (legacy),
  49 invocations = 104 turns (fast) ([`verification/paired-run-02-call-breakdown.json`](verification/paired-run-02-call-breakdown.json)). Fast
  critic calls are individually slower (median 7.11 s vs 5.98 s); the saving is the removed final call.
* Not universal: in one pair (`audit04` r2) fast took 31.80 s against legacy 30.13 s, and provider-turn jitter plus genuine retries exist in both modes.

| case | repeat | legacy s | fast s | legacy / fast invocations | legacy / fast cited topics |
|---|---|---|---|---|---|
| audit04 | 1 | 29.68 | 21.70 | 4 / 3 | 2 / 2 |
| audit04 | 2 | 30.13 | 31.80 | 4 / 3 | 2 / 2 |
| audit07 | 1 | 34.74 | 23.21 | 4 / 3 | 2 / 2 |
| audit07 | 2 | 49.03 | 39.51 | 5 / 4 | 2 / 2 |
| audit10 (descriptive) | 1 | 23.83 | 23.28 | 7 / 7 | 0 / 0 (withheld) |
| audit10 (descriptive) | 2 | 22.11 | 23.10 | 7 / 7 | 0 / 0 (withheld) |
| broad21 | 1 | 29.15 | 19.53 | 4 / 3 | 2 / 2 |
| broad21 | 2 | 27.83 | 23.80 | 4 / 3 | 2 / 2 |
| broad25 | 1 | 29.74 | 24.26 | 4 / 3 | 2 / 2 |
| broad25 | 2 | 26.55 | 22.97 | 4 / 3 | 2 / 2 |
| broad30 (descriptive) | 1 | 30.83 | 23.26 | 6 / 5 | 1 / 1 |
| broad30 (descriptive) | 2 | 32.80 | 20.92 | 6 / 5 | 1 / 1 |

Twelve runs per mode is a small N: this supports "faster in this preregistered run", not a latency distribution.

## 2. Failed first attempt, contract fix, fresh second run

`paired-run-01` is **FAILED and preserved**: 23 runs verified, but the fast `broad25` repeat 2 run (`paired-run-01/latency/runs/11-fast-broad25-r2.failure.json`)
was rejected with "critic retained unknown or duplicate evidence". The raw returned values were not saved, so the exact cause is unknown; the
two-identifier-namespace design (canonical IDs plus `E01` handles) is a plausible but unproven cause. All 24 outcomes were kept
([`paired-run-01/latency/paired-latency-verification.json`](paired-run-01/latency/paired-latency-verification.json), [`verification/paired-run-01-verifier.log`](verification/paired-run-01-verifier.log)).
The retention contract was then changed to one namespace (handles only; unknown/repeated handle rejects the whole verdict; counts-only error text) and legacy
prompt/payload/output equivalence against `HEAD 489bfcf` was re-checked ([`verification/legacy-equivalence-after-retention-fix.json`](verification/legacy-equivalence-after-retention-fix.json),
4 checks over empty and five-passage evidence contexts built from 5 saved real passages; not 5 separate fixtures). `paired-run-02` is a separate fresh run; run 01 is not used as evidence for the new code. In the 24 fresh runs and the 30 completed full cases below no
contract rejection occurred; the fix is supported by those runs, not proven for all inputs.

## 3. Full frozen 30-case fast check: RECOVERED from an external interruption, not one uninterrupted run

The execution environment/session ended during `full-run-01` after **18** cases were saved (`audit01`..`broad18`). Its `run-status.json` still says `started`; no
surviving worker was found; its original files are untouched ([`verification/full-run-01-interruption.json`](verification/full-run-01-interruption.json)).
Any unsaved in-flight usage is unknown and is not counted. The existing verifier on that directory independently verified 18/18 saved cases and flags
exactly the unfinished status and the 12 omitted cases; its `passed: false` is preserved ([`verification/full-run-01-verifier.log`](verification/full-run-01-verifier.log)).
`full-run-02-remaining` selected **only** `broad19`..`broad30` before its first request and completed 12/12, strictly verified with `--allow-subset --enforce-gates`, owned services stopped
([`verification/full-run-02-verifier.log`](verification/full-run-02-verifier.log)). **There is no normal-cleanup or health-after record for a single 30-case run, and none is fabricated.**

Composite checks: actual outputs [`verification/full-30-recovery-verification.json`](verification/full-30-recovery-verification.json) (sha256 `f313a4133afa…`) and its
log [`verification/full-30-recovery-verifier.log`](verification/full-30-recovery-verifier.log) (sha256 `a51726b4df80…`), produced by
[`bench_kit/verify_agent_recovery.py`](../../verify_agent_recovery.py), which Codex independently ran and passed (reuses the existing verifier in memory, strict code comparison on both
directories; command in `docs/build/agent-latency.md`), and Codex's independent streaming source/metadata/quote/scope/arithmetic check
[`verification/full-30-direct-source-check.json`](verification/full-30-direct-source-check.json) over the disjoint 18 + 12 union, with matching code/model/index/corpus hashes.

Offline checks after recovery: all 30 saved results projected through the actual UI projection, passed, 140 quotes, max serialized 39,773 bytes
([`verification/full-30-ui-projection.json`](verification/full-30-ui-projection.json)); health profiles show 4 workers × 2 threads and no active audit service
processes before the UI restart ([`verification/concurrency-and-cleanup.json`](verification/concurrency-and-cleanup.json); the interrupted batch has no health-after or
normal-cleanup record). These are offline checks: no new live UI result, and no single uninterrupted 30-case run, is claimed.

Totals over the 30 completed cases: **140 citations**; **52/52 available subqueries cited**; **27 supported + 3 expected-unavailable**; 0 partial/abstained;
**77 retrievals, 17 retries, 107 CLI invocations (224 CLI-reported turns)**; routes **27 `host_quotes` + 3 `none`, 0 fallbacks**; **244 unique original source
passages**; no model, transport or structural failures among the completed cases. Completed-case seconds: median **20.347354651 s**, max **47.207789934 s**,
descriptive, from two batches.

The "requested scope" column lists the company/year/section scope only (not the topic); the topic is in the prompt in each raw JSON/Markdown link. Counters were
re-checked against [`verification/full-30-recovery-verification.json`](verification/full-30-recovery-verification.json).

| id | kind | requested scope | citations | retries | retrievals | CLI invocations | route | flags | outcome | seconds | raw |
|---|---|---|---|---|---|---|---|---|---|---|---|
| audit01 | regression | AAPL 2023; NVDA 2024 | 5 | 0 | 2 | 3 | host_quotes | 0 | supported | 21.71 | [json](full-run-01/evaluation/audit01.json) · [md](full-run-01/evaluation/audit01.md) |
| audit02 | regression | MSFT 2024; AMZN 2024 | 6 | 2 | 4 | 5 | host_quotes | 0 | supported | 47.21 | [json](full-run-01/evaluation/audit02.json) · [md](full-run-01/evaluation/audit02.md) |
| audit03 | regression | NVDA 2024 | 6 | 0 | 2 | 3 | host_quotes | 0 | supported | 23.71 | [json](full-run-01/evaluation/audit03.json) · [md](full-run-01/evaluation/audit03.md) |
| audit04 | regression | AMZN 2024 | 4 | 0 | 2 | 3 | host_quotes | 0 | supported | 22.11 | [json](full-run-01/evaluation/audit04.json) · [md](full-run-01/evaluation/audit04.md) |
| audit05 | regression | AAPL 2023 | 6 | 0 | 2 | 3 | host_quotes | 0 | supported | 19.77 | [json](full-run-01/evaluation/audit05.json) · [md](full-run-01/evaluation/audit05.md) |
| audit06 | regression | MSFT 2024 | 6 | 0 | 2 | 3 | host_quotes | 0 | supported | 19.21 | [json](full-run-01/evaluation/audit06.json) · [md](full-run-01/evaluation/audit06.md) |
| audit07 | regression | AMZN 2024 | 6 | 0 | 2 | 3 | host_quotes | 0 | supported | 29.31 | [json](full-run-01/evaluation/audit07.json) · [md](full-run-01/evaluation/audit07.md) |
| audit08 | regression | NVDA 2024 | 4 | 0 | 2 | 3 | host_quotes | 0 | supported | 19.41 | [json](full-run-01/evaluation/audit08.json) · [md](full-run-01/evaluation/audit08.md) |
| audit09 | regression | AAPL 2025 | 5 | 0 | 2 | 3 | host_quotes | 0 | supported | 19.42 | [json](full-run-01/evaluation/audit09.json) · [md](full-run-01/evaluation/audit09.md) |
| audit10 | regression | MSFT 2024 (Item 1A) | 0 | 4 | 6 | 7 | none | 3 | expected-unavailable (withheld) | 23.26 | [json](full-run-01/evaluation/audit10.json) · [md](full-run-01/evaluation/audit10.md) |
| broad11 | positive | KO 2025 (Item 7); PG 2025 (Item 7) | 4 | 0 | 2 | 3 | host_quotes | 0 | supported | 18.36 | [json](full-run-01/evaluation/broad11.json) · [md](full-run-01/evaluation/broad11.md) |
| broad12 | positive | PFE 2025 (Item 1A); MRK 2025 (Item 1A) | 6 | 0 | 2 | 3 | host_quotes | 0 | supported | 21.55 | [json](full-run-01/evaluation/broad12.json) · [md](full-run-01/evaluation/broad12.md) |
| broad13 | positive | BAC 2026 (Item 7) | 5 | 0 | 2 | 3 | host_quotes | 0 | supported | 18.77 | [json](full-run-01/evaluation/broad13.json) · [md](full-run-01/evaluation/broad13.md) |
| broad14 | positive | TGT 2026 | 5 | 0 | 2 | 3 | host_quotes | 0 | supported | 22.29 | [json](full-run-01/evaluation/broad14.json) · [md](full-run-01/evaluation/broad14.md) |
| broad15 | positive | TSLA 2026 | 6 | 0 | 2 | 3 | host_quotes | 0 | supported | 21.12 | [json](full-run-01/evaluation/broad15.json) · [md](full-run-01/evaluation/broad15.md) |
| broad16 | positive | V 2024 (Item 1A); V 2025 (Item 1A) | 6 | 0 | 2 | 3 | host_quotes | 0 | supported | 23.28 | [json](full-run-01/evaluation/broad16.json) · [md](full-run-01/evaluation/broad16.md) |
| broad17 | positive | COP 2025 (Item 7); OXY 2025 (Item 7) | 6 | 0 | 2 | 3 | host_quotes | 0 | supported | 25.91 | [json](full-run-01/evaluation/broad17.json) · [md](full-run-01/evaluation/broad17.md) |
| broad18 | positive | MET 2025; AFL 2025 | 6 | 0 | 2 | 3 | host_quotes | 0 | supported | 21.73 | [json](full-run-01/evaluation/broad18.json) · [md](full-run-01/evaluation/broad18.md) |
| broad19 | positive | CRM 2026 (Item 8); ADBE 2026 (Item 8) | 6 | 0 | 2 | 3 | host_quotes | 0 | supported | 16.73 | [json](full-run-02-remaining/evaluation/broad19.json) · [md](full-run-02-remaining/evaluation/broad19.md) |
| broad20 | positive | AMD 2026; TXN 2026 | 5 | 0 | 2 | 3 | host_quotes | 0 | supported | 15.46 | [json](full-run-02-remaining/evaluation/broad20.json) · [md](full-run-02-remaining/evaluation/broad20.md) |
| broad21 | positive | UNH 2025 (Item 7); CVS 2026 (Item 7) | 5 | 0 | 2 | 3 | host_quotes | 0 | supported | 18.15 | [json](full-run-02-remaining/evaluation/broad21.json) · [md](full-run-02-remaining/evaluation/broad21.md) |
| broad22 | positive | SBUX 2024 (Item 7); SBUX 2025 (Item 7) | 6 | 0 | 2 | 3 | host_quotes | 0 | supported | 17.34 | [json](full-run-02-remaining/evaluation/broad22.json) · [md](full-run-02-remaining/evaluation/broad22.md) |
| broad23 | positive | ISRG 2026 (Item 7) | 6 | 0 | 2 | 3 | host_quotes | 0 | supported | 19.05 | [json](full-run-02-remaining/evaluation/broad23.json) · [md](full-run-02-remaining/evaluation/broad23.md) |
| broad24 | positive | LLY 2026 (Item 7); JNJ 2026 (Item 7) | 5 | 0 | 2 | 3 | host_quotes | 0 | supported | 13.39 | [json](full-run-02-remaining/evaluation/broad24.json) · [md](full-run-02-remaining/evaluation/broad24.md) |
| broad25 | positive | HPE 2024 (Item 7); HPE 2025 (Item 7) | 5 | 0 | 2 | 3 | host_quotes | 0 | supported | 21.01 | [json](full-run-02-remaining/evaluation/broad25.json) · [md](full-run-02-remaining/evaluation/broad25.md) |
| broad26 | positive | HD 2026 (Item 8); WMT 2026 (Item 8) | 6 | 0 | 2 | 3 | host_quotes | 0 | supported | 16.48 | [json](full-run-02-remaining/evaluation/broad26.json) · [md](full-run-02-remaining/evaluation/broad26.md) |
| broad27 | negative | TSLA 2024 | 0 | 4 | 6 | 7 | none | 3 | expected-unavailable (withheld) | 22.54 | [json](full-run-02-remaining/evaluation/broad27.json) · [md](full-run-02-remaining/evaluation/broad27.md) |
| broad28 | negative | NVDA 2024 (Item 7) | 0 | 4 | 6 | 7 | none | 3 | expected-unavailable (withheld) | 20.90 | [json](full-run-02-remaining/evaluation/broad28.json) · [md](full-run-02-remaining/evaluation/broad28.md) |
| broad29 | mixed | GS 2026; BAC 2025 | 1 | 1 | 3 | 4 | host_quotes | 3 | mixed: available side supported, absent side withheld | 19.80 | [json](full-run-02-remaining/evaluation/broad29.json) · [md](full-run-02-remaining/evaluation/broad29.md) |
| broad30 | mixed | KO 2025 (Item 7); AMZN 2024 (Item 7) | 3 | 2 | 4 | 5 | host_quotes | 1 | mixed: available side supported, absent side withheld | 17.48 | [json](full-run-02-remaining/evaluation/broad30.json) · [md](full-run-02-remaining/evaluation/broad30.md) |

Absent scopes (reason, from the frozen suite's availability expectations; nothing was substituted): `audit10` MSFT 2024 Item 1A and `broad28` NVDA 2024 Item 7 —
the filing exists but has zero passages under that exact section filter (`exact_section_absent`); `broad30` AMZN 2024 Item 7 — same reason; `broad27` TSLA 2024 and
`broad29` BAC 2025 — no such filing year is enrolled (`filing_year_absent`). The fully absent rows spend 4 retries (the bounded budget of 2 per subquery) before withholding. Mixed cases `broad29` and `broad30` cite their available side and withhold the absent side.

**Why `broad29` has 3 flags.** (1) The expected missing BAC 2025 scope ("no evidence meeting threshold after 1 retries") and (2) the stalled-rewrite stop after 1 retry
(no verified evidence and no new critic query), plus (3) one GS quote proposal that the host rejected as not verbatim in its E03 passage ("Dropped claim for handle E03:
quote not found verbatim in that passage"). The rejected quote is not published; this case is therefore **not** a zero-quote-rejection case. One valid GS ratio quote
(`000088698226000091#item8#passage-000180`) remains and available-subquery coverage is unchanged (52/52 over all 30 cases). `broad30` has 1 flag (the expected absent AMZN Item 7 scope). Source checks are not human semantic gold.

### Why 140 citations here versus 178 in the historical full run

The historical `full-run-05` published 178 citations, 136 model calls, median 21.03 s (see `bench_kit/RESULTS.md`). The difference is **selection, not measured accuracy**:
legacy synthesis picked quotes across all accepted passages in one final call, fast mode takes up to 4 critic-proposed, host-verified quotes per accepted hop and caps
at 12. Both cover every available topic (52/52). No currently available topic was lost. The 136-call/21.03 s historical suite is **not a matched control** (different code, a different run with its own retries and provider conditions); the matched comparison is the paired run in §1. Likewise 20.35 s vs 21.03 is not a causal speedup claim.

## Limits and known issues

* Fallback (one extra `HandleDraft` when an accepted hop yields no valid quote) was **not exercised live** (0 fallbacks); it is covered by unit-test doubles only.
* Retries of 40+ s are still possible (`audit02` took 47.21 s with 2 retries; `audit07` fast r2 took 39.51 s). No universal SLA.
* Source checks verify source identity, verbatim quotes, scope, bounds and arithmetic. They are **not independent human semantic gold**, and quote quality is not scored.
* Small N (12 + 12 paired runs; 30 cases): no p99, throughput or cost claim.
* Load, retrieval benchmarks and Go race/vet were not rerun for this work. Tests: Codex's latest **192 application / 216 benchmark tests** passed (logs
  [`verification/application-tests-03.log`](verification/application-tests-03.log), [`verification/benchmark-tests-04.log`](verification/benchmark-tests-04.log); the 216 include the 25 new recovery-verifier tests).
  The earlier 192 / 191 logs ([`application-tests-03`](verification/application-tests-03.log), [`benchmark-tests-03`](verification/benchmark-tests-03.log)) are historical, from before those tests were added.
  The earlier UI real checks (167 application / 168 benchmark) are historical.

## Evidence index (SHA-256 prefixes; no secrets, weights, datasets or personal contact data are stored here)

| artifact | hash (first 12) |
|---|---|
| `verification/pre-run-plan.json` | b83066b89457 |
| `paired-run-02/latency/paired-latency-verification.json` | 31cdd8faae95 |
| `paired-run-01/latency/paired-latency-verification.json` | fccdff70061d |
| `verification/full-run-01-interruption.json` | 204ac6ab0239 |
| `verification/full-30-direct-source-check.json` | 91000b1af8ca |
| `full-run-01/independent-broad-verification.json` (original non-passing report, kept) | fe1a786e086b |
| `full-run-02-remaining/independent-broad-verification.json` | 3130b8d4732c |
| `paired-run-02/artifact-hashes.json` = `full-run-01/…` = `full-run-02-remaining/…` | 82e66a0ea350 |
| `paired-run-02/latency/records.jsonl` | 25e317a26beb |

Corpus SHA-256 `c62207623da1fd592d79248916ece408b5568eeceaafc1c0777a83a518f7a56f`, suite `2d87557ba1e776f6489b0174211bde7ec21fa60f9a0544f32352b2608808831d`,
ingest report `5364f6c5e7f6027391fcf90ff0b0db0c4adb7538b55e9e697e5d68f662c7233e`. Vector-index, sparse-index and reranker-model file hashes (generated artifacts, not committed) are listed per file in
each run's `artifact-hashes.json`. Raw run records, status files and the original verifier outputs are frozen; nothing was rewritten or deleted.
