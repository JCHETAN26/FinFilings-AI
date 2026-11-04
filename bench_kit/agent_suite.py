"""Frozen broad agent-validation suite: loading, scope availability and failure taxonomy.

Pure standard-library helpers shared by the evaluator, the suite builder and the
offline verifier. Nothing here talks to a model or a retrieval service.

The suite is a set of *source-informed regression fixtures*. Anchor passages are
reviewer evidence recorded after reading actual filings; they are not independent
human gold labels, are never shown to the agent, and are not an answer-accuracy
measure.
"""
import collections
import hashlib
import json
from pathlib import Path
import re
import traceback
from typing import Any

SCHEMA = 'finfilings-agent-suite/v1'
COMPLETE_PARSER = 'deterministic-canonical-heading-v3-go'
ENROLLMENT_MODES = ('legacy', 'complete_report')
UNCLASSIFIED = 'Full filing (item not classified)'
EXACT_SECTIONS = ('Item 1A', 'Item 7', 'Item 8')
SECTION_COMPONENT = {'item1A': 'Item 1A', 'item7': 'Item 7', 'item8': 'Item 8', None: UNCLASSIFIED}
PASSAGE_ID = re.compile(r'(\d{18})#(?:(item(?:1A|7|8))#)?passage-(\d{6})')
MAX_RECORD_BYTES = 4 * 1024 * 1024
CASE_COUNT = 30
KIND_COUNTS = {'regression': 10, 'positive': 16, 'negative+mixed': 4}
REGRESSION_IDS = [f'audit{i:02}' for i in range(1, 11)]
BASELINE_COMPANIES = frozenset({'AAPL', 'MSFT', 'AMZN', 'NVDA'})
MIN_NEW_COMPANIES = 12
MAX_PROMPT_BYTES = 8192
# Agent-visible: the prompt only. Everything else in a case is evaluator/reviewer metadata.
AGENT_VISIBLE_FIELDS = ('id', 'prompt')
FAILURE_CATEGORIES = ('transport', 'model', 'structural')
EVIDENCE_OUTCOMES = ('supported', 'partial_gap', 'abstained', 'expected_unavailable')


class SuiteError(ValueError):
    """The frozen suite or its availability expectations are invalid."""


def sha256_file(path: Path) -> str:
    checksum = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            checksum.update(block)
    return checksum.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def normalize(text: str) -> str:
    return ' '.join(text.split())


def build_trusted_sources(original_provenance: Path, ingest_report: Path, *, mode: str = 'legacy') -> list[dict[str, Any]]:
    """Explicit enrollment: historical original+expanded union, or complete pinned report.

    complete_report replaces original source identities only after matching their
    immutable filing metadata. Current raw hashes come from the retained replay;
    historical provenance is kept as an input, never silently deduplicated.
    """
    if mode not in ENROLLMENT_MODES:
        raise ValueError('unknown source enrollment mode')
    originals = json.loads(Path(original_provenance).read_text())['sources']
    report = json.loads(Path(ingest_report).read_text())
    sources = []
    seen = set()
    for filing in report['filings']:
        ref, download = filing['ref'], filing['download']
        accession = ref['Accession'].replace('-', '')
        if accession in seen:
            raise ValueError('duplicate ingestion accession: ' + accession)
        seen.add(accession)
        sources.append(dict(ticker=ref['Ticker'], cik=ref['CIK'], filing_year=int(ref['FilingDate'][:4]),
            form=ref['Form'], url=download['url'], accession=accession,
            raw_sha256=download['sha256'], fiscal_period_end=ref.get('ReportDate', '')))
    if not 1 <= len(sources) <= 10000 or len({s['accession'] for s in originals}) != len(originals):
        raise ValueError('invalid source enrollment size or duplicate originals')
    by_accession = {s['accession']: s for s in sources}
    overlap = set(by_accession) & {s['accession'] for s in originals}
    if mode == 'legacy':
        if overlap:
            raise ValueError('overlapping original/report sources require explicit complete_report enrollment')
        return list(originals) + sources
    validate_complete_report(ingest_report)
    if overlap != {s['accession'] for s in originals}:
        raise ValueError('complete report omits original filings')
    for old in originals:
        current = by_accession[old['accession']]
        for field in ('ticker', 'cik', 'filing_year', 'form', 'url'):
            if current[field] != old[field]:
                raise ValueError('replacement filing identity changed: ' + old['accession'] + '/' + field)
    # Preserve the historical catalog order while taking EVERY identity from the new report.
    original_ids = {s['accession'] for s in originals}
    return [by_accession[s['accession']] for s in originals] + [s for s in sources if s['accession'] not in original_ids]


def validate_complete_report(path: Path) -> dict[str, Any]:
    """Check complete replay integrity from retained raw bytes; no network/model calls."""
    path = Path(path)
    report = json.loads(path.read_text())
    if (report.get('version') != 1 or report.get('parser') != COMPLETE_PARSER
            or report.get('raw_retained') is not True or report.get('skipped')
            or not 1 <= len(report.get('filings', [])) <= 10000):
        raise ValueError('complete enrollment requires a retained, unskipped parser-v3 replay')
    seen = set()
    sections = collections.Counter()
    for filing in report['filings']:
        ref, download, parsed = filing['ref'], filing['download'], filing['parse']
        accession = ref['Accession']
        if not re.fullmatch(r'\d{18}', accession) or accession in seen:
            raise ValueError('invalid/duplicate complete-report accession')
        seen.add(accession)
        if not re.fullmatch(r'\d{1,10}', ref['CIK']) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', ref['PrimaryDocument']):
            raise ValueError('invalid complete-report CIK/document')
        expected_url = f"https://www.sec.gov/Archives/edgar/data/{ref['CIK']}/{accession}/{ref['PrimaryDocument']}"
        raw = path.parent / 'raw' / (accession + '.raw')
        if (download['url'] != expected_url or not re.fullmatch(r'[0-9a-f]{64}', download['sha256'])
                or parsed['parser'] != COMPLETE_PARSER or parsed['raw_sha256'] != download['sha256']
                or parsed['raw_bytes'] != download['bytes'] or raw.is_symlink() or raw.parent.is_symlink() or raw.stat().st_size != download['bytes']
                or sha256_file(raw) != download['sha256']):
            raise ValueError('complete-report raw/source identity mismatch: ' + accession)
        if set(parsed['sections']) != set(EXACT_SECTIONS) or any(v['chunks'] <= 0 for v in parsed['sections'].values()):
            raise ValueError('complete report lacks a supported major section')
        if parsed['chunks'] != sum(v['chunks'] for v in parsed['sections'].values()):
            raise ValueError('complete-report filing count mismatch')
        sections.update({k:v['chunks'] for k,v in parsed['sections'].items()})
    if report['chunks'] != sum(sections.values()) or report['sections'] != dict(sections):
        raise ValueError('complete-report totals mismatch')
    return dict(mode='complete_retained_raw', parsers={COMPLETE_PARSER:len(seen)}, retained_raw_files_verified=len(seen))


LINEAGE_SCHEMA = 'finfilings-ingest-lineage/v1'


def check_report_lineage(report_path: Path, root: Path) -> tuple[dict[str, Any], list[str]]:
    """Check an ingestion report's declared lineage against the previous report it names. Returns (info, problems).

    A report without a `lineage` block must be single-parser; mixed parsers that are not declared are a problem, so a
    partially reparsed corpus can never pass as a uniformly parsed one. With a lineage block the previous report is re-hashed,
    every non-targeted filing must equal its previous entry exactly, and each targeted filing must retain the previous raw
    identity in `raw_identity_history` and record its current identity in `download`/`parse`.
    """
    report_path, root = Path(report_path), Path(root)
    report = json.loads(report_path.read_text())
    parsers = collections.Counter(f['parse']['parser'] for f in report['filings'])
    lineage = report.get('lineage')
    if lineage is None and report.get('raw_retained') is True:
        return validate_complete_report(report_path), []
    if lineage is None:
        problems = [f'ingestion report mixes parsers {dict(parsers)} without a lineage declaration'] if len(parsers) > 1 else []
        return dict(mode='single_parser', parsers=dict(parsers)), problems
    problems: list[str] = []

    def need(condition: bool, message: str) -> bool:
        if not condition:
            problems.append(message)
        return bool(condition)

    info: dict[str, Any] = dict(mode=lineage.get('kind'), parsers=dict(parsers), reparsed_filings=lineage.get('reparsed_filings'),
        untouched_filings=lineage.get('untouched_filings'))
    need(lineage.get('schema') == LINEAGE_SCHEMA and lineage.get('kind') == 'targeted_replacement', 'unsupported lineage schema/kind')
    previous = lineage.get('previous') or {}
    relative = previous.get('report_path', '')
    previous_path = (root / relative).resolve() if relative and not Path(relative).is_absolute() else None
    if previous_path is None or not previous_path.is_relative_to(root.resolve()) or not previous_path.is_file():
        need(False, 'lineage previous report path is unsafe or missing')
        return info, problems
    need(sha256_file(previous_path) == previous.get('report_sha256'), 'previous ingestion report differs from the lineage hash')
    old = json.loads(previous_path.read_text())
    targets = {t['accession'] for t in lineage.get('targeted_filings', [])}
    need(bool(targets) and lineage.get('reparsed_filings') == len(targets), 'lineage reparsed_filings disagrees with targeted_filings')
    old_by = {f['ref']['Accession']: f for f in old['filings']}
    new_by = {f['ref']['Accession']: f for f in report['filings']}
    need(list(old_by) == list(new_by) and len(new_by) == len(report['filings']), 'filing set/order differs from the previous report')
    need(lineage.get('untouched_filings') == len(new_by) - len(targets), 'lineage untouched_filings disagrees with the report')
    need(report.get('skipped') == old.get('skipped'), 'skipped filings differ from the previous report')
    for accession, entry in new_by.items():
        before = old_by.get(accession)
        if before is None:
            continue
        if accession not in targets:
            need(entry == before, f'untouched filing {accession} differs from the previous report')
            continue
        history = entry.get('raw_identity_history') or []
        need(len(history) >= 2, f'targeted filing {accession} lacks raw identity history')
        if len(history) >= 2:
            first, last = history[0], history[-1]
            need((first.get('url'), first.get('bytes'), first.get('sha256')) == (before['download']['url'], before['download']['bytes'], before['download']['sha256']),
                 f'targeted filing {accession} does not retain the previous raw identity')
            need((last.get('bytes'), last.get('sha256')) == (entry['download']['bytes'], entry['download']['sha256']) == (entry['parse']['raw_bytes'], entry['parse']['raw_sha256']),
                 f'targeted filing {accession} current raw identity is inconsistent')
        need(entry['ref'] == before['ref'] and entry['download']['url'] == before['download']['url'], f'targeted filing {accession} changed identity fields')
    need(dict(parsers) == report.get('parsers'), 'declared parser counts differ from the filings')
    totals = collections.Counter()
    for filing in report['filings']:
        for name, value in filing['parse']['sections'].items():
            totals[name] += value['chunks']
    need(report['chunks'] == sum(f['parse']['chunks'] for f in report['filings']) and dict(totals) == report['sections'], 'report totals differ from the filings')
    fixture = lineage.get('current_raw_fixture') or {}
    fixture_path = (root / fixture['path']).resolve() if fixture.get('path') and not Path(fixture['path']).is_absolute() else None
    if fixture_path is not None and fixture_path.is_relative_to(root.resolve()) and fixture_path.is_file():
        need(sha256_file(fixture_path) == fixture.get('sha256'), 'current raw fixture bytes differ from the recorded hash')
        info['current_raw_fixture'] = 'recomputed_verified'
    else:
        info['current_raw_fixture'] = 'not_present_locally'
    info['previous_report_sha256'] = previous.get('report_sha256')
    info['statement'] = lineage.get('statement')
    return info, problems


def scan_corpus(corpus: Path, accessions: set[str] | frozenset[str],
                wanted: set[str] | frozenset[str] = frozenset()) -> tuple[dict[str, collections.Counter], dict[str, dict]]:
    """Stream the corpus once. Memory: one counter per accession plus only the `wanted` records."""
    sections = {a: collections.Counter() for a in accessions}
    found: dict[str, dict] = {}
    with Path(corpus).open('rb') as stream:
        while True:
            line = stream.readline(MAX_RECORD_BYTES + 1)
            if not line:
                break
            if len(line) > MAX_RECORD_BYTES:
                raise ValueError('oversized corpus record')
            chunk = json.loads(line)
            accession = chunk['ID'].split('#')[0]
            if accession in sections:
                sections[accession][chunk['SectionItem']] += 1
            if chunk['ID'] in wanted:
                if chunk['ID'] in found:
                    raise ValueError(f"duplicate source ID {chunk['ID']}")
                found[chunk['ID']] = chunk
    return sections, found


def scope_availability(sources: list[dict[str, Any]], sections: dict[str, collections.Counter],
                       ticker: str, year: int, section: str) -> dict[str, Any]:
    """Whether a requested (ticker, filing year[, exact section]) exists in the enrolled corpus.

    This is availability of the *filing and section only*. It never asserts that a
    particular semantic topic is answerable from the passages.
    """
    matches = [s for s in sources if s['ticker'] == ticker and s['filing_year'] == year]
    if len(matches) > 1:
        raise SuiteError(f'ambiguous trusted filing for {ticker} {year}')
    row: dict[str, Any] = dict(ticker=ticker, year=year, section=section, source_exists=bool(matches),
        accession=matches[0]['accession'] if matches else '', passages_in_scope=0)
    if matches:
        counts = sections.get(matches[0]['accession'], collections.Counter())
        row['passages_in_scope'] = counts[section] if section else sum(counts.values())
    row['available'] = row['passages_in_scope'] > 0
    row['reason'] = 'available' if row['available'] else ('exact_section_absent' if row['source_exists'] else 'filing_year_absent')
    return row


def agent_view(case: dict[str, Any]) -> dict[str, Any]:
    """The only case fields the agent harness may see. Anchors and expectations are excluded."""
    return {field: case[field] for field in AGENT_VISIBLE_FIELDS}


def case_kind_group(kind: str) -> str:
    return 'negative+mixed' if kind in ('negative', 'mixed') else kind


def suite_coverage(suite: dict[str, Any]) -> dict[str, Any]:
    cases = suite['cases']
    positive = [c for c in cases if c['kind'] == 'positive']
    companies = {s['ticker'] for c in positive for s in c['scopes']}
    return dict(
        positive_companies=sorted(companies), new_companies_beyond_baseline=sorted(companies - BASELINE_COMPANIES),
        multi_company_comparisons=[c['id'] for c in cases if len({s['ticker'] for s in c['scopes']}) > 1],
        multi_year_comparisons=[c['id'] for c in cases if len({s['year'] for s in c['scopes']}) > 1],
        filing_years=sorted({s['year'] for c in cases for s in c['scopes']}),
        exact_sections=sorted({c['section'] for c in cases if c['section']}),
        absent_filing_year=[c['id'] for c in cases if any(e['reason'] == 'filing_year_absent' for e in c['expected_scopes'])],
        absent_exact_section=[c['id'] for c in cases if any(e['reason'] == 'exact_section_absent' for e in c['expected_scopes'])],
        mixed_availability=[c['id'] for c in cases if len({e['available'] for e in c['expected_scopes']}) > 1])


def validate_suite(suite: dict[str, Any]) -> None:
    """Structural validation of the frozen suite (no corpus access)."""
    if suite.get('schema') != SCHEMA:
        raise SuiteError('unknown suite schema')
    cases = suite.get('cases')
    if not isinstance(cases, list) or len(cases) != CASE_COUNT or suite.get('case_count') != CASE_COUNT:
        raise SuiteError(f'suite must contain exactly {CASE_COUNT} cases')
    ids = [c.get('id') for c in cases]
    if ids != suite.get('case_ids') or len(set(ids)) != CASE_COUNT:
        raise SuiteError('case_ids must match the ordered unique case list')
    expected_ids = REGRESSION_IDS + [f'broad{i}' for i in range(11, 11 + CASE_COUNT - len(REGRESSION_IDS))]
    if ids != expected_ids:
        raise SuiteError('case ids must be audit01..audit10 then broad11..broad30 in order')
    groups = collections.Counter(case_kind_group(c.get('kind', '')) for c in cases)
    if dict(groups) != KIND_COUNTS:
        raise SuiteError(f'case kind counts {dict(groups)} != {KIND_COUNTS}')
    prompts = [c.get('prompt') for c in cases]
    if len(set(prompts)) != CASE_COUNT:
        raise SuiteError('prompts must be unique')
    all_anchor_text: list[str] = []
    for case in cases:
        prompt = case['prompt']
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode()) > MAX_PROMPT_BYTES:
            raise SuiteError(f"{case['id']}: invalid prompt")
        scopes = case.get('scopes')
        if not scopes or len(scopes) > 3 or len({(s['ticker'], s['year']) for s in scopes}) != len(scopes):
            raise SuiteError(f"{case['id']}: scopes must be 1..3 unique (ticker, year) pairs")
        if case.get('section', '') not in ('',) + EXACT_SECTIONS:
            raise SuiteError(f"{case['id']}: unsupported exact section")
        for scope in scopes:
            if scope['ticker'] not in prompt or str(scope['year']) not in prompt:
                raise SuiteError(f"{case['id']}: prompt must state {scope['ticker']} and {scope['year']}")
        if case['section'] and f'"{case["section"]}"' not in prompt:
            raise SuiteError(f"{case['id']}: exact section must be stated in the prompt")
        expected = case.get('expected_scopes')
        if not expected or [(e['ticker'], e['year'], e['section']) for e in expected] != [(s['ticker'], s['year'], case['section']) for s in scopes]:
            raise SuiteError(f"{case['id']}: expected_scopes must mirror scopes and section")
        if case['kind'] == 'regression':
            if case.get('reviewer_evidence'):
                raise SuiteError(f"{case['id']}: verbatim regression prompts carry no added anchors")
        else:
            anchors = case.get('reviewer_evidence', {}).get('anchors', [])
            unavailable = [e for e in expected if not e['available']]
            if case['kind'] == 'positive' and (unavailable or not anchors):
                raise SuiteError(f"{case['id']}: positive case needs available scopes and anchors")
            if case['kind'] == 'negative' and (len(unavailable) != len(expected)):
                raise SuiteError(f"{case['id']}: negative case must have every scope unavailable")
            if case['kind'] == 'mixed' and not (unavailable and len(unavailable) < len(expected)):
                raise SuiteError(f"{case['id']}: mixed case must have available and unavailable scopes")
            for anchor in anchors:
                scope = next((e for e in expected if (e['ticker'], e['year']) == (anchor['ticker'], anchor['year'])), None)
                if scope is None or not scope['available'] or not anchor['id'].startswith(scope['accession'] + '#'):
                    raise SuiteError(f"{case['id']}: anchor {anchor.get('id')} is outside an available requested scope")
                all_anchor_text += [anchor['id'], anchor['excerpt']]
    for case in cases:  # Answer/anchor material must never be recoverable from the agent-visible prompt.
        for text in all_anchor_text:
            if text in case['prompt']:
                raise SuiteError(f"{case['id']}: prompt leaks anchor material")
    coverage = suite_coverage(suite)
    if len(coverage['new_companies_beyond_baseline']) < MIN_NEW_COMPANIES:
        raise SuiteError(f'positive cases must cover >= {MIN_NEW_COMPANIES} companies beyond {sorted(BASELINE_COMPANIES)}')
    for required in ('multi_company_comparisons', 'multi_year_comparisons', 'absent_filing_year', 'absent_exact_section', 'mixed_availability'):
        if not coverage[required]:
            raise SuiteError(f'suite lacks required scenario: {required}')
    if set(coverage['exact_sections']) != set(EXACT_SECTIONS):
        raise SuiteError('suite must exercise exact Item 1A, Item 7 and Item 8 filters')


def load_suite(path: Path, expected_sha256: str | None = None) -> tuple[dict[str, Any], str]:
    """Load, hash and validate a frozen suite. Returns (suite, sha256 of the exact file bytes)."""
    path = Path(path)
    digest = sha256_file(path)
    if expected_sha256 is not None and digest != expected_sha256.lower():
        raise SuiteError(f'suite sha256 {digest} != pinned {expected_sha256}')
    suite = json.loads(path.read_text())
    validate_suite(suite)
    return suite, digest


def check_expected_scopes(suite: dict[str, Any], sources: list[dict[str, Any]],
                          sections: dict[str, collections.Counter], compare_counts: bool = True) -> None:
    """Recompute availability from the live corpus/enrollment and require it to equal the frozen expectation.

    `compare_counts=False` is for callers that only know which sections exist (the registry), not how many passages.
    """
    for case in suite['cases']:
        for expected in case['expected_scopes']:
            actual = scope_availability(sources, sections, expected['ticker'], expected['year'], expected['section'])
            if not compare_counts:
                actual = dict(actual, passages_in_scope=expected['passages_in_scope'])
            if actual != expected:
                raise SuiteError(f"{case['id']}: frozen availability {expected} != corpus {actual}")


_AGENT_FILES = {'state_machine.py', 'planner.py', 'critic.py', 'synthesizer.py', 'provenance.py', 'models.py',
                'filters.py', 'eval_agent.py', 'fast.py', 'eval_agent_latency.py'}


def classify_failure(exc: BaseException) -> str:
    """transport = retrieval service/network; model = Claude CLI generation; structural = agent/contract/verifier.

    Decided by the innermost agent-code frame of the traceback, then by exception type. It is a triage
    label, not proof: the full type, message and traceback are always saved beside it.
    """
    for frame in reversed(traceback.extract_tb(exc.__traceback__)):
        base = Path(frame.filename).name
        if base == 'provider.py':
            return 'model'
        if base == 'retrieval.py':
            return 'transport'
        if base in _AGENT_FILES:
            return 'structural'
    module = type(exc).__module__
    if module.startswith(('httpx', 'httpcore')) or isinstance(exc, ConnectionError):
        return 'transport'
    if isinstance(exc, (TimeoutError, FileNotFoundError)) or module.startswith('pydantic'):
        return 'model'
    return 'structural'


def failure_record(case_id: str, phase: str, exc: BaseException) -> dict[str, Any]:
    return dict(id=case_id, status='fail', phase=phase, category=classify_failure(exc), error_type=type(exc).__name__,
        error=f'{type(exc).__name__}: {exc}',
        traceback=''.join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-8000:])


def evidence_outcome(result_flags: list[str], citations: int, subqueries: int, cited_subqueries: int) -> str:
    """Honest evidence outcome. supported/partial_gap/abstained are NOT failures."""
    if citations and cited_subqueries == subqueries and not any(f.startswith('Subquery') for f in result_flags):
        return 'supported'
    return 'partial_gap' if cited_subqueries else 'abstained'


def registry_sections(registry: Any) -> dict[str, collections.Counter]:
    """Adapt ProvenanceRegistry.sections (sets) to Counters; counts are 1 per section and not meaningful."""
    return {accession: collections.Counter({name: 1 for name in names}) for accession, names in registry.sections.items()}


REQUIRED_CODE = ('py/service.py', 'py/common.py', 'py/rerank.py', 'py/dense_batcher.py', 'py/agent/state_machine.py',
                 'cmd/finfilings/main.go', 'pkg/hybrid/hybrid.go', 'pkg/index/shards.go', 'go.mod',
                 'bench_kit/eval_agent.py', 'bench_kit/agent_suite.py', 'scripts/evaluate-expanded-agent.py')


def code_files(root: Path) -> list[str]:
    """Implementation files whose hashes a run must record: sidecar, agent, Go retrieval stack, harness.

    Third-party packages under models/runtime are not hashed; they are pinned by the environment, not this repository.
    """
    root = Path(root)
    found = set()
    for pattern in ('py/*.py', 'py/agent/*.py', 'cmd/**/*.go', 'pkg/**/*.go', 'internal/**/*.go', 'go.mod', 'go.sum',
                    'scripts/evaluate-expanded-agent.py', 'scripts/benchmark-expanded.py', 'bench_kit/eval_agent.py',
                    'bench_kit/agent_suite.py', 'bench_kit/verify_broad_agent.py', 'bench_kit/build_broad_suite.py', 'bench_kit/eval_agent_latency.py'):
        for path in root.glob(pattern):
            name = path.name
            if path.is_file() and not name.startswith('test_') and not name.endswith('_test.go'):
                found.add(str(path.relative_to(root)))
    return sorted(found)
