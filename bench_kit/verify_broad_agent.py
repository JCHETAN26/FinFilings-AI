#!/usr/bin/env python3
"""Offline, independent verification of a broad-suite agent run.

Trusts nothing the evaluator reported. From the frozen suite (pinned by hash), the original ingestion
manifests and the original corpus bytes it re-derives: exact case membership and count, per-scope source and
section availability, trusted source identities, every evidence passage's bytes/metadata, citation
quotes/tags/URLs/accessions, state order, retry and context bounds, carried-evidence continuity, and the
behaviour required for unavailable scopes (including mixed available/unavailable comparisons).

Structural results are not answer accuracy. Anchors are reviewer evidence of source-informed fixtures and are only
reported (retrieved/cited), never required. Honest partial gaps and abstentions on available scopes are reported
separately from transport/model/structural failures and from verification failures.
"""
import argparse
import collections
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_suite as lib  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
STATES = ['Plan', 'Retrieve_Evaluate', 'Synthesize']
MAX_RETRIES, MAX_CONTEXT, MAX_CARRIED = 2, 5, 2
CASE_FILE = re.compile(r'^((?:audit|broad)\d{2})(\.failure)?\.(json|md)$')
SUMMARY_FILES = {'report.json', 'records.jsonl', 'suite.json', 'suite.sha256', 'run-manifest.json'}
DASHED = re.compile(r'\d{10}-\d{2}-\d{6}')
# Fields the evaluator reports per passing case; every one is recomputed independently and compared.
COMPARE_FIELDS = ('subqueries', 'retrieval_calls', 'retries', 'accepted_subqueries', 'cited_subqueries', 'citations', 'complete_coverage',
                  'negative', 'evidence_outcome', 'unavailable_subqueries', 'flags', 'model_calls')
IMMUTABLE_TREES = ('vector_index', 'reranker_model', 'sparse_index')
COVERAGE_GATE = 0.80


def safe_path(root, relative):
    path = (Path(root) / relative).resolve()
    if Path(relative).is_absolute() or not path.is_relative_to(Path(root).resolve()):
        return None
    return path


def verify_artifacts(hashes, root, suite_digest, corpus_sha, ingest_report, original_provenance, strict_code):
    """Re-hash recorded files from their recorded paths. Returns (report, problems).

    `recomputed_verified` means bytes on disk now hash to the recorded value; presence alone is never counted. Index and model
    trees are immutable: any change, missing file or unrecorded file is a problem. Code drift is reported separately and is
    a problem only with `strict_code` (historical replay legitimately runs against newer code).
    """
    problems, report = [], {}
    for key, expected in (('suite_sha256', suite_digest), ('corpus_sha256', corpus_sha), ('ingest_report_sha256', lib.sha256_file(ingest_report)),
                          ('original_provenance_sha256', lib.sha256_file(original_provenance))):
        report[key] = 'recomputed_verified' if hashes.get(key) == expected else 'MISMATCH'
        if hashes.get(key) != expected:
            problems.append(f'artifact hash {key} differs from the recomputed value')
    for tree in IMMUTABLE_TREES:
        recorded = hashes.get(tree) or {}
        entry = dict(recorded_files=len(recorded), recomputed_verified=0, mismatched=[], missing=[], unrecorded=[])
        if not recorded:
            problems.append(f'artifact-hashes.json records no files for {tree}')
        directories = {str(Path(r).parent) for r in recorded}
        for relative, value in recorded.items():
            path = safe_path(root, relative)
            if path is None:
                problems.append(f'{tree}: unsafe recorded path {relative!r}')
            elif not path.is_file():
                entry['missing'].append(relative)
            elif lib.sha256_file(path) != value:
                entry['mismatched'].append(relative)
            else:
                entry['recomputed_verified'] += 1
        for directory in directories:
            base = safe_path(root, directory)
            if base is not None and base.is_dir():
                entry['unrecorded'] += sorted(str(f.relative_to(Path(root).resolve())) for f in base.rglob('*') if f.is_file() and str(f.relative_to(Path(root).resolve())) not in recorded)
        for kind in ('mismatched', 'missing', 'unrecorded'):
            if entry[kind]:
                problems.append(f'{tree}: {len(entry[kind])} {kind} immutable file(s), e.g. {entry[kind][0]}')
        report[tree] = entry
    code = hashes.get('code') or {}
    drift = dict(changed=[], missing=[], recomputed_unchanged=0)
    for relative, value in code.items():
        path = safe_path(root, relative)
        if path is None or not path.is_file():
            drift['missing'].append(relative)
        elif lib.sha256_file(path) != value:
            drift['changed'].append(relative)
        else:
            drift['recomputed_unchanged'] += 1
    drift['required_but_unrecorded'] = [r for r in lib.REQUIRED_CODE if r not in code]
    if drift['required_but_unrecorded']:
        problems.append(f"code hashes omit required implementation files: {drift['required_but_unrecorded']}")
    drift['strict'] = strict_code
    if strict_code and (drift['changed'] or drift['missing']):
        problems.append(f"strict code check: {len(drift['changed'])} changed, {len(drift['missing'])} missing since the run")
    report['code'] = drift
    return report, problems


def escape_markdown(text):
    return re.sub(r'([\\`*_{}\[\]<>()#!|~])', r'\\\1', text)


class Checks:
    def __init__(self):
        self.problems = []

    def req(self, condition, message):
        if not condition:
            self.problems.append(message)
        return bool(condition)


def trusted_source_checks(sources, checks):
    seen_accession, seen_scope = set(), set()
    for source in sources:
        accession, url = source['accession'], urlparse(source['url'])
        checks.req(re.fullmatch(r'\d{18}', accession) is not None and accession not in seen_accession, f'bad/duplicate accession {accession}')
        checks.req(url.scheme == 'https' and url.netloc == 'www.sec.gov' and not url.query and not url.fragment
                   and url.path.startswith(f"/Archives/edgar/data/{source['cik']}/{accession}/"), f'untrusted source URL for {accession}')
        scope = (source['ticker'], source['filing_year'])
        checks.req(scope not in seen_scope, f'two filings for {scope}')
        seen_accession.add(accession)
        seen_scope.add(scope)


def verify_anchors(suite, sources_by_accession, originals, checks):
    for case in suite['cases']:
        for anchor in case.get('reviewer_evidence', {}).get('anchors', []):
            label = f"{case['id']} anchor {anchor['id']}"
            chunk = originals.get(anchor['id'])
            if not checks.req(chunk is not None, f'{label}: passage missing from corpus'):
                continue
            source = sources_by_accession.get(anchor['id'].split('#')[0])
            checks.req(source is not None and (source['ticker'], source['filing_year']) == (anchor['ticker'], anchor['year']), f'{label}: scope mismatch')
            checks.req(lib.sha256_text(chunk['Text']) == anchor['passage_text_sha256'], f'{label}: passage bytes changed since freeze')
            checks.req(anchor['excerpt'] in lib.normalize(chunk['Text']), f'{label}: excerpt not in passage')
            checks.req(chunk['SectionItem'] == anchor['section_item'] and (not case['section'] or chunk['SectionItem'] == case['section']), f'{label}: section mismatch')


def audit_evidence(item, scope, section, source, originals, checks, where):
    original = originals.get(item['id'])
    if not checks.req(original is not None, f'{where}: evidence {item["id"]} not in corpus'):
        return
    match = lib.PASSAGE_ID.fullmatch(item['id'])
    if not checks.req(match is not None, f'{where}: malformed id {item["id"]}'):
        return
    accession, section_component, number = match.groups()
    metadata = {k: v for k, v in original.items() if k not in ('Text', 'Tokens')}
    checks.req(item['text'] == original['Text'], f'{where}: text differs from original bytes ({item["id"]})')
    checks.req(item['metadata'] == metadata, f'{where}: metadata differs from original ({item["id"]})')
    checks.req(lib.SECTION_COMPONENT.get(section_component) == metadata['SectionItem'], f'{where}: id section component != SectionItem ({item["id"]})')
    checks.req(accession == scope['accession'], f'{where}: SCOPE LEAK {item["id"]} is outside requested {scope["ticker"]} {scope["year"]}')
    if section:
        checks.req(metadata['SectionItem'] == section, f'{where}: SCOPE LEAK {item["id"]} section {metadata["SectionItem"]} != exact {section}')
    for key, value in [('Ticker', scope['ticker']), ('FilingYear', scope['year']), ('CIK', source['cik']), ('FormType', source['form'])]:
        checks.req(metadata.get(key) == value, f'{where}: {key} mismatch ({item["id"]})')
    suffix = f'{section_component}-p{number}' if section_component else f'p{number}'
    checks.req(item['accession'] == f'{accession[:10]}-{accession[10:12]}-{accession[12:]}', f'{where}: accession formatting')
    checks.req(item['url'] == source['url'], f'{where}: url differs from trusted enrollment')
    checks.req(item['tag'] == f"{source['ticker']}-{source['filing_year']}-{source['form'].replace('-', '')}-{suffix}", f'{where}: tag mismatch')


def audit_case(case, result, markdown_file, sources_by_scope, originals, checks):
    """Return per-case metrics. Every check appends a problem instead of stopping."""
    cid = case['id']
    req = checks.req
    req(result.get('prompt') == case['prompt'], f'{cid}: saved prompt differs from the frozen suite')
    req(result.get('states') == STATES, f'{cid}: invalid state order {result.get("states")}')
    expected = {(e['ticker'], e['year']): e for e in case['expected_scopes']}
    subqueries = result['plan']['subqueries']
    req(2 <= len(subqueries) <= 3, f'{cid}: {len(subqueries)} subqueries')
    req({(q['filters']['ticker'], q['filters']['year']) for q in subqueries} == set(expected), f'{cid}: plan scopes differ from requested scopes')
    hops_by_query = collections.defaultdict(list)
    for hop in result['hops']:
        req(0 <= hop['subquery'] < len(subqueries), f'{cid}: hop for nonexistent subquery {hop["subquery"]}')
        hops_by_query[hop['subquery']].append(hop)
    accepted_evidence, unavailable_positions, retrieved_ids = {}, [], set()
    squash = lambda text: ' '.join(text.split()).casefold()
    goals = [q.get('goal') for q in subqueries]
    req(all(g is None for g in goals) or all(isinstance(g, str) and g for g in goals), f'{cid}: goals must be present and non-empty for every subquery or absent (legacy)')
    for position, query in enumerate(subqueries):
        if isinstance(query.get('goal'), str) and query['goal']:  # user goal: extractive, bounded, constant across every hop
            req(len(query['goal']) <= 400 and squash(query['goal']) in squash(result.get('prompt', '')), f'{cid}/sq{position}: goal is not a contiguous span of the audit prompt')
        elif 'goal' in query:
            req(False, f'{cid}/sq{position}: invalid goal')
        filters = query['filters']
        scope = expected.get((filters['ticker'], filters['year']))
        if scope is None:
            continue
        where = f'{cid}/sq{position}'
        source = sources_by_scope.get((scope['ticker'], scope['year']))
        req(filters['section'] == case['section'], f'{where}: plan changed exact section "{filters["section"]}"')
        req(filters['accession'] == scope['accession'], f'{where}: accession {filters["accession"]!r} != trusted {scope["accession"]!r}')
        hops = hops_by_query.get(position, [])
        req(1 <= len(hops) <= MAX_RETRIES + 1 and [h['attempt'] for h in hops] == list(range(len(hops))), f'{where}: attempts {[h["attempt"] for h in hops]}')
        if not hops:
            continue
        req(hops[0]['query'] == query['query'], f'{where}: first search differs from the planned query')
        req(hops[-1]['accepted'] or len(hops) == MAX_RETRIES + 1 or hops[-1].get('terminal_reason') == 'stalled_rewrite',
            f'{where}: stopped before accepting, exhausting retries or a valid stalled-rewrite stop')
        previous = {}
        for index, hop in enumerate(hops):
            req(hop['filters'] == filters, f'{where}/h{index}: rewrite changed immutable filters')
            req(hop.get('requested_goal', '') == query.get('goal', ''), f'{where}/h{index}: graded goal differs from the planned user goal')
            req(not (hop['accepted'] and index < len(hops) - 1), f'{where}/h{index}: retried after acceptance')
            req(1 <= hop['score'] <= 5, f'{where}/h{index}: score {hop["score"]}')
            evidence = hop['evidence']
            ids = [e['id'] for e in evidence]
            req(len(ids) == len(set(ids)) <= MAX_CONTEXT, f'{where}/h{index}: context {len(ids)} passages / duplicates')
            req(hop['accepted'] == (bool(evidence) and hop['score'] >= 4), f'{where}/h{index}: acceptance inconsistent with score/evidence')
            carried = hop.get('retained_evidence_ids', [])
            req(len(carried) <= MAX_CARRIED and len(set(carried)) == len(carried), f'{where}/h{index}: carried passages {len(carried)}')
            current = {e['id']: e for e in evidence}
            req(all(c in previous and current.get(c) == previous[c] for c in carried), f'{where}/h{index}: carried evidence not identical to the previous hop')
            req(ids[:len(carried)] == carried, f'{where}/h{index}: carried passages are not first in the merged context')
            trace_keys = [k for k in ('nominated_retained_ids', 'retrieved_ids', 'fresh_verified_ids') if k in hop]
            req(len(trace_keys) in (0, 3), f'{where}/h{index}: partial selection trace')
            if len(trace_keys) == 3:  # Newer runs: independently recompute the context budget (legacy traces lack these keys).
                nominated, retrieved, fresh = hop['nominated_retained_ids'], hop['retrieved_ids'], hop['fresh_verified_ids']
                listed = all(isinstance(v, list) and all(isinstance(x, str) for x in v) for v in (nominated, retrieved, fresh))
                req(listed, f'{where}/h{index}: selection trace must be lists of IDs')
                if listed:
                    req(len(retrieved) <= 10 and len(set(retrieved)) == len(retrieved), f'{where}/h{index}: retrieved IDs exceed 10 or repeat')
                    req(len(fresh) <= MAX_CONTEXT and len(set(fresh)) == len(fresh), f'{where}/h{index}: fresh verified IDs exceed 5 or repeat')
                    req(len(nominated) <= MAX_CARRIED and len(set(nominated)) == len(nominated), f'{where}/h{index}: nominees exceed 2 or repeat')
                    req(fresh == [r for r in retrieved if r in set(fresh)], f'{where}/h{index}: fresh verified IDs are not an ordered subset of retrieved IDs')
                    req(nominated == [i for i in previous if i in set(nominated)] and set(nominated) <= set(previous),
                        f'{where}/h{index}: nominees are not previous-context passages in context order')
                    # Independent recomputation: top-4 fresh reserved; nominees (in order) take the remaining slots.
                    reserved = fresh[:4]
                    free = MAX_CONTEXT - len(reserved)
                    expected_carried = []
                    for nominee in nominated:
                        if nominee in reserved:
                            expected_carried.append(nominee)
                        elif free > 0:
                            expected_carried.append(nominee)
                            free -= 1
                    req(carried == expected_carried, f'{where}/h{index}: carried {carried} differs from the budget result {expected_carried}')
                    expected_context = (expected_carried + [f for f in fresh if f not in expected_carried])[:MAX_CONTEXT]
                    req(ids == expected_context, f'{where}/h{index}: context differs from carried-then-fresh budget')
            rewrite_keys = [k for k in ('proposed_rewrite', 'terminal_reason') if k in hop]
            req(len(rewrite_keys) in (0, 2), f'{where}/h{index}: partial rewrite trace')
            if len(rewrite_keys) == 2:  # Newer runs; legacy hops lack both keys and keep the original exhaustion contract.
                proposed, terminal = hop['proposed_rewrite'], hop['terminal_reason']
                req(isinstance(proposed, str) and len(proposed) <= 1000 and terminal in (None, 'stalled_rewrite'), f'{where}/h{index}: malformed rewrite trace')
                if isinstance(proposed, str) and terminal in (None, 'stalled_rewrite'):
                    same = proposed.strip().casefold() == hop['query'].strip().casefold()
                    last_hop = index == len(hops) - 1
                    if hop['accepted']:
                        req(proposed == '' and terminal is None, f'{where}/h{index}: accepted hop carries a rewrite or terminal reason')
                    else:
                        req(len(proposed.strip()) >= 4, f'{where}/h{index}: non-accepted hop did not record the critic rewrite')
                        if index < MAX_RETRIES and same:  # an unchanged rewrite can only be a marked empty-evidence stall
                            req(terminal == 'stalled_rewrite', f'{where}/h{index}: unchanged rewrite not marked as a stalled stop')
                        if not last_hop:
                            req(hops[index + 1]['query'] == proposed, f'{where}/h{index}: next query is not the recorded critic rewrite')
                    if terminal == 'stalled_rewrite':
                        req(last_hop and index < MAX_RETRIES and not hop['accepted'] and hop['score'] == 1 and not evidence and same,
                            f'{where}/h{index}: invalid stalled-rewrite stop (needs final hop before the limit, score 1, no evidence, unchanged normalized rewrite)')
            if index + 1 < len(hops):
                req(hops[index + 1]['query'].strip().casefold() != hop['query'].strip().casefold(), f'{where}/h{index}: critic did not rewrite')
            for item in evidence:
                retrieved_ids.add(item['id'])
                if source is not None:
                    audit_evidence(item, scope, case['section'], source, originals, checks, f'{where}/h{index}')
                else:
                    req(False, f'{where}/h{index}: evidence returned for a scope with no trusted filing')
            if hop['accepted']:
                accepted_evidence.update(current)
            previous = current
        if not scope['available']:
            unavailable_positions.append(position)
            req(all(h['score'] == 1 and not h['accepted'] and not h['evidence'] for h in hops), f'{where}: unavailable scope produced evidence/acceptance')
            stalled_stop = hops[-1].get('terminal_reason') == 'stalled_rewrite'  # validity of the marker is checked per hop above
            req(len(hops) == MAX_RETRIES + 1 or stalled_stop, f'{where}: unavailable scope neither exhausted {MAX_RETRIES} retries nor stopped on a valid stalled rewrite')
            req(stalled_stop or len({h['query'] for h in hops}) >= 2, f'{where}: unavailable scope never rewrote its query')
    available_accessions = {e['accession'] for e in expected.values() if e['available']}
    requested_accessions = {e['accession'] for e in expected.values() if e['accession']}
    # Citations
    citations = result['citations']
    seen = set()
    for number, citation in enumerate(citations):
        where = f'{cid}/citation{number}'
        chunk = citation['chunk_id']
        item = accepted_evidence.get(chunk)
        if not req(item is not None, f'{where}: cites {chunk}, which is not accepted evidence'):
            continue
        req(chunk.split('#')[0] in available_accessions, f'{where}: SCOPE LEAK citation from unavailable/unrequested scope')
        req(chunk.split('#')[0] in requested_accessions, f'{where}: citation outside requested scopes')
        original = originals.get(chunk)
        req(original is not None and 40 <= len(citation['quote']) <= 2000 and citation['quote'] in lib.normalize(original['Text']),
            f'{where}: quote is not a verbatim passage excerpt')
        req(DASHED.fullmatch(citation['accession']) is not None and (citation['tag'], citation['accession'], citation['url']) == (item['tag'], item['accession'], item['url']),
            f'{where}: tag/accession/url differ from the verified evidence')
        req((chunk, citation['quote']) not in seen, f'{where}: duplicate citation')
        seen.add((chunk, citation['quote']))
        line = f"- {escape_markdown(citation['quote'])} [{citation['tag']}]({citation['url']}) (chunk: `{chunk}`; accession: `{citation['accession']}`)."
        req(line in result['markdown'], f'{where}: inline citation line missing or altered')
    # Flags and gap reporting
    cited_ids = {c['chunk_id'] for c in citations}
    cited_positions = []
    for position in range(len(subqueries)):
        hops = hops_by_query.get(position, [])
        last = hops[-1] if hops else None
        cited = bool(last and last['accepted'] and cited_ids.intersection(e['id'] for e in last['evidence']))
        cited_positions.append(cited)
        if last is None:
            continue
        gap = f'Subquery {position} has no evidence meeting threshold after {len(hops) - 1} retries.'
        req((gap in result['flags']) == (not last['accepted']), f'{cid}/sq{position}: evidence-gap flag inconsistent with outcome')
        stop = f'Subquery {position} stopped early: no verified evidence and the critic proposed no new query (stalled rewrite).'
        req((stop in result['flags']) == (last.get('terminal_reason') == 'stalled_rewrite'), f'{cid}/sq{position}: stop-reason flag inconsistent with the trace')
        silent = f'Subquery {position} has retrieved evidence but no published supporting quote.'
        req((silent in result['flags']) == (last['accepted'] and not cited), f'{cid}/sq{position}: unquoted-evidence flag inconsistent')
    if not accepted_evidence:
        req(not citations and 'No verified answer; evidence unavailable or below threshold.' in result['flags'], f'{cid}: abstention not explicit')
    if result['flags']:
        req('Evidence gaps:' in result['markdown'] and all('- ' + escape_markdown(f) in result['markdown'] for f in result['flags']), f'{cid}: flags missing from markdown')
    req(not unavailable_positions or (not any(cited_positions[p] for p in unavailable_positions)), f'{cid}: unavailable scope was cited')
    if len(unavailable_positions) == len(subqueries):
        req(not citations, f'{cid}: all scopes unavailable but citations were published')
    counts = collections.Counter(call['schema'] for call in result['model_calls'])
    synthesis = counts.get('Draft', 0) + counts.get('HandleDraft', 0)
    allowed = {'Plan', 'Critique', 'Draft', 'HandleDraft'}
    req(set(counts) <= allowed, f'{cid}: unknown model-call schemas {sorted(set(counts) - allowed)}')
    req(counts.get('Plan', 0) == 1 and counts.get('Critique', 0) == len(result['hops'])
        and synthesis == (1 if accepted_evidence else 0) and not (counts.get('Draft', 0) and counts.get('HandleDraft', 0))
        and len(result['model_calls']) == 1 + len(result['hops']) + synthesis,
        f'{cid}: model-call ledger {dict(counts)} inconsistent with trace')
    req(all(call['models'] for call in result['model_calls']), f'{cid}: model call without a recorded model id')
    req(markdown_file is not None and markdown_file.rstrip() == result['markdown'].rstrip(), f'{cid}: saved markdown differs from result JSON')
    available_positions = [p for p in range(len(subqueries)) if p not in unavailable_positions]
    outcome = 'expected_unavailable' if len(unavailable_positions) == len(subqueries) else lib.evidence_outcome(
        [f for f in result['flags'] if not any(f.startswith(f'Subquery {p} ') for p in unavailable_positions)], len(citations),
        len(available_positions), sum(cited_positions[p] for p in available_positions))
    anchors = case.get('reviewer_evidence', {}).get('anchors', [])
    negative = len(unavailable_positions) == len(subqueries)
    cited_available = sum(cited_positions[p] for p in available_positions)
    mixed = bool(unavailable_positions) and not negative
    return dict(subqueries=len(subqueries), retrieval_calls=len(result['hops']), retries=sum(h['attempt'] > 0 for h in result['hops']),
        accepted_subqueries=sum(h['accepted'] for h in result['hops']), negative=negative, model_calls=len(result['model_calls']),
        complete_coverage=(not negative and sum(cited_positions) == len(subqueries)),
        available_subqueries=len(available_positions), cited_available_subqueries=cited_available,
        unavailable_withheld=all(not cited_positions[p] and not any(c['chunk_id'].split('#')[0] == expected[(subqueries[p]['filters']['ticker'], subqueries[p]['filters']['year'])]['accession'] for c in citations) for p in unavailable_positions),
        mixed_result=(None if not mixed else 'mixed_fully_supported' if cited_available == len(available_positions) else 'mixed_available_side_gap'),
        citations=len(citations), cited_subqueries=sum(cited_positions), evidence_outcome=outcome,
        unavailable_subqueries=unavailable_positions, mixed_availability=mixed,
        flags=result['flags'], seconds=result['elapsed_seconds'], models=sorted({m for c in result['model_calls'] for m in c['models']}),
        anchors_retrieved=[a['id'] for a in anchors if a['id'] in retrieved_ids], anchors_cited=[a['id'] for a in anchors if a['id'] in cited_ids],
        anchors_total=len(anchors))


def compare_evaluator_summary(evaluation, expected_ids, records, results):
    """Duplicate IDs, ledger membership, per-case metric fields and aggregate counts versus independent recomputation."""
    found = []
    by_id = {r['id']: r for r in records}
    ledgers = {}
    for name in ('report.json', 'records.jsonl'):
        path = evaluation / name
        if not path.is_file():
            found.append(f'evaluation/{name} missing')
            continue
        try:
            if name == 'report.json':
                report = json.loads(path.read_text())
                rows = report.get('cases', [])
            else:
                rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        except (ValueError, AttributeError) as exc:
            found.append(f'evaluation/{name} unreadable: {exc}')
            continue
        ids = [row.get('id') for row in rows]
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            found.append(f'{name}: duplicate case IDs {duplicates}')
        if sorted(i for i in ids if i is not None) != sorted(expected_ids) and not duplicates:
            found.append(f'{name}: case IDs differ from the frozen selection (missing {sorted(set(expected_ids) - set(ids))}, extra {sorted(set(ids) - set(expected_ids))})')
        ledgers[name] = rows
        for row in rows:
            record = by_id.get(row.get('id'))
            if record is None:
                continue
            if (row.get('status') == 'pass') != (record['verdict'] == 'verified'):
                found.append(f"{name}/{row.get('id')}: evaluator says {row.get('status')}, independent verdict {record['verdict']}")
            elif record['verdict'] == 'verified':
                for field in COMPARE_FIELDS:
                    if row.get(field) != record.get(field):
                        found.append(f"{name}/{row['id']}: reported {field}={row.get(field)!r} but recomputed {record.get(field)!r}")
                if abs(row.get('seconds', -1) - record['seconds']) > 1e-9:
                    found.append(f"{name}/{row['id']}: reported seconds differ from the saved trace")
            elif record['verdict'] == 'run_failed' and row.get('category') != record.get('failure_category'):
                found.append(f"{name}/{row['id']}: reported failure category {row.get('category')!r} != saved failure record")
    if 'report.json' in ledgers and 'records.jsonl' in ledgers and ledgers['report.json'] != ledgers['records.jsonl']:
        found.append('report.json cases differ from the records.jsonl ledger')
    if (evaluation / 'report.json').is_file():
        try:
            report = json.loads((evaluation / 'report.json').read_text())
        except ValueError:
            return found
        ok = [r for r in records if r['verdict'] == 'verified']
        seconds = sorted(r['seconds'] for r in ok)
        median = None if not seconds else (seconds[len(seconds) // 2] if len(seconds) % 2 else (seconds[len(seconds) // 2 - 1] + seconds[len(seconds) // 2]) / 2)
        expected = dict(passed=len(ok), total=len(expected_ids), citations=sum(r['citations'] for r in ok),
            retrieval_calls=sum(r['retrieval_calls'] for r in ok), retries=sum(r['retries'] for r in ok),
            negative_cases=sum(r['negative'] for r in ok), fully_covered_positive_cases=sum(r['complete_coverage'] for r in ok),
            failures_by_category={k: sum(r.get('failure_category') == k for r in records) for k in lib.FAILURE_CATEGORIES},
            evidence_outcomes={k: sum(r.get('evidence_outcome') == k for r in ok) for k in lib.EVIDENCE_OUTCOMES}, selected=expected_ids)
        for key, value in expected.items():
            if report.get(key) != value:
                found.append(f'report.json aggregate {key}={report.get(key)!r} but recomputed {value!r}')
        reported_median = report.get('median_seconds')
        if (reported_median is None) != (median is None) or (median is not None and abs(reported_median - median) > 1e-9):
            found.append(f'report.json median_seconds={reported_median!r} but recomputed {median!r}')
    return found


def release_gates(summary, records):
    """Structural gates the manager set before inference. True/False are computed here; None = not checkable offline.

    Coverage is the share of planned subqueries on AVAILABLE scopes with an independently verified published citation. It is a
    structural threshold, not semantic accuracy. An honest available-side gap is a structural pass but fails these gates.
    """
    available, cited = summary['available_subqueries'], summary['cited_available_subqueries']
    ratio = cited / available if available else None
    mixed = summary['mixed_cases']
    unavailable = [r for r in records if r.get('unavailable_subqueries')]
    return dict(
        all_frozen_cases_retained=summary['verified'] + summary['run_failed'] + summary['verification_failed'] == len(summary['expected_case_ids']) and not summary['omitted'],
        no_failures_or_violations=summary['passed'],
        unavailable_scopes_publish_zero_claims=all(r.get('unavailable_withheld') for r in unavailable if r['verdict'] == 'verified') and all(r['verdict'] == 'verified' for r in unavailable),
        mixed_cases_preserve_unavailable_and_fully_support_available=bool(mixed) and all(m['result'] == 'mixed_fully_supported' and m['unavailable_withheld'] for m in mixed.values()),
        citation_coverage_of_available_subqueries=dict(cited=cited, available=available, ratio=ratio, threshold=COVERAGE_GATE, met=ratio is not None and ratio >= COVERAGE_GATE),
        not_checked_by_this_tool=['full Python suite', 'Go race/vet', 'public snapshot has no weights/datasets/credentials/private contact info'],
        all_checked_gates_met=None)


def verify(directory, suite_path, pin, corpus, original_provenance, ingest_report, allow_subset=False, root=ROOT, strict_code=False):
    global_checks = Checks()
    gp = global_checks.req
    directory = Path(directory)
    suite, suite_digest = lib.load_suite(suite_path, pin)
    gp(lib.sha256_file(corpus) == suite['corpus_sha256'], 'corpus bytes differ from the corpus the suite was frozen against')
    # --- run inputs: the saved suite must be the pinned suite, written before inference ---
    for relative in ('suite.json', 'evaluation/suite.json'):
        path = directory / relative
        if gp(path.is_file(), f'missing {relative}'):
            gp(lib.sha256_file(path) == suite_digest, f'{relative} differs from the pinned frozen suite')
    gp((directory / 'suite.sha256').is_file() and (directory / 'suite.sha256').read_text().split()[0] == suite_digest, 'suite.sha256 missing or wrong')
    manifest_path = directory / 'evaluation' / 'run-manifest.json'
    manifest = json.loads(manifest_path.read_text()) if manifest_path.is_file() else {}
    gp(bool(manifest) and manifest.get('suite_sha256') == suite_digest, 'run-manifest.json missing or names a different suite')
    expected_ids = suite['case_ids']
    selected = manifest.get('selected', [])
    if selected != expected_ids:
        if gp(allow_subset and selected and set(selected) <= set(expected_ids), f'partial run {selected} requires --allow-subset'):
            expected_ids = [i for i in expected_ids if i in selected]
    # --- artifacts + trusted enrollment re-derived independently ---
    enrolled = json.loads((directory / 'agent-provenance.json').read_text())
    trusted = lib.build_trusted_sources(original_provenance, ingest_report)
    gp(enrolled['sources'] == trusted, 'run enrollment differs from the independently rebuilt trusted source list')
    gp(enrolled['corpus_sha256'] == suite['corpus_sha256'], 'run enrollment names a different corpus')
    gp(len(trusted) == suite['enrolled_filings'], 'enrolled filing count differs from frozen suite')
    trusted_source_checks(trusted, global_checks)
    # Lineage: a mixed-parser corpus must declare it, retain previous raw identities and leave untouched filings exactly as before.
    lineage_info, lineage_problems = lib.check_report_lineage(ingest_report, root)
    global_checks.problems += lineage_problems
    hashes_path = directory / 'artifact-hashes.json'
    artifact_report = {}
    if gp(hashes_path.is_file(), 'artifact-hashes.json missing'):
        artifact_report, artifact_problems = verify_artifacts(json.loads(hashes_path.read_text()), root, suite_digest, suite['corpus_sha256'],
                                                              ingest_report, original_provenance, strict_code)
        global_checks.problems += artifact_problems
        recorded_previous = json.loads(hashes_path.read_text()).get('previous_ingest_report_sha256')
        if lineage_info.get('previous_report_sha256') is not None or recorded_previous is not None:
            gp(recorded_previous == lineage_info.get('previous_report_sha256'), 'artifact-hashes previous_ingest_report_sha256 differs from the report lineage')
    status_path = directory / 'run-status.json'
    run_status = json.loads(status_path.read_text()) if status_path.is_file() else {}
    gp(bool(run_status) and run_status.get('status') in ('completed', 'failed') and run_status.get('owned_processes_stopped') is True,
       'run-status.json missing, unfinished, or owned processes not confirmed stopped')
    # --- exact membership: every frozen case has exactly one outcome, nothing extra ---
    evaluation = directory / 'evaluation'
    files = collections.defaultdict(set)
    unexpected = []
    for path in sorted(evaluation.iterdir()) if evaluation.is_dir() else []:
        found = CASE_FILE.match(path.name)
        if found:
            cid, failure, extension = found.groups()
            if cid not in expected_ids:
                unexpected.append(path.name)
            files[cid].add(('failure.' if failure else '') + extension)
        elif path.name not in SUMMARY_FILES:
            unexpected.append(path.name)
    gp(not unexpected, f'unexpected files in evaluation/: {unexpected}')
    # --- one streaming pass over the original bytes ---
    cases = {c['id']: c for c in suite['cases']}
    results, failures, state = {}, {}, {}
    for cid in expected_ids:
        present = files.get(cid, set())
        has_result = {'json', 'md'} <= present
        failure = None
        if 'failure.json' in present:
            failure = json.loads((evaluation / f'{cid}.failure.json').read_text())
        if not present:
            state[cid] = 'omitted'
        elif failure is not None:
            failures[cid] = failure
            ok_shape = (failure.get('id') == cid and failure.get('status') == 'fail' and failure.get('category') in lib.FAILURE_CATEGORIES
                        and failure.get('error') and failure.get('traceback') and failure.get('phase') in ('agent_run', 'in_process_verification'))
            state[cid] = 'run_failed' if ok_shape else 'bad_failure_record'
            if failure.get('phase') == 'in_process_verification' and has_result:
                results[cid] = json.loads((evaluation / f'{cid}.json').read_text())
        elif has_result:
            results[cid] = json.loads((evaluation / f'{cid}.json').read_text())
            state[cid] = 'completed'
        else:
            state[cid] = 'incomplete_files'
    needed = {e['id'] for r in results.values() for h in r['hops'] for e in h['evidence']}
    anchors = {a['id'] for c in suite['cases'] for a in c.get('reviewer_evidence', {}).get('anchors', [])}
    gp(len(needed) <= 1500, 'trace evidence exceeds the bounded protocol')
    sections, originals = lib.scan_corpus(corpus, {s['accession'] for s in trusted}, needed | anchors)
    gp(set(originals) == needed | anchors, f'passages missing from corpus: {sorted((needed | anchors) - set(originals))[:5]}')
    try:
        lib.check_expected_scopes(suite, trusted, sections)
    except lib.SuiteError as exc:
        gp(False, f'frozen availability expectations no longer hold: {exc}')
    by_accession = {s['accession']: s for s in trusted}
    by_scope = {(s['ticker'], s['filing_year']): s for s in trusted}
    verify_anchors(suite, by_accession, originals, global_checks)
    # --- cases ---
    records = []
    for cid in expected_ids:
        case = cases[cid]
        record = dict(id=cid, kind=case['kind'], state=state[cid], problems=[])
        if cid in failures:
            f = failures[cid]
            record.update(failure_category=f.get('category'), failure_phase=f.get('phase'), failure_error=str(f.get('error'))[:500])
        if state[cid] == 'omitted':
            record['problems'].append(f'{cid}: OMITTED - no result or failure record in evaluation/')
        elif state[cid] in ('incomplete_files', 'bad_failure_record'):
            record['problems'].append(f'{cid}: {state[cid]}')
        if cid in results:
            checks = Checks()
            markdown_path = evaluation / f'{cid}.md'
            markdown = markdown_path.read_text() if markdown_path.is_file() else None
            try:
                record.update(audit_case(case, results[cid], markdown, by_scope, originals, checks))
            except (KeyError, TypeError, IndexError) as exc:
                checks.req(False, f'{cid}: malformed result JSON ({type(exc).__name__}: {exc})')
            record['problems'] += checks.problems
        record['verdict'] = ('verified' if state[cid] == 'completed' and not record['problems'] else
                             'run_failed' if state[cid] == 'run_failed' and not [p for p in record['problems']] else 'verification_failed')
        records.append(record)
    # --- compare, never trust, the evaluator's own summary; any disagreement fails the run ---
    discrepancies = compare_evaluator_summary(evaluation, expected_ids, records, results)
    # Suite must be saved before the first request: compare file times as an extra, weak, check.
    outputs = [p.stat().st_mtime for cid in results for p in [evaluation / f'{cid}.json']]
    saved = [p.stat().st_mtime for p in (directory / 'suite.json', evaluation / 'suite.json') if p.is_file()]
    gp(not outputs or not saved or max(saved) <= min(outputs), 'suite copy is newer than the first case result')
    verified = [r for r in records if r['verdict'] == 'verified']
    groups = collections.Counter(r.get('evidence_outcome') for r in verified)
    summary = dict(
        suite_id=suite['suite_id'], suite_sha256=suite_digest, corpus_sha256=suite['corpus_sha256'], frozen_case_count=suite['case_count'],
        expected_case_ids=expected_ids, full_suite=expected_ids == suite['case_ids'], enrolled_filings=len(trusted),
        verified=len(verified), run_failed=sum(r['verdict'] == 'run_failed' for r in records),
        verification_failed=sum(r['verdict'] == 'verification_failed' for r in records),
        omitted=[r['id'] for r in records if r['state'] == 'omitted'],
        failures_by_category={k: sum(r.get('failure_category') == k for r in records) for k in lib.FAILURE_CATEGORIES},
        evidence_outcomes={k: groups.get(k, 0) for k in lib.EVIDENCE_OUTCOMES},
        mixed_availability_cases_verified=[r['id'] for r in verified if r.get('mixed_availability')],
        verified_citations=sum(r.get('citations', 0) for r in verified), unique_source_passages_checked=len(needed),
        anchors=dict(total=sum(r.get('anchors_total', 0) for r in verified), retrieved=sum(len(r.get('anchors_retrieved', [])) for r in verified),
                     cited=sum(len(r.get('anchors_cited', [])) for r in verified),
                     note='Informational reviewer-evidence overlap for source-informed fixtures. Not recall, not accuracy.'),
        coverage=lib.suite_coverage(suite), global_problems=global_checks.problems, evaluator_summary_discrepancies=discrepancies,
        artifact_verification=artifact_report, ingest_lineage=lineage_info,
        available_subqueries=sum(r.get('available_subqueries', 0) for r in verified), cited_available_subqueries=sum(r.get('cited_available_subqueries', 0) for r in verified),
        mixed_cases={r['id']: dict(result=r['mixed_result'], available_side=f"{r['cited_available_subqueries']}/{r['available_subqueries']}", unavailable_withheld=r['unavailable_withheld']) for r in verified if r.get('mixed_availability')},
        run_status=run_status.get('status'), cases=records,
        scope='Frozen-suite membership, scope availability, source identity/bytes, citations, bounds, continuity and mixed-availability behaviour. '
              'Source-informed regression fixtures; not independent human gold and not an answer-accuracy score.')
    summary['passed'] = not global_checks.problems and not discrepancies and len(verified) == len(expected_ids)
    gates = summary['release_gates'] = release_gates(summary, records)
    gates['all_checked_gates_met'] = all([gates['all_frozen_cases_retained'], gates['no_failures_or_violations'], gates['unavailable_scopes_publish_zero_claims'],
        gates['mixed_cases_preserve_unavailable_and_fully_support_available'], gates['citation_coverage_of_available_subqueries']['met']])
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--suite', type=Path, default=ROOT / 'bench_kit/suites/broad-validation-v1.json')
    parser.add_argument('--expected-suite-sha256', help='Defaults to the hash recorded beside the suite file')
    parser.add_argument('--corpus', type=Path, default=ROOT / 'data/sec-scale-v2-evaluation-20261031/chunks.jsonl')
    parser.add_argument('--original-provenance', type=Path, default=ROOT / 'data/sec/provenance.json')
    parser.add_argument('--ingest-report', type=Path, default=ROOT / 'indexes/sec-scale-v2-20261031/report.json')
    parser.add_argument('--allow-subset', action='store_true', help='Accept a run whose manifest records a deliberate partial case selection')
    parser.add_argument('--root', type=Path, default=ROOT, help='Repository root that recorded artifact paths are relative to')
    parser.add_argument('--strict-code', action='store_true', help='Fail if any recorded code file changed since the run (default: report drift only)')
    parser.add_argument('--enforce-gates', action='store_true', help='Also exit non-zero unless every checked release gate is met')
    parser.add_argument('--output', type=Path, help='Default: <directory>/independent-broad-verification.json (never overwritten)')
    args = parser.parse_args()
    pin = args.expected_suite_sha256 or args.suite.with_suffix('.sha256').read_text().split()[0]
    result = verify(args.directory, args.suite, pin, args.corpus, args.original_provenance, args.ingest_report, args.allow_subset, args.root, args.strict_code)
    output = args.output or args.directory / 'independent-broad-verification.json'
    with output.open('x') as stream:
        stream.write(json.dumps(result, indent=2) + '\n')
    print(json.dumps({k: v for k, v in result.items() if k not in ('cases', 'coverage', 'expected_case_ids')}, indent=2))
    raise SystemExit(0 if result['passed'] and (not args.enforce_gates or result['release_gates']['all_checked_gates_met']) else 1)
