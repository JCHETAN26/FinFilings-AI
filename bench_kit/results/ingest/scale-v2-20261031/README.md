# Live re-ingestion with parser v2 (2026-10-31)

Same 100-company cohort and options as `../scale-20261031/` (5 requests/second, up to two newest 10-Ks per company, unparseable filings recorded as skipped). The parser was `deterministic-canonical-heading-v2-go`. EDGAR returned the identical 191 accessions, and every re-ingested filing's bytes matched the first run's SHA-256.

| | v1 run | v2 run |
|---|---:|---:|
| Accepted filings | 138 | 148 |
| Rejected filings | 53 | 43 |
| Companies with at least one filing | 71 | 77 |
| Section-tagged passages | 26,412 | 29,941 |
| Item 1A / 7 / 8 passages | 6,846 / 6,487 / 13,079 | 7,538 / 7,564 / 14,839 |
| Quarter shards | 9 | 9 |

- **Gained (10):** BAC, MS (titled fallback), AXP ×2, PNC ×2, COF ×2, CVS ×2.
- **Lost:** 0.
- **Changed (2, both AFL):** boundary corrections of a few words, described in `../parser-v2-20261031/`.

`verification.log` shows these checks:
- all 29,941 passage IDs are unique and tagged Item 1A/7/8;
- the quarter-shard router opens all 9 shards with 29,941 documents;
- ticker+section-filtered searches return only that ticker and section.

The output is in `indexes/sec-scale-v2-20261031/` (gitignored). The expanded benchmark corpus and vectors in `../scale-20261031/` were built from the v1 output and have not been rebuilt.
