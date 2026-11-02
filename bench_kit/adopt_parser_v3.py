#!/usr/bin/env python3
"""Adopt the verified Morgan Stanley parser-v3 passages into a NEW, separately versioned evaluation corpus.

This is a *targeted replacement* with explicit mixed lineage: one filing (MS, accession 000089542126000086) is replaced by
its parser-v3 passages; the other 147 ingested filings and the 19,511 baseline passages are NOT reparsed and are
copied byte for byte. Nothing in the existing v2 corpus, SEC ingestion output, indexes or evidence is modified.

Subcommands
  build   stage -> verify -> publish the new evaluation data directory and SEC ingestion directory (exclusive create;
          existing outputs are refused; failed builds leave nothing at the final paths)
  verify  independently re-derive every relation from the files on disk (also run automatically on the staged build)

Memory/disk bounds: corpora are streamed line by line (one record <= 4 MiB). The only in-memory collections are the
replaced filing's records (<= 4,096 passages, enforced) and 148 report entries. Duplicate-ID detection and
corpus-wide ID tracking use a disk-backed SQLite table. No network, model or retrieval service is used.
"""
import argparse
import collections
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'bench_kit'))
import agent_suite as lib  # noqa: E402

MAX_RECORD_BYTES = 4 << 20
MAX_TARGETED_PASSAGES = 4096
LINEAGE_SCHEMA = 'finfilings-ingest-lineage/v1'
TARGET_TICKER = 'MS'
TARGET_ACCESSION = '000089542126000086'
V2_PARSER = 'deterministic-canonical-heading-v2-go'
V3_PARSER = 'deterministic-canonical-heading-v3-go'
DEFAULTS = dict(
    base_data='data/sec-scale-v2-evaluation-20261031',
    base_sec='indexes/sec-scale-v2-20261031',
    replacement='indexes/parser-boundaries-20261101-codex/ms-v3',
    legacy='indexes/parser-boundaries-20261101-codex/ms-v2-legacy',
    replay_summary='bench_kit/results/parser-boundaries-20261101/verified/replay-summary.json',
    raw='data/parser-replay-20261101-codex/ms-20251231.sec-current-NOT-CORPUS-BYTES.htm',
    raw_sha256='3002143b544ab01805ac755efc857f8f62e59fe7a80c6d6fecdf64be560278b2',
    out_data='data/sec-scale-v3-evaluation-20261101',
    out_sec='indexes/sec-scale-v3-20261101')
EXPECTED = dict(old_passages=361, new_passages=315, unchanged=314, removed=46, changed=1, old_total=49452, new_total=49406)


class MigrationError(ValueError):
    """An input, relation or output of the corpus migration is invalid."""


def sha256_file(path):
    return lib.sha256_file(Path(path))


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def read_lines(path):
    """Yield raw newline-terminated lines; one line is at most MAX_RECORD_BYTES."""
    with Path(path).open('rb') as stream:
        while True:
            line = stream.readline(MAX_RECORD_BYTES + 1)
            if not line:
                return
            if len(line) > MAX_RECORD_BYTES:
                raise MigrationError(f'{path}: record exceeds {MAX_RECORD_BYTES} bytes')
            if not line.endswith(b'\n'):
                raise MigrationError(f'{path}: final record is not newline terminated')
            yield line


def record_id(line, where):
    try:
        record = json.loads(line)
        identifier = record['ID']
    except (ValueError, KeyError, TypeError) as exc:
        raise MigrationError(f'{where}: malformed record ({type(exc).__name__})') from exc
    if not isinstance(identifier, str) or not identifier or not isinstance(record.get('Text'), str) or not record['Text']:
        raise MigrationError(f'{where}: record lacks a string ID and non-empty Text')
    return identifier, record


def load_targeted(path, accession, ticker=TARGET_TICKER):
    """Ordered (id, line) for the replaced filing. Bounded; every record must belong to that filing."""
    rows = []
    seen = set()
    for number, line in enumerate(read_lines(path), 1):
        identifier, record = record_id(line, f'{path}:{number}')
        if not identifier.startswith(accession + '#') or record.get('Ticker') != ticker:
            raise MigrationError(f'{path}:{number}: {identifier} is not a {ticker} {accession} passage')
        if identifier in seen:
            raise MigrationError(f'{path}: duplicate ID {identifier}')
        seen.add(identifier)
        rows.append((identifier, line))
        if len(rows) > MAX_TARGETED_PASSAGES:
            raise MigrationError(f'{path}: more than {MAX_TARGETED_PASSAGES} passages for one filing')
    if not rows:
        raise MigrationError(f'{path}: no passages')
    return rows


class IdTable:
    """Disk-backed unique-ID guard: corpus-sized state never lives in Python memory."""

    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute('PRAGMA cache_size=-4096')
        self.db.execute('CREATE TABLE ids(id TEXT PRIMARY KEY) WITHOUT ROWID')

    def add(self, identifier, where):
        try:
            self.db.execute('INSERT INTO ids VALUES (?)', (identifier,))
        except sqlite3.IntegrityError as exc:
            raise MigrationError(f'{where}: duplicate ID {identifier}') from exc

    def close(self):
        self.db.commit()
        self.db.close()


def replace_filing(source, target, accession, replacement_lines, scratch):
    """Stream `source` to `target`, substituting the filing's contiguous run with `replacement_lines`.

    Every other line is copied as the identical bytes. Returns counts and digests so the relation is checkable:
    `kept_sha256_in == kept_sha256_out` is the byte-for-byte preservation proof for all non-replaced records.
    """
    prefix = accession + '#'
    old = {}
    state = 0  # 0 before the run, 1 inside it, 2 after it
    kept_in, kept_out = hashlib.sha256(), hashlib.sha256()
    whole_in, whole_out = hashlib.sha256(), hashlib.sha256()
    lines_in = lines_out = kept = 0
    ids = IdTable(scratch)
    try:
        with Path(target).open('xb') as sink:
            for line in read_lines(source):
                lines_in += 1
                whole_in.update(line)
                identifier, _ = record_id(line, f'{source}:{lines_in}')
                if identifier.startswith(prefix):
                    if state == 2:
                        raise MigrationError(f'{source}: {accession} records are not contiguous')
                    if identifier in old:
                        raise MigrationError(f'{source}: duplicate ID {identifier}')
                    old[identifier] = sha256_bytes(line)
                    if len(old) > MAX_TARGETED_PASSAGES:
                        raise MigrationError(f'{source}: more than {MAX_TARGETED_PASSAGES} passages for {accession}')
                    if state == 0:
                        state = 1
                        for new_id, new_line in replacement_lines:
                            ids.add(new_id, 'replacement')
                            sink.write(new_line)
                            whole_out.update(new_line)
                            lines_out += 1
                    continue
                if state == 1:
                    state = 2
                kept_in.update(line)
                ids.add(identifier, f'{source}:{lines_in}')
                sink.write(line)
                whole_out.update(line)
                kept_out.update(line)
                kept += 1
                lines_out += 1
            if state == 0:
                raise MigrationError(f'{source}: no records for accession {accession}')
            sink.flush()
            os.fsync(sink.fileno())
    finally:
        ids.close()
    return dict(lines_in=lines_in, lines_out=lines_out, kept_lines=kept, old=old, source_sha256=whole_in.hexdigest(),
                output_sha256=whole_out.hexdigest(), kept_sha256_in=kept_in.hexdigest(), kept_sha256_out=kept_out.hexdigest())


def classify(old, legacy, current):
    """old/legacy/current: id -> line digest. Returns the diff of the replacement against the active corpus."""
    if legacy != old:
        raise MigrationError('legacy (parser-v2) replay is not byte-identical to the active corpus records for this filing')
    unchanged = sorted(i for i in current if old.get(i) == current[i])
    changed = sorted(i for i in current if i in old and old[i] != current[i])
    added = sorted(i for i in current if i not in old)
    removed = sorted(i for i in old if i not in current)
    return dict(unchanged=unchanged, changed=changed, added=added, removed=removed)


def section_counts(path):
    counts = collections.Counter()
    for number, line in enumerate(read_lines(path), 1):
        _, record = record_id(line, f'{path}:{number}')
        counts[record.get('SectionItem')] += 1
    return dict(counts)


def check_parse_report(parse, raw_sha256, counts):
    if parse.get('parser') != V3_PARSER:
        raise MigrationError(f"replacement parse report parser {parse.get('parser')!r} != {V3_PARSER}")
    if parse.get('raw_sha256') != raw_sha256:
        raise MigrationError('replacement parse report was produced from different raw bytes than the verified raw hash')
    if parse.get('chunks') != sum(counts.values()) or {k: v['chunks'] for k, v in parse['sections'].items()} != counts:
        raise MigrationError('replacement parse report chunk counts disagree with the replacement passages')


def check_replay_summary(summary, replacement, legacy, parse_report, raw_sha256):
    v3, v2 = summary['parser_v3'], summary['parser_v2_legacy']
    problems = []
    for label, record, passages in (('v3', v3, replacement), ('v2-legacy', v2, legacy)):
        if record['passages_sha256'] != sha256_file(passages):
            problems.append(f'{label} passages differ from the verified replay summary')
    if v3['artifact_file_sha256']['parse-report.json'] != sha256_file(parse_report):
        problems.append('v3 parse report differs from the verified replay summary')
    if summary['source']['sha256'] != raw_sha256:
        problems.append('verified replay summary names different raw bytes')
    if problems:
        raise MigrationError('; '.join(problems))


def make_report(base_report, parse, diff, ctx):
    """New ingestion report: every other filing untouched, the targeted entry updated honestly with its history."""
    report = copy.deepcopy(base_report)
    entries = [i for i, f in enumerate(report['filings']) if f['ref']['Accession'] == TARGET_ACCESSION]
    if len(entries) != 1:
        raise MigrationError('base report must contain the targeted filing exactly once')
    old = report['filings'][entries[0]]
    previous = dict(role='previous_archived_acquisition', url=old['download']['url'], bytes=old['download']['bytes'],
        sha256=old['download']['sha256'], parser=old['parse']['parser'], heading_mode=old['parse'].get('heading_mode'),
        chunks=old['parse']['chunks'], sections=old['parse']['sections'],
        status='recorded by the parser-v2 ingestion report; the archived HTML bytes are no longer retained, so this hash is history, not a currently re-verified fact')
    current = dict(role='current_sec_bytes', url=old['download']['url'], bytes=parse['raw_bytes'], sha256=parse['raw_sha256'],
        parser=parse['parser'], heading_mode=parse.get('heading_mode'), chunks=parse['chunks'], sections=parse['sections'],
        status='local fixture re-hashed during this migration', fixture_path=ctx['raw_path'])
    entry = dict(old)
    entry['download'] = dict(url=old['download']['url'], bytes=parse['raw_bytes'], sha256=parse['raw_sha256'])
    entry['parse'] = dict(parse)
    entry['raw_identity_history'] = [previous, current]
    entry['replacement'] = dict(kind='targeted_parser_v3_replacement', replaces_parser=V2_PARSER, with_parser=V3_PARSER,
        raw_identity_changed=previous['sha256'] != current['sha256'],
        legacy_replay_records_matching_previous_corpus=len(diff['unchanged']) + len(diff['changed']) + len(diff['removed']),
        passages_before=len(diff['unchanged']) + len(diff['changed']) + len(diff['removed']),
        passages_after=len(diff['unchanged']) + len(diff['changed']) + len(diff['added']),
        unchanged=len(diff['unchanged']), changed=diff['changed'], removed=len(diff['removed']), added=len(diff['added']))
    report['filings'][entries[0]] = entry
    counts = collections.Counter()
    for filing in report['filings']:
        for name, value in filing['parse']['sections'].items():
            counts[name] += value['chunks']
    report['chunks'] = sum(f['parse']['chunks'] for f in report['filings'])
    report['sections'] = {name: counts[name] for name in base_report['sections']}
    report['parser'] = 'mixed'
    report['parsers'] = dict(collections.Counter(f['parse']['parser'] for f in report['filings']))
    report['lineage'] = dict(schema=LINEAGE_SCHEMA, kind='targeted_replacement',
        statement='Mixed lineage: a targeted parser-v3 replacement of one filing. The other filings were NOT reparsed and keep their parser-v2 output byte for byte.',
        previous=dict(report_path=ctx['previous_report_path'], report_sha256=ctx['previous_report_sha256'],
                      passages_sha256=ctx['previous_passages_sha256'], chunks=base_report['chunks'], filings=len(base_report['filings'])),
        targeted_filings=[dict(ticker=TARGET_TICKER, accession=TARGET_ACCESSION)],
        reparsed_filings=1, untouched_filings=len(report['filings']) - 1,
        replacement=dict(passages_path=ctx['replacement_path'], passages_sha256=ctx['replacement_sha256'],
                         parse_report_sha256=ctx['parse_report_sha256'], legacy_passages_sha256=ctx['legacy_sha256'],
                         replay_summary_path=ctx['replay_summary_path'], replay_summary_sha256=ctx['replay_summary_sha256']),
        current_raw_fixture=dict(path=ctx['raw_path'], sha256=parse['raw_sha256'], bytes=parse['raw_bytes'], rehashed_at_build=True,
                                 note='Current SEC bytes. They differ from the previously archived acquisition, whose bytes are no longer available. Raw-file equivalence is not claimed; the 361 parser-v2 replay records were compared with the active corpus byte for byte.'),
        diff=dict(unchanged=len(diff['unchanged']), changed_ids=diff['changed'], removed=len(diff['removed']), added=len(diff['added'])))
    return report


def make_provenance(base_provenance, report_path, passages_path, chunks_path, report, ctx, counts):
    provenance = copy.deepcopy(base_provenance)
    total = sum(counts.values())
    provenance.update(
        dataset='Frozen FinQA/Phase 2 benchmark plus additional live SEC 10-K sections (mixed lineage: targeted parser-v3 Morgan Stanley replacement)',
        corpus_version='sec-scale-v3-evaluation-20261101',
        documents=total, corpus_sha256=sha256_file(chunks_path), document_counts=counts,
        new_sec_passages_sha256=sha256_file(passages_path), new_sec_report_sha256=sha256_file(report_path),
        new_sec_filings=len(report['filings']), new_sec_skipped=len(report.get('skipped', [])),
        lineage=dict(kind='targeted_replacement', previous_corpus_sha256=base_provenance['corpus_sha256'],
                     previous_documents=base_provenance['documents'], previous_provenance_path=ctx['previous_provenance_path'],
                     previous_provenance_sha256=ctx['previous_provenance_sha256'], targeted_filings=[TARGET_ACCESSION],
                     reparsed_filings=1, untouched_filings=len(report['filings']) - 1,
                     queries_and_qrels='byte-identical copies of the v2 files (hash checked)'))
    provenance['evaluation_notes'] = list(base_provenance['evaluation_notes']) + [
        'Mixed lineage: only Morgan Stanley (accession %s) was reparsed with parser v3; 147 other filings keep parser-v2 output. Not all 148 filings were reparsed.' % TARGET_ACCESSION,
        'This corpus is a new version; the v2 corpus, its report and its indexes are unchanged.']
    return provenance


def write_exclusive(path, data):
    with Path(path).open('xb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def copy_exclusive(source, target):
    with Path(source).open('rb') as src, Path(target).open('xb') as dst:
        shutil.copyfileobj(src, dst, 1 << 20)
        dst.flush()
        os.fsync(dst.fileno())


def json_bytes(value):
    return (json.dumps(value, indent=2, ensure_ascii=False) + '\n').encode()


def build(opts):
    root = Path(opts.root)
    path = lambda name: root / getattr(opts, name)  # absolute option values replace the root in a Path join  # noqa: E731
    base_data, base_sec, replacement, legacy = path('base_data'), path('base_sec'), path('replacement'), path('legacy')
    raw, summary_path, out_data, out_sec = path('raw'), path('replay_summary'), path('out_data'), path('out_sec')
    for target in (out_data, out_sec):
        if os.path.lexists(target):
            raise FileExistsError(f'{target} already exists; a corpus version is never overwritten')
    exp = opts.expected
    # ---- verify every input before anything is staged ----
    base = json.loads((base_data / 'provenance.json').read_text())
    if sha256_file(base_data / 'chunks.jsonl') != base['corpus_sha256']:
        raise MigrationError('base corpus differs from its provenance')
    for name, key in (('queries.jsonl', 'query_sha256'), ('qrels.tsv', 'qrels_sha256')):
        if sha256_file(base_data / name) != base[key]:
            raise MigrationError(f'base {name} differs from its provenance')
    if sha256_file(base_sec / 'passages.jsonl') != base['new_sec_passages_sha256'] or sha256_file(base_sec / 'report.json') != base['new_sec_report_sha256']:
        raise MigrationError('base SEC ingestion output differs from the base provenance')
    if base['documents'] != exp['old_total']:
        raise MigrationError(f"base corpus has {base['documents']} documents, expected {exp['old_total']}")
    raw_hash = sha256_file(raw)
    if raw_hash != opts.raw_sha256:
        raise MigrationError(f'raw fixture sha256 {raw_hash} != expected {opts.raw_sha256}')
    if (replacement / 'segments/CORPUS_SHA256').read_text().strip() != sha256_file(replacement / 'passages.jsonl'):
        raise MigrationError('replacement segment fingerprint differs from its passages')
    parse = json.loads((replacement / 'parse-report.json').read_text())
    if parse['raw_bytes'] != raw.stat().st_size:
        raise MigrationError('parse report raw byte count differs from the raw fixture')
    replay = json.loads(summary_path.read_text())
    check_replay_summary(replay, replacement / 'passages.jsonl', legacy / 'passages.jsonl', replacement / 'parse-report.json', raw_hash)
    new_rows = load_targeted(replacement / 'passages.jsonl', TARGET_ACCESSION)
    legacy_rows = load_targeted(legacy / 'passages.jsonl', TARGET_ACCESSION)
    check_parse_report(parse, raw_hash, section_counts(replacement / 'passages.jsonl'))
    stage = out_data.with_name(out_data.name + f'.staging-{os.getpid()}')
    stage_sec = out_sec.with_name(out_sec.name + f'.staging-{os.getpid()}')
    for directory in (stage, stage_sec):
        directory.parent.mkdir(parents=True, exist_ok=True)
        directory.mkdir(exist_ok=False)
    try:
        chunks = replace_filing(base_data / 'chunks.jsonl', stage / 'chunks.jsonl', TARGET_ACCESSION, new_rows, stage / 'ids-chunks.sqlite')
        passages = replace_filing(base_sec / 'passages.jsonl', stage_sec / 'passages.jsonl', TARGET_ACCESSION, new_rows, stage / 'ids-passages.sqlite')
        for name in ('ids-chunks.sqlite', 'ids-passages.sqlite'):
            (stage / name).unlink()
        old_digests = chunks['old']
        if passages['old'] != old_digests:
            raise MigrationError('SEC passages and evaluation corpus disagree about the replaced filing')
        diff = classify(old_digests, {i: sha256_bytes(l) for i, l in legacy_rows}, {i: sha256_bytes(l) for i, l in new_rows})
        observed = dict(old_passages=len(old_digests), new_passages=len(new_rows), unchanged=len(diff['unchanged']),
                        removed=len(diff['removed']), changed=len(diff['changed']), old_total=chunks['lines_in'], new_total=chunks['lines_out'])
        if observed != exp:
            raise MigrationError(f'migration counts {observed} != expected {exp}')
        if diff['added']:
            raise MigrationError(f"replacement adds IDs absent from the active corpus: {diff['added'][:3]}")
        for stats in (chunks, passages):
            if stats['kept_sha256_in'] != stats['kept_sha256_out']:
                raise MigrationError('non-replaced records were not preserved byte for byte')
        if chunks['source_sha256'] != base['corpus_sha256']:
            raise MigrationError('base corpus changed while it was being read')
        # ---- SEC ingestion directory ----
        base_report = json.loads((base_sec / 'report.json').read_text())
        ctx = dict(raw_path=opts.raw, previous_report_path=str(Path(opts.base_sec) / 'report.json'),
                   previous_report_sha256=sha256_file(base_sec / 'report.json'), previous_passages_sha256=base['new_sec_passages_sha256'],
                   replacement_path=str(Path(opts.replacement) / 'passages.jsonl'), replacement_sha256=sha256_file(replacement / 'passages.jsonl'),
                   parse_report_sha256=sha256_file(replacement / 'parse-report.json'), legacy_sha256=sha256_file(legacy / 'passages.jsonl'),
                   replay_summary_path=opts.replay_summary, replay_summary_sha256=sha256_file(summary_path),
                   previous_provenance_path=str(Path(opts.base_data) / 'provenance.json'),
                   previous_provenance_sha256=sha256_file(base_data / 'provenance.json'))
        report = make_report(base_report, parse, diff, ctx)
        write_exclusive(stage_sec / 'report.json', json_bytes(report))
        with (base_sec / 'provenance.jsonl').open('rb') as source, (stage_sec / 'provenance.jsonl').open('xb') as sink:
            for line in source:
                row = json.loads(line)
                if row['ref']['Accession'] == TARGET_ACCESSION:
                    row = next(f for f in report['filings'] if f['ref']['Accession'] == TARGET_ACCESSION)
                    line = (json.dumps(row, separators=(',', ':'), ensure_ascii=False) + '\n').encode()
                sink.write(line)
        # ---- evaluation data directory ----
        for name in ('queries.jsonl', 'qrels.tsv'):
            copy_exclusive(base_data / name, stage / name)
        counts = dict(base['document_counts'], new_sec=passages['lines_out'])
        write_exclusive(stage / 'provenance.json', json_bytes(make_provenance(base, stage_sec / 'report.json', stage_sec / 'passages.jsonl', stage / 'chunks.jsonl', report, ctx, counts)))
        migration = dict(tool='bench_kit/adopt_parser_v3.py build', utc=datetime.now(timezone.utc).isoformat(), observed=observed,
            diff=dict(unchanged=len(diff['unchanged']), changed=diff['changed'], removed=len(diff['removed']), removed_first=diff['removed'][0], removed_last=diff['removed'][-1]),
            non_target_records_sha256_before=chunks['kept_sha256_in'], non_target_records_sha256_after=chunks['kept_sha256_out'],
            non_target_records=chunks['kept_lines'], new_corpus_sha256=chunks['output_sha256'], previous_corpus_sha256=chunks['source_sha256'],
            raw_fixture=dict(path=opts.raw, sha256=raw_hash), lineage='mixed: targeted MS parser-v3 replacement; 147 filings not reparsed')
        write_exclusive(stage / 'migration.json', json_bytes(migration))
        problems = verify_dirs(base_data, base_sec, stage, stage_sec, replacement, legacy, raw, opts, root)
        if problems:
            raise MigrationError('self-verification failed: ' + '; '.join(problems))
        os.rename(stage_sec, out_sec)
        try:
            os.rename(stage, out_data)
        except OSError:
            os.rename(out_sec, stage_sec)
            raise
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        shutil.rmtree(stage_sec, ignore_errors=True)
        raise
    return dict(out_data=str(out_data), out_sec=str(out_sec), corpus_sha256=chunks['output_sha256'], documents=chunks['lines_out'], observed=observed)


def stream_split(path, accession):
    """Digest of non-targeted lines and the targeted lines (bounded) of a corpus file."""
    kept, targeted, count = hashlib.sha256(), [], 0
    for line in read_lines(path):
        identifier, _ = record_id(line, str(path))
        if identifier.startswith(accession + '#'):
            targeted.append((identifier, line))
            if len(targeted) > MAX_TARGETED_PASSAGES:
                raise MigrationError(f'{path}: too many targeted records')
        else:
            kept.update(line)
            count += 1
    return kept.hexdigest(), targeted, count


def verify_dirs(base_data, base_sec, new_data, new_sec, replacement, legacy, raw, opts, root):
    """Independent re-derivation from files. Returns a list of problems (empty = verified)."""
    problems = []

    def need(condition, message):
        if not condition:
            problems.append(message)
        return bool(condition)

    try:
        base = json.loads((Path(base_data) / 'provenance.json').read_text())
        new = json.loads((Path(new_data) / 'provenance.json').read_text())
        report = json.loads((Path(new_sec) / 'report.json').read_text())
        old_report = json.loads((Path(base_sec) / 'report.json').read_text())
    except (OSError, ValueError) as exc:
        return [f'unreadable provenance/report: {exc}']
    need(sha256_file(Path(new_data) / 'chunks.jsonl') == new['corpus_sha256'], 'new corpus differs from its provenance')
    need(new['corpus_sha256'] != base['corpus_sha256'], 'new corpus hash equals the old corpus hash')
    need(sha256_file(Path(new_sec) / 'report.json') == new['new_sec_report_sha256'], 'report hash differs from provenance')
    need(sha256_file(Path(new_sec) / 'passages.jsonl') == new['new_sec_passages_sha256'], 'SEC passages hash differs from provenance')
    for name, key in (('queries.jsonl', 'query_sha256'), ('qrels.tsv', 'qrels_sha256')):
        need(sha256_file(Path(new_data) / name) == base[key] == new[key], f'{name} is not byte-identical to v2')
        need(sha256_file(Path(new_data) / name) == sha256_file(Path(base_data) / name), f'{name} bytes differ from v2')
    # Non-target records: identical bytes and order; targeted records: exactly the verified replacement.
    kept_old, old_target, old_kept_count = stream_split(Path(base_data) / 'chunks.jsonl', TARGET_ACCESSION)
    kept_new, new_target, new_kept_count = stream_split(Path(new_data) / 'chunks.jsonl', TARGET_ACCESSION)
    need(kept_old == kept_new and old_kept_count == new_kept_count, 'non-Morgan-Stanley records are not byte-identical to v2')
    replacement_rows = load_targeted(Path(replacement) / 'passages.jsonl', TARGET_ACCESSION)
    need(new_target == replacement_rows, 'targeted records are not exactly the verified parser-v3 passages')
    need(len(old_target) == opts.expected['old_passages'] and len(new_target) == opts.expected['new_passages'], 'unexpected targeted record counts')
    legacy_rows = load_targeted(Path(legacy) / 'passages.jsonl', TARGET_ACCESSION)
    need(legacy_rows == old_target, 'parser-v2 legacy replay is not byte-identical to the v2 corpus records')
    old_by, new_by = dict(old_target), dict(new_target)
    need(sum(old_by.get(i) == l for i, l in new_by.items()) == opts.expected['unchanged'], 'unchanged-ID count differs')
    changed = sorted(i for i in new_by if i in old_by and old_by[i] != new_by[i])
    need(len(changed) == opts.expected['changed'] and changed == sorted(opts.expect_changed_id), f'changed IDs {changed} differ from the expected {opts.expect_changed_id}')
    need(len(set(old_by) - set(new_by)) == opts.expected['removed'], 'removed-ID count differs')
    need(new['documents'] == opts.expected['new_total'] and old_kept_count + len(new_target) == new['documents'], 'document count differs')
    # Position: the replacement occupies the old filing's slot (check by ordered ID walk, streaming).
    old_ids = (record_id(l, 'old')[0] for l in read_lines(Path(base_data) / 'chunks.jsonl'))
    new_ids = (record_id(l, 'new')[0] for l in read_lines(Path(new_data) / 'chunks.jsonl'))
    skip = lambda ids: (i for i in ids if not i.startswith(TARGET_ACCESSION + '#'))  # noqa: E731
    need(all(a == b for a, b in zip(skip(old_ids), skip(new_ids))), 'non-targeted record order changed')
    # Evaluation corpus = baseline prefix + SEC passages.
    baseline = base['document_counts']['baseline']
    head, tail = hashlib.sha256(), hashlib.sha256()
    for number, line in enumerate(read_lines(Path(new_data) / 'chunks.jsonl')):
        (head if number < baseline else tail).update(line)
    need(head.hexdigest() == base['base_corpus_sha256'], 'baseline prefix differs from the frozen Phase 2 corpus')
    need(tail.hexdigest() == new['new_sec_passages_sha256'], 'SEC suffix differs from the SEC ingestion passages')
    need(new['document_counts'] == dict(base['document_counts'], new_sec=new['document_counts']['new_sec']) and sum(new['document_counts'].values()) == new['documents'],
         'document_counts inconsistent')
    # Report and lineage.
    info, lineage_problems = lib.check_report_lineage(Path(new_sec) / 'report.json', root)
    problems += lineage_problems
    need(info.get('mode') == 'targeted_replacement', 'report does not declare targeted-replacement lineage')
    need(report['chunks'] == new['document_counts']['new_sec'] == sum(f['parse']['chunks'] for f in report['filings']), 'report chunk totals inconsistent')
    need(report['skipped'] == old_report['skipped'], 'skipped filings changed')
    old_sources = lib.build_trusted_sources(Path(root) / 'data/sec/provenance.json', Path(base_sec) / 'report.json')
    new_sources = lib.build_trusted_sources(Path(root) / 'data/sec/provenance.json', Path(new_sec) / 'report.json')
    need(len(old_sources) == len(new_sources), 'trusted source count changed')
    differing = [(o['accession'], sorted(k for k in o if o[k] != n[k])) for o, n in zip(old_sources, new_sources) if o != n]
    need(differing == [(TARGET_ACCESSION, ['raw_sha256'])], f'trusted sources must differ from v2 only in the Morgan Stanley raw_sha256: {differing}')
    # Raw identity.
    need(sha256_file(raw) == opts.raw_sha256, 'raw fixture hash differs')
    ms = next(f for f in report['filings'] if f['ref']['Accession'] == TARGET_ACCESSION)
    need(ms['download']['sha256'] == opts.raw_sha256 and ms['parse']['raw_sha256'] == opts.raw_sha256, 'current raw identity not recorded')
    return problems


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('command', choices=('build', 'verify'))
    for name, default in DEFAULTS.items():
        parser.add_argument('--' + name.replace('_', '-'), default=default)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--expect-changed-id', action='append', default=None, help='ID whose bytes change (repeatable); default: the verified item7 passage-000080')
    for key, value in EXPECTED.items():
        parser.add_argument('--expect-' + key.replace('_', '-'), type=int, default=value)
    opts = parser.parse_args(argv)
    opts.expect_changed_id = opts.expect_changed_id if opts.expect_changed_id is not None else ([TARGET_ACCESSION + '#item7#passage-000080'] if opts.expect_changed == 1 else [])
    opts.expected = {key: getattr(opts, 'expect_' + key) for key in EXPECTED}
    return opts


def main(argv=None):
    opts = parse_args(argv)
    root = Path(opts.root)
    resolve = lambda name: Path(getattr(opts, name)) if Path(getattr(opts, name)).is_absolute() else root / getattr(opts, name)  # noqa: E731
    if opts.command == 'build':
        print(json.dumps(build(opts), indent=2))
        return 0
    problems = verify_dirs(resolve('base_data'), resolve('base_sec'), resolve('out_data'), resolve('out_sec'), resolve('replacement'), resolve('legacy'), resolve('raw'), opts, root)
    print(json.dumps(dict(verified=not problems, problems=problems), indent=2))
    return 1 if problems else 0


if __name__ == '__main__':
    raise SystemExit(main())
