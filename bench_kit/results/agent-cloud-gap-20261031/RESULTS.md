# AMZN cloud audit retrieval fix — October 31, 2026

The affected MSFT/AMZN filing-year-2024 cloud competition and infrastructure-cost audit now has published, source-verified evidence for **both company-specific subqueries**. **8/8 citations verify independently: four MSFT, four AMZN.** The live rerun uses **three retrieval calls, one retry**, takes **37.849243 s**, and has **no unresolved evidence-gap flags**. All **40 Python tests pass**.

| Same audit | Expanded-corpus baseline | Fixed affected-audit rerun |
|---|---:|---:|
| Published citations verified | 8/8, all MSFT | 8/8, four MSFT + four AMZN |
| Cited company subqueries | 1/2 | 2/2 |
| Retrieval calls | 4 | 3 |
| Retries | 2 | 1 |
| Elapsed seconds | 43.169065 | 37.849243 |
| Evidence-gap flags | AMZN below threshold after two retries | None |

These are single model-dependent runs, not statistically controlled latency comparisons. All ten prompts were **not** rerun after this follow-up; the earlier ten-case evidence remains intact. “Covered” means published supporting quotes for every planned subquery, not exhaustive financial audit correctness.

## Diagnosis

Replayed the three failed AMZN query strings through the same actual Go shard router → BM25/PQ128 → RRF → FP32 ONNX pipeline. [All six query replays with sparse/dense/fusion/rerank outputs](diagnostic/diagnostic.json).

- The original compound query retrieved useful expense material but ranked competition passage `000101872424000008#passage-000012` eighth after reranking, outside the agent’s five-passage context. Competition passage `...000006` was in the Top-50 candidates but outside reranked Top-10.
- The old critic then inserted accession numbers, filing identifiers, named competitors and market-share requirements not requested by the original audit. Those rewrites ranked cover pages and exhibit lists highly. Useful cost evidence from the first partial result was discarded on retry.
- An exploratory topical competition query places passages `...000006` and `...000012` first and second. Expense and competition evidence exists in the acquired filing; the gap was query/context handling, not absent SEC content. These diagnostic alternatives are not hard-coded into production retrieval.

## General agent change

The critic may now select **up to two useful source-verified passage IDs** from a partial result. The next hop combines those passages with fresh, deduplicated retrieval evidence, with **five passages total**. The combined context must pass the relevance threshold for the **original subquery goal** before any passage supports published claims. Each hop records carried IDs, and the independent verifier checks their exact identity against the preceding hop. Unknown/duplicate retained IDs fail explicitly. Carryover is per subquery, so filing filters cannot mix.

Planner queries remain concise and topical. Critic rewrites target the missing facet, keep filing identifiers in immutable filters, and avoid inventing named-competitor, market-share or numerical requirements. No company-specific query templates, relevance thresholds, source files, vector indexes, reranker weights or hybrid ranking algorithms changed.

## Actual successful retry

The real model initially searched `cloud business competition and cloud infrastructure operating costs` for each company. MSFT scored 4 and passed. AMZN scored 3: cost evidence was useful but competition evidence was missing.

The AMZN retry carried original passages `...000064` and `...000151`, then searched `AWS competitive landscape and competitors in cloud computing services`. The five-item combined context scored 4. Final AMZN citations include:

- `...000012`: competition from web and infrastructure computing services.
- `...000005`: AWS services and enterprise customers.
- `...000064`: AWS operating income and spending on technology infrastructure.
- `...000151`: AWS segment sales, operating expenses and operating income table.

[Full published answer](affected-audit/evaluation/audit02.md) · [complete plan/hops/grades/rationales](affected-audit/evaluation/audit02.json).

## Bounds, checks and limitations

- Unchanged: four inference workers × two threads, Go admission limits, at most three parallel subqueries, two concurrent Claude calls, maximum two retries per subquery. Retained context remains five passages; at most two are carried between hops.
- All eight citations match accepted evidence, exact original passage text, metadata, accession, URL and inline source identities. The independent replay also checks source-verified retained passage continuity.
- Final sidecar admission is drained with zero rejected requests; all owned retrieval processes stopped. This is an audit run, not a 50-client load test.
- Source-supported competition disclosures remain general; the excerpts do not establish named AWS competitors or an AWS-specific infrastructure expense breakdown. No such unsupported conclusions are generated. A model relevance score and exact-source checks do not replace expert semantic judgments.
- Other financial audit prompts and broad retrieval nDCG were not remeasured after this agent-only change. Earlier results retain their original scope.

## Reproduce and evidence

```bash
export PYTHONPATH="$PWD/models/runtime:$PWD/py"
python3 scripts/evaluate-expanded-agent.py --out bench_kit/results/cloud-audit-repeat --audit-id audit02
python3 bench_kit/verify_expanded_agent.py --directory bench_kit/results/cloud-audit-repeat
```

Use a fresh output directory, the acquired expanded corpus/indexes, model snapshots and authenticated Claude CLI. Omit `--audit-id` to rerun all ten prompts. Add `--diagnose-only` to replay the saved failed queries and exploratory topical alternatives instead of invoking the agent.

[Evaluation report](affected-audit/evaluation/report.json) · [evaluation terminal log](affected-audit/evaluation.log) · [independent checks](affected-audit/independent-verification.json) · [health after](affected-audit/health-after.json) · [40-test verification log](python-tests.log) · [source hashes](source-sha256.json).

Original baseline: [expanded audit 02](../phase3-expanded-20261031/evaluation/audit02.json). No commits or pushes were made.
