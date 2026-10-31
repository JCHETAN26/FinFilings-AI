# Ingestion fixes and shard-backed hybrid verification

Verified locally on 2026-10-31. Changes are uncommitted.

- Shared EDGAR header dispatch prevents waiting callers from bypassing Retry-After. Regression tests cover concurrent callers, cancelled admission, and cooldown after exhausted retries.
- Go parser accepts HTML paragraphs and plain-text lines above 64 KiB up to the 1 MiB bound; oversized inputs remain errors.
- Go metadata/mmap predicates and Python metadata/SQLite predicates support both legacy and section-tagged accession IDs, with exact accession boundaries.
- Hybrid HTTP serving recognizes quarter-shard roots and retains single-index support. Integration tests exercise actual mmap shard search, HTTP dense/rerank protocol fixtures, RRF, accession filtering and rejection of mismatched corpus fingerprints. Fixture scores are not vector-quality measurements.
- New Go ingestion outputs fingerprint the exact passage stream. Build vector artifacts from that same `passages.jsonl` and serve `-index <output>/segments`.

## Checks

| Check | Outcome | Evidence |
|---|---|---|
| `go test -race -count=1 ./...` | PASS, no reported races | [log](go-race.log) |
| Python unittest discovery | 25 tests PASS | [log](python-tests.log) |
| `go test -tags=purego ./pkg/index ./pkg/ingest` | PASS | [log](go-purego.log) |
| `go vet ./...` | PASS (empty log) | [log](go-vet.log) |
| Four real filings replayed under race detector | PASS | [log](real-replay.log), [report](real-replay-report.json) |

Real replay: **443 passages, 3 quarter shards, 9 binary segments**, identical to the Python parser for all four filings; **0** segment mappings remained after close. The replay uses cached SEC bytes through localhost fixtures, not live SEC requests. No new retrieval-quality or production latency claims are made. Actual FAISS filtering is covered by the Python suite using existing Phase 2 PQ artifacts; the new HTTP protocol integration uses explicitly labeled fixtures.

Serving instructions and limitations: [EDGAR ingestion documentation](../../../../docs/build/edgar-ingest.md).
