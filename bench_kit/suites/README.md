# Broad agent-validation suite `broad-validation-v1`

Frozen 30-case suite for the Plan → Retrieve_Evaluate → Synthesize agent over the existing 49,452-passage corpus
(corpus SHA-256 `9e7267d5…6c72`), 152 trusted SEC filings, sparse shard router, PQ128 and FP32 ONNX reranker.
Suite SHA-256: see `broad-validation-v1.sha256` (the suite file is never overwritten; the builder refuses to).

**What it is:** source-informed regression fixtures. Prompts `audit01`–`audit10` are the historical regression prompts
verbatim (checked against the saved real run). `broad11`–`broad30` were written after reading the passages recorded as
anchors. **What it is not:** independent human gold, an answer-accuracy score, a retrieval-quality metric or a load test.
The first live run is documented in [`../results/broad-validation-20261101/RESULTS.md`](../results/broad-validation-20261101/RESULTS.md).

| Group | Cases | Notes |
|---|---|---|
| Regression | `audit01`–`audit10` | verbatim; AAPL/MSFT/AMZN/NVDA |
| Positive | `broad11`–`broad26` | 16 cases covering 25 companies, none of AAPL/MSFT/AMZN/NVDA (ADBE, AFL, AMD, BAC, COP, CRM, CVS, HD, HPE, ISRG, JNJ, KO, LLY, MET, MRK, OXY, PFE, PG, SBUX, TGT, TSLA, TXN, UNH, V, WMT; GS appears in `broad29`): staples, pharma, banks, retail, auto, payments, energy, insurance, software, semiconductors, managed care, restaurants, med-tech, hardware |
| Absent filing year | `broad27` (TSLA 2024) | filing was never enrolled (not an ingest-skipped filing) |
| Absent exact section | `broad28` (NVDA 2024 "Item 7") | filing exists but is unclassified legacy text |
| Mixed availability | `broad29` (GS 2026 ✓ / BAC 2025 ✗), `broad30` (KO 2025 "Item 7" ✓ / AMZN 2024 "Item 7" ✗) | per-scope expectations |

Multi-company comparisons among the 20 new cases: 11 (`broad11/12/17/18/19/20/21/24/26/29/30`); different filing years in one case:
`broad16/21/22/25/29/30`; exact filters: Item 1A (`broad12`, `16`), Item 7 (`broad11/13/17/21/22/23/24/25/28/30`), Item 8 (`broad19`, `26`).

## Contract

* **Availability** (`expected_scopes`) means the filing year and exact section exist in the enrolled corpus. It never
  asserts a topic is answerable. Positive-case outcomes are reported as `supported`, `partial_gap` or `abstained`; these
  are honest results, not failures. `expected_unavailable` is the required outcome for an unavailable scope.
* **Failures** are separate: `transport` (retrieval service/network), `model` (Claude CLI), `structural` (agent contract
  or in-process check). Classification uses the innermost agent frame of the traceback; the full type, message and
  traceback are saved in `<id>.failure.json`. A failed case never stops later cases.
* **Anchors** (`reviewer_evidence`) are passage IDs + excerpts + passage hashes for reviewers. The agent receives the
  prompt string only (`agent_suite.agent_view`); tests check anchors do not appear in any model payload or prompt. The
  verifier reports anchors retrieved/cited as information, never as a requirement.
* **Unchanged bounds:** 2–3 subqueries, ≤2 retries/subquery, ≤5 context passages, ≤2 carried, ≤3 concurrent subqueries,
  ≤2 concurrent Claude calls, 4 inference workers × 2 threads, Go admission/connection limits. No retrieval/model tuning.

## Commands

```bash
export PYTHONPATH="$PWD/models/runtime:$PWD/py" HF_HOME="$PWD/models/hf-cache" HF_HUB_OFFLINE=1 \
  OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=2 GOCACHE=/tmp/finfilings-go-cache
# unit/contract checks (no live inference)
python3 -m unittest bench_kit.test_broad_agent_suite
python3 -m unittest discover -s py -p 'test_*.py'
(cd bench_kit/suites && sha256sum -c broad-validation-v1.sha256)
# full live run: fresh --out; the suite hash is pinned
SUITE_SHA=$(cut -d' ' -f1 bench_kit/suites/broad-validation-v1.sha256)
python3 scripts/evaluate-expanded-agent.py --out bench_kit/results/broad-validation-RUN \
  --suite bench_kit/suites/broad-validation-v1.json --expected-suite-sha256 "$SUITE_SHA"
# independent offline verification (exit 0 only if all 30 are verified)
python3 bench_kit/verify_broad_agent.py --directory bench_kit/results/broad-validation-RUN --enforce-gates
# add --strict-code to also fail on code changed since the run (default: drift is reported, not failed, for historical replay)
```

`--suite-case ID` (repeatable) runs a deliberate subset; verify it with `--allow-subset`.

## How omissions are caught

The verifier loads the suite by pinned hash (not from the run), takes the expected 30 IDs from it, and requires each
ID to have exactly one outcome in `evaluation/`: `<id>.json` + `<id>.md`, or `<id>.failure.json` (agent-run failures
only; a failed in-process check keeps its result). Missing → `omitted`; half-written → `incomplete_files`; extra files
→ a global problem; a manifest listing fewer than 30 cases → a problem unless `--allow-subset`. `report.json` is only
compared afterwards and every disagreement is listed; it cannot make an omitted case pass.


## Release gates (set before any live score)

Set by the manager before inference. Nothing is pushed unless all pass; otherwise fix reproducible issues, **preserve the failed
attempt**, and run a fresh *versioned* evaluation without deleting failures or cherry-picking cases.

1. Full suite and checksums frozen before inference; all 30 outcomes retained, including failures.
2. No missing cases, transport/model exceptions, citation/provenance violations, filter leakage, retry/context/concurrency
   bound violations or fabricated claims.
3. All unavailable scopes publish zero claims; mixed cases preserve the unavailable scope **and** produce supported
   available-scope evidence.
4. At least 80% of planned subqueries on available scopes have independently verified published-citation coverage (a structural
   threshold, not semantic accuracy). All partials and flags are reported.
5. Complete Python suite and applicable Go race/vet checks pass.
6. The public snapshot contains no local model weights, datasets, credentials or private contact information.

`verify_broad_agent.py` computes gates 1–4 under `release_gates` (`--enforce-gates` makes them the exit code). Gates 5–6, and the
live-run exceptions it cannot see, are listed in `not_checked_by_this_tool` and must be checked separately. Gate 4 uses the
independently computed `available_subqueries` / `cited_available_subqueries` (per case and aggregate). A mixed case reports
`mixed_result` (`mixed_fully_supported` or `mixed_available_side_gap`) and `unavailable_withheld` separately: an honest
available-side gap is a structural pass (exit 0 without `--enforce-gates`) but is not a successful mixed case and fails gate 3.

### What `passed` means

`passed` requires every frozen case verified **and** no evaluator-summary discrepancy. The verifier recomputes each case's
reported metrics (`subqueries`, `retrieval_calls`, `retries`, `accepted_subqueries`, `cited_subqueries`, `citations`,
`complete_coverage`, `negative`, `evidence_outcome`, `unavailable_subqueries`, `flags`, `model_calls`, `seconds`) and the
aggregate counts, and checks that `report.json` and `records.jsonl` each list exactly the frozen selection with no duplicate or
missing IDs and agree with each other. A missing ledger is a failure.

### Artifact hashes: recorded vs. verified

`artifact-hashes.json` records paths relative to the repository root (`--root`). The verifier **re-hashes those paths now**;
`artifact_verification` states `recomputed_verified` counts, never mere presence. The vector index, reranker model and sparse
index are immutable: a changed, missing or unrecorded file (or an unsafe path) fails the run, as do changed suite, corpus, ingestion
report or original-provenance hashes. Code hashes (sidecar `py/*.py`, `py/agent`, the Go `cmd/` and `pkg/` stack, `go.mod`, the
harness scripts) must cover the required implementation files, but **drift since the run is reported separately** under
`artifact_verification.code` and fails only with `--strict-code`. Third-party packages in `models/runtime` and the raw HTML of
the parser-v2 filings are not hashed.

## Known limitations

* Fixtures are source-informed, single-reviewer, and from one corpus snapshot; there is no human relevance label.
* Raw HTML for the 148 parser-v2 filings is not stored locally. "Original bytes" therefore means the corpus passage
  bytes (verified against the corpus hash); raw-document identity rests on the ingestion report's `sha256`/URL, which
  the verifier re-derives but cannot re-hash. The four original filings do have local HTML.
* The saved traces do not record start times, so the verifier cannot prove the ≤2 concurrent Claude calls or ≤3
  concurrent subqueries; it checks the model-call ledger (1 Plan, 1 Critique per hop, ≤1 Draft) instead.
* Gates 5–6 are manual. The suite is saved before inference by the supervisor and the evaluator; the verifier compares file times as a weak
  extra check only.
* Model outputs vary between runs; one run is a sample, not a distribution.

## Parser coverage gaps found while reading passages (not fixed; awaiting review)

Evidence from `indexes/sec-scale-v2-20261031/report.json` and the corpus:

1. **Item 8 cross-reference stubs.** NVDA 2025/2026, EOG 2025/2026 and REGN 2026 have an `Item 8` of 29–36 words
   (e.g. NVDA: "The information required by this Item is set forth in our Consolidated Financial Statements … included in
   this Annual Report"). The statements themselves are not in any section: NVDA 2026 has no cash-flow-statement or
   notes passage, only MD&A summary text (`…#item7#passage-000015`). `exact Item 8` queries for these filings return one
   stub passage. 5 of 148 ingested filings are affected.
2. **43 filings skipped entirely** (e.g. JPM, ORCL, PEP, COST, IBM, INTC) because a major section "was not found with ≥128
   words": 24 for Item 8, 10 for Item 7, 8 for Item 1A. These are absent from the corpus because of the parser, not because the
   company lacks a filing, so they are deliberately not used as "absent" negatives.
3. **Legacy unclassified filings:** AAPL 2023, MSFT 2024, AMZN 2024, NVDA 2024 have only `Full filing (item not
   classified)`, so exact Item filters cannot match them (used as the `broad28`/`broad30` absent-section cases).

**Recommended next step (supersedes the earlier draft):** do **not** silently extend a short `Item 8` into Item 15/F-pages or
relabel reference content under another item: a section label must keep the identity the filing gives it, and an Item 8 stub
that only incorporates statements by reference should stay a short Item 8 (with the gap documented), not become a place
where other items' text is filed. The next implementation step is instead to investigate the already documented Morgan Stanley
(`MS`, not `MSFT`) titled-heading MD&A truncation, where the titled Item 7 stops at a "Cybersecurity" subheading inside MD&A
and about 3,000 MD&A words are not emitted (`docs/build/edgar-ingest.md`). That investigation must verify against the raw
source document, produce fresh replay artifacts, and leave the current frozen corpus, indexes and `broad-validation-v1`
untouched; any corrected corpus is a new version with its own new suite version. The Item 8 stub and skipped-filing gaps above
remain documented, unfixed findings.

## `broad-validation-v2` (parser-v3 corpus)

Frozen 2026-11-01 for the mixed-lineage corpus `data/sec-scale-v3-evaluation-20261101` (49,406 passages; only Morgan Stanley reparsed with
parser v3). Built by `python3 bench_kit/build_broad_suite.py --derive-from bench_kit/suites/broad-validation-v1.json --suite-id broad-validation-v2 --corpus … --ingest-report … --output bench_kit/suites/broad-validation-v2.json`.
Every case's `id`, `kind`, `prompt`, `scopes`, `section`, `expected_scopes` (availability and passage counts, including the negative and mixed
expectations) and `reviewer_evidence` is carried over **unchanged**; the builder recomputes availability and each anchor's passage hash
against the new corpus and refuses to write the suite if anything differs, and refuses to overwrite. Only `suite_id`, `frozen_on`, `label`,
`derived_from`, `corpus_sha256` change. Anchors remain reviewer information and are never agent input. `broad-validation-v1` and its
evidence are untouched. The first v2 run (`agent-run-01`) is complete: 30/30 verified under the strict verifier, 26 supported / 1 partial (HPE 2024) / 3 expected-unavailable, 51/52 available-scope subqueries cited ([all 30 rows](../results/parser-v3-adoption-20261101/RESULTS.md)); see [`docs/build/parser-v3-adoption.md`](../../docs/build/parser-v3-adoption.md).
