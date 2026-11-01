# Expanded-corpus agent revalidation — October 31, 2026

All ten original audit prompts ran once against the frozen **49,452-passage** expanded corpus and the optimized PQ128 / FP32 ONNX hybrid service. **10/10 execution/source checks passed; 61/61 published citations independently verified.** Eight audits have published evidence for every planned subquery, one is partial, and one correctly abstains. Median audit time **22.697032 s**; **28 retrieval calls, seven retries** across 21 subqueries, maximum two retries per subquery.

**Scope:** source grounding and structural subquery coverage, not a human-labeled financial answer-accuracy score. The final answers are exact supporting excerpts, not derived calculations. These are the same ten regression prompts, largely about the original four filings; this is not a representative test across all 77 newly ingested companies. AAPL 2025 directly exercises the newly ingested sectioned filing.

| Audit / prompt | Citation checks | Retries | Cited subqueries | Seconds | Evidence gaps / outcome |
|---|---:|---:|---:|---:|---|
| [01: AAPL 2023 / NVDA 2024 manufacturing and supply-chain risks](evaluation/audit01.md) | 6/6 verified | 0 | 2/2 | 19.356 | No flagged gap. |
| [02: MSFT / AMZN 2024 cloud competition and infrastructure costs](evaluation/audit02.md) | 8/8 verified | 2 | 1/2 | 43.169 | Partial: AMZN evidence remained below threshold after two rewrites; MSFT claims only. This is a retrieval gap, not proof the filing lacks the disclosures. |
| [03: NVDA 2024 export controls and manufacturing dependence](evaluation/audit03.md) | 8/8 verified | 0 | 2/2 | 23.053 | No flagged gap. |
| [04: AMZN 2024 AWS sales and operating expenses](evaluation/audit04.md) | 5/5 verified | 0 | 2/2 | 21.728 | No flagged gap. |
| [05: AAPL 2023 repurchases and dividends](evaluation/audit05.md) | 6/6 verified | 0 | 2/2 | 23.228 | No flagged gap. |
| [06: MSFT 2024 cloud growth and cybersecurity](evaluation/audit06.md) | 7/7 verified | 0 | 2/2 | 20.818 | No flagged gap. |
| [07: AMZN 2024 liquidity, cash flows and lease obligations](evaluation/audit07.md) | 8/8 verified | 1 | 3/3 | 35.408 | Initial operating-lease results lacked obligation amounts; one rewrite recovered a maturity schedule. No remaining flagged gap. |
| [08: NVDA 2024 Data Center growth and gross margin](evaluation/audit08.md) | 5/5 verified | 0 | 2/2 | 20.423 | No flagged gap. |
| [09: AAPL 2025 revenue and repurchases](evaluation/audit09.md) | 8/8 verified | 0 | 2/2 | 24.984 | New AAPL 2025 filing is available; both topics cite section-tagged Item 7/8 passages. |
| [10: MSFT 2024 exact Item 1A cybersecurity and cloud competition](evaluation/audit10.md) | 0/0 verified (no claims) | 4 | 0/2 | 22.341 | Correct abstention: no exact Item 1A passages exist for MSFT filing year 2024 in this corpus. Both subqueries exhaust two retries; no other year/section is substituted. |

## What was checked

- Required Plan → Retrieve_Evaluate → Synthesize state order and 2–3 subqueries.
- Exact requested ticker, filing year and section scope; immutable filters across rewrites; no more than two retries per subquery.
- Retrieved evidence matches original on-disk passage text and metadata, including accession/CIK/form/year identity.
- Published quotes are verbatim contiguous source excerpts after whitespace normalization; citation tag, URL, chunk ID and dashed accession all agree and appear inline.
- Independent verifier streams the corpus once and checks every evidence passage retained in the traces (88 unique source passages), plus all 61 final citations.

## Corpus, provenance and runtime

- Corpus SHA-256: `9e7267d5726e1bfb12cbf4896040da57d63a1cc13efc123e0dcaead3df0f6c72`.
- Trusted catalog: **152 filings** = four original full filings + 148 new parser-v2 acquisitions. Published FinQA contexts remain retrieval distractors and cannot support SEC claims.
- New section-tagged IDs are accepted only with trusted SEC accession metadata and exact source-byte verification. A temporary disk-backed SQLite offset index avoids repeatedly scanning the corpus or retaining all passages in memory. Section components are included in citation tags to avoid within-filing passage-number collisions.
- The enrollment supervisor verifies original SEC provenance, expanded corpus and acquisition-report fingerprints. The new report supplies source URLs, raw-document hashes and filing identities. No SEC requests were made for this revalidation.
- Go shard router and original hybrid RRF configuration; sidecar **four inference workers × two intra-op threads**, reranker ONNX FP32 batch one, pressure-triggered dense batches up to eight above eight live requests. Agent: at most three subqueries and two concurrent Claude calls. Native/HTTP bounds remain unchanged.
- Same ten prompt strings and planner/critic/synthesis instructions as the original harness; availability expectations now derive from the verified corpus. AAPL 2025 is positive, while the exact MSFT 2024 Item 1A test remains negative.
- Requested Claude alias: `haiku`; observed model: `claude-haiku-5-5`. Per-call model IDs, usage, elapsed times and critic rationales are saved in each audit JSON. Model outputs can vary on repetition.
- Single sequential ten-audit run on a non-isolated CPU host; this is not a load-latency measurement. Python verification ran during the evaluation. All owned retrieval/model processes were stopped after completion.

## Remaining evidence limitations

The cloud comparison retrieved AMZN cover-page/index material rather than adequate cloud competition and infrastructure-cost disclosures. Both bounded rewrites failed; the answer explicitly flags that company-specific gap. Other AMZN prompts successfully retrieve sales, expenses and cash-flow material, so this does not establish absence of those disclosures from the corpus.

The recovered AMZN lease query obtains a maturity schedule, but the critic still notes that some supporting table excerpts are truncated. “Full” coverage means every planned subquery has at least one published supporting quote; it does not imply exhaustive disclosure coverage or expert audit approval.

MSFT 2024 remains item-unclassified in the historical source. Newer classified MSFT filings do not satisfy the exact 2024 Item 1A predicate. Parser coverage gaps in the broader ingestion corpus remain unchanged.

## Reproduce

With the previously acquired local indexes/models, Python dependencies and an authenticated Claude CLI:

```bash
export PYTHONPATH="$PWD/models/runtime:$PWD/py"
python3 scripts/evaluate-expanded-agent.py --out bench_kit/results/phase3-expanded-repeat
python3 bench_kit/verify_expanded_agent.py --directory bench_kit/results/phase3-expanded-repeat
```

The output path must be fresh. The supervisor builds the Go bridge, starts both services, verifies fingerprints, runs all ten prompts, captures health/logs, and stops the processes in a finally block. Build binaries are removed. No commits or pushes are performed.

## Evidence

- [Evaluation report](evaluation/report.json) · [terminal output](evaluation.log)
- [Independent checks, every hop/rationale and per-audit totals](independent-verification.json) · [verification log](independent-verification.log)
- [Enrolled source identities](agent-provenance.json) · [service health before](health-before.json) · [after](health-after.json)
- [All 39 Python tests passed](python-all-tests.log) · [13 targeted agent tests passed](python-tests.log)
- [Source fingerprints](source-sha256.json)

Earlier four-file results remain unchanged in `../phase3/`; their 54 citations and two deliberate missing-evidence cases belong to that historical corpus, not this run.
