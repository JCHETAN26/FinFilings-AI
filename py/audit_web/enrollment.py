"""Provenance enrollment for the workbench launcher.

Performs the same checks as scripts/evaluate-expanded-agent.py (report/corpus/original-corpus checksums, ingestion
report lineage) and builds the identical trusted-source list under an explicit enrollment mode. Legacy enrollment keeps the
original-plus-expanded union; complete_report uses the retained all-v3 report after original-identity checks. To avoid a second, weaker implementation it reuses
`bench_kit/agent_suite.build_trusted_sources` and `check_report_lineage`, loaded by path (they are pure standard
library helpers). This is the only place product code touches the benchmark helpers.
"""
import importlib.util
import json
from pathlib import Path
from agent.provenance import digest


def _agent_suite(root: Path):
    spec = importlib.util.spec_from_file_location('finfilings_agent_suite', root / 'bench_kit' / 'agent_suite.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def enroll(*, root: Path, corpus_dir: Path, ingest_report: Path, original_provenance: Path, source_enrollment: str = 'legacy') -> dict:
    """Return {manifest, corpus, corpus_sha256, filings, lineage}. Raises ValueError on any mismatch."""
    root = Path(root).resolve()
    suite = _agent_suite(root)
    meta = json.loads((corpus_dir / 'provenance.json').read_text())
    corpus = corpus_dir / 'chunks.jsonl'
    if digest(ingest_report) != meta['new_sec_report_sha256'] or digest(corpus) != meta['corpus_sha256']:
        raise ValueError('expanded corpus/report checksum mismatch')
    baseline = json.loads(original_provenance.read_text())
    if digest(root / 'data/sec/chunks.jsonl') != baseline['corpus_sha256'] or baseline['corpus_sha256'] != meta['sec_sha256']:
        raise ValueError('original SEC provenance checksum mismatch')
    lineage, problems = suite.check_report_lineage(ingest_report, root)
    if problems:
        raise ValueError('ingestion report lineage invalid: ' + '; '.join(problems))
    sources = suite.build_trusted_sources(original_provenance, ingest_report, mode=source_enrollment)
    manifest = dict(corpus_sha256=meta['corpus_sha256'], sources=sources, source_enrollment_mode=source_enrollment,
                    source_manifests=[dict(path=str(p), sha256=digest(p)) for p in (original_provenance, ingest_report)])
    return dict(manifest=manifest, corpus=corpus, corpus_sha256=meta['corpus_sha256'], filings=len(sources), lineage=lineage)
