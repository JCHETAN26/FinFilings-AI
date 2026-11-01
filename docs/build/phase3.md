# Phase 3 — bounded agent and grounded audit evaluation

Phase 3 implements an asynchronous `Plan → Retrieve_Evaluate → Synthesize` controller in `py/agent/`. Phases 1 and 2 remain available; there is no automatic transition to Phase 4 or UI work.

## Implementation

- `models.py` defines Pydantic contracts with unknown fields forbidden. The planner produces two or three targeted subqueries. Each has a ticker and filing year; optional section and accession constraints use AND semantics. The controller resolves accessions against the acquired SEC provenance catalog. A missing year remains missing. The catalog extracts the actual fiscal-period end from each verified cover page and passes it, with the original audit prompt, into planning and critique. This prevents confusing a 2024 filing with fiscal 2024: Amazon's acquired 2024 filing reports fiscal 2023.
- `provider.py` invokes the authenticated Claude CLI in JSON output mode with the Pydantic JSON schema. Actual inference uses the `haiku` alias; exact model IDs and token usage are recorded per call. It disables file/command tools, customizations, skills and MCP servers, and does not persist conversations. At most two model processes run concurrently. Calls have a 180-second deadline and 128-KiB input/256-KiB output bounds; timeout/cancellation terminates the process group. There is no heuristic model substitute. CLI-reported dollar amounts are list-price estimates, not proof of subscription charges.
- The controller bounds parallel subqueries to three and retries to **two per subquery**, for at most nine retrieval calls. A real structured critic grades relevance 1–5; the default acceptance threshold is 4. Below-threshold queries receive model-generated rewrites. A rewrite cannot change the original filing filters; a repeated unchanged query is an error. Empty evidence cannot receive a passing score. Retrieval/transport/model errors propagate instead of being treated as low relevance.
- `pkg/index.SearchFiltered` compares metadata directly against borrowed mmap bytes **before Top-K admission**, under the existing mapping lifetime lock. Its predicate allocates no strings per candidate. Scored result metadata is still copied for safe ownership. Existing unfiltered scoring and corpus IDF are preserved.
- The Go bridge sends the same predicates to PQ8 and validates reranked metadata. FAISS IndexPQ does not accept ID selectors in this version. The dense filter streams matching row IDs from SQLite in blocks of 2,048, copies only their compressed codes, uses the original trained centroids, searches each block, and merges a bounded Top-K heap. It does not materialize raw vectors or retain a corpus-sized document list. Tests compare filtered results with filtering the complete real PQ ranking, including the block boundary.
- `provenance.py` verifies the original SEC corpus SHA-256, accession/CIK/source URL identity, exact stored metadata and exact passage text. The source corpus must remain immutable during a run. The original run enrolled four acquired SEC filings; the expanded revalidation below enrolls 152 trusted sources. FinQA passages have no verified SEC accession and cannot support published financial claims.
- `synthesizer.py` asks the model to select verbatim supporting excerpts. Every proposed quote must match an accepted retrieved SEC passage, allowing whitespace normalization only. Unknown IDs and altered/fabricated quotes are dropped and flagged. Markdown escapes evidence formatting and attaches a generated citation tag, SEC filing URL, exact chunk ID and dashed accession to every published excerpt. This conservative extractive synthesis does not generate unverified paraphrases, arithmetic or comparative conclusions.
- Missing subqueries and evidence omitted from synthesis are explicitly flagged. Original SEC chunks are item-unclassified, so the system does not invent Item 1A tags. Exact Item 1A filters correctly return no verified evidence.

## Reproduce

Use the Phase 2 models/indexes and authenticated `claude` CLI. Normal clean-venv setup is documented in Phase 2. On this SSD, symlinks are unsupported, so the verified continuation used an isolated `pip --target models/runtime` installation and the host PyTorch. `models/` is ignored. The model lock was refreshed to the persistent cache without changing model revisions or weight hashes. Install requirements in a clean compatible environment rather than changing the host Python packages.

```bash
# From the repository root, with Python dependencies installed:
export PYTHONPATH="$PWD/models/runtime:$PWD/py"
export HF_HOME="$PWD/models/hf-cache"
export OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=4
export GOCACHE=/tmp/finfilings-go-cache
export CCACHE_DIR=/tmp/finfilings-ccache CCACHE_TEMPDIR=/tmp/finfilings-ccache-tmp
mkdir -p models/bin
go build -o models/bin/finfilings ./cmd/finfilings
python3 scripts/verify-phase3.py
```

The supervisor checks both services' health and matching corpus fingerprints before starting all ten prompts. It owns their lifetimes, saves service/evaluation logs, creates a fresh timestamped evaluation directory, and stops services on completion. `FINFILINGS_GO_BINARY` overrides its binary path. `bench_kit/results/phase3/latest-run.json` records the latest evaluation directory.

For an individual audit, start the Go and Python services as documented for Phase 2, then:

```bash
PYTHONPATH=py python3 -m agent.state_machine \
  'Compare AAPL filing year 2023 and NVDA filing year 2024 supply-chain risks.' --json
```

Use the appropriate environment's Python and include `models/runtime` in PYTHONPATH if using this isolated target installation.

## Verification and interpretation

`bench_kit/eval_agent.py` contains ten explicit prompts: eight available-filing audits and two deliberate evidence-gap cases (AAPL filing year 2025 and an exact unclassified MSFT Item 1A filter). It records the complete plan, per-hop queries, immutable filters, relevance grades/rationales, retries, accepted evidence, published quotes, citations, model calls, elapsed times and flags. Each case checks:

1. Two or three subqueries and the required state order.
2. Requested filing scope and section constraints are preserved.
3. At most two retries, with no retry after acceptance; negative cases exercise both retries.
4. Every citation uses accepted evidence and exactly matches its SEC quote, chunk ID, accession, generated tag and source URL.
5. Negative cases publish no financial claims and explicitly flag missing evidence.

A passing case means these execution/provenance checks passed. It is **not a human-labeled answer correctness score**. Full subquery coverage and evidence gaps are reported separately; a partial audit can pass the safety checks. The eight positive prompts are not a representative production or statistical benchmark. Model outputs and timings can vary on repetition. No tenant authentication/isolation or production availability claim is made.

Go normal/race/purego/vet logs and Python tests are in `bench_kit/results/phase3/`. The Python tests include corrupted provenance, altered content, wrong scopes, unknown accessions, unsupported claims, markdown escaping, PQ filter correctness, model process timeout cleanup and admission-slot recovery. The Go fixture allocation benchmark is explicitly synthetic and measures implementation allocations, not real SEC search quality or production latency.

## Final measured run

All ten audits passed execution/filter/retry/source/citation checks and an independent replay of those checks. There were **54 verified quote citations**, **32 retrieval calls**, and **12 total retries** (maximum two per subquery). Median full-audit time was **23.338 seconds**. All eight positive prompts produced grounded excerpts; six had every subquery linked to published evidence, while two had explicit AMZN retrieval gaps. Both deliberate unavailable-evidence tests correctly withheld all claims.

The two partial cases concerned AMZN cloud competition/infrastructure costs and operating-expense detail. Their rewrites retrieved irrelevant passages and exhausted the critic budget. These are retrieval gaps, not evidence that the filing lacks the information. The corrected date context correctly distinguishes Amazon's 2024 filing from its fiscal 2023 reporting period.

- [All ten prompts, results, retries and gap rationales](../../bench_kit/results/phase3/RESULTS.md)
- [Structured final report](../../bench_kit/results/phase3/evaluation-20261031T061549Z/report.json)
- [Independent citation verification](../../bench_kit/results/phase3/citation-checks.json)
- [Full evaluation log](../../bench_kit/results/phase3/evaluation-final.log)

The final run used real `claude-haiku-5-5` structured outputs and the original pinned BGE/MiniLM weights. Sixteen Python tests, normal/race/purego Go tests and go vet passed. Preliminary interrupted/failed attempts are retained under `bench_kit/results/phase3/attempts/` and are not included in the final metrics.

## Expanded-corpus follow-up

The ten original prompts have been rerun on the frozen 49,452-passage corpus through the shard-aware hybrid service. The provenance registry now verifies section-tagged IDs through disk-backed passage offsets and includes the section in citation tags. Source enrollment is checksum-linked to the original SEC provenance and parser-v2 acquisition report. Availability expectations come from actual source/section presence: AAPL 2025 is available; MSFT 2024 Item 1A remains unavailable.

10/10 structural cases pass; 61/61 published citations independently verify. Eight audits have every planned subquery cited, one is partial, one abstains. There are seven retries across 28 retrievals; median audit time 22.697032 seconds. These regression prompts mostly retain their original filing scopes, so they do not measure representative semantic accuracy over all new companies. [Complete results and reproduction](../../bench_kit/results/phase3-expanded-20261031/RESULTS.md).

## AMZN cloud gap follow-up

The critic now optionally retains up to two useful source-verified passages from a partial result while its rewrite targets missing information. Carried and fresh evidence share the existing five-passage context cap, immutable filters and two-retry limit. Unknown/duplicate retained IDs are errors; traces record carried IDs, and the independent verifier checks continuity against the preceding hop. Rewrites stay topical rather than adding filing identifiers or unsupported named-competitor requirements. No global hybrid ranking/index changes were made.

The affected MSFT/AMZN audit rerun now publishes eight independently verified citations, four per company, after one AMZN retry. Both subqueries have published support and no unresolved flags. All 40 Python tests pass. Other audits were not rerun after this follow-up. [Evidence and reproduction](../../bench_kit/results/agent-cloud-gap-20261031/RESULTS.md).

## Broad validation suite

`bench_kit/suites/broad-validation-v1.json` (SHA-256 `0e8c02d3…ed4fae`, never edited after freezing) holds 30 cases: the ten historical prompts verbatim, 16 positive cases over 25 further companies, and four negative/mixed-availability cases (absent filing year, absent exact section, two mixed available/unavailable comparisons). Availability is checked per requested scope before inference; anchors are reviewer evidence only and are never passed to the agent. `bench_kit/verify_broad_agent.py` independently re-verifies exact case membership, source bytes, citations, bounds, carried-evidence continuity, mixed-availability behaviour, evaluator-summary agreement and recomputed index/model/code hashes.

Live run: **30/30 cases verified** (zero transport/model/structural failures): **26 supported, 1 partial gap, 3 expected-unavailable**; **172** independently verified published citations, 86 retrieval calls, 24 retries, median **23.64 s**, 143 Claude calls (`claude-haiku-5-5`). "Supported" means every planned subquery on an *available* scope has a verified published quote; it does **not** mean 26 complete answers. Both mixed cases have their available side fully cited (2/2, 1/1) with the unavailable scope withheld; available-scope coverage is **53/54 = 98.1481 %**. The one gap is HPE filing year 2024 (stayed at critic score 3 through two retries; the amount and remaining authorization sit in adjacent passages 35/36 split mid-sentence, so this is not evidence the disclosure is absent). One TSLA model-proposed quote was rejected by exact-quote grounding. The suite is ten historical prompts plus twenty source-informed regression fixtures over 30 companies with citations; it is not independent human gold, not a semantic accuracy score, and not load latency. [Results, per-case answers/traces, limitations and release gates](../../bench_kit/results/broad-validation-20261101/RESULTS.md).
