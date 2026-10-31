#!/usr/bin/env python3
"""Prepare real report facts and published support labels without gold-text injection."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import ijson


def row_text(header, row):
    # FinQA code/utils/general_utils.py, pinned revision in provenance.
    prefix = header[0] + ' ' if header[0] else ''
    return ' '.join((prefix + ''.join(f'the {row[0]} of {h} is {c} ; ' for h, c in zip(header[1:], row[1:]))).split())


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda: f.read(65536), b''):
            h.update(b)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--source', type=Path, default=Path('data/finqa/dev.json'))
    ap.add_argument('--train-source', type=Path, default=Path('data/finqa/train.json'))
    ap.add_argument('--train-contexts', type=int, default=500)
    ap.add_argument('--tickers', type=Path, default=Path('data/finqa/company_tickers.json'))
    ap.add_argument('--sec', type=Path, default=Path('data/sec/chunks.jsonl'))
    ap.add_argument('--out', type=Path, default=Path('data/phase2'))
    ap.add_argument('--query-limit', type=int, default=100)
    args = ap.parse_args()
    if not 1 <= args.query_limit <= 10000 or not 0 <= args.train_contexts <= 10000:
        ap.error('query limit must be 1..10000; training contexts 0..10000')
    args.out.mkdir(parents=True, exist_ok=True)
    db_path = args.out / 'preparation.sqlite'
    if db_path.exists():
        raise FileExistsError(db_path)
    db = sqlite3.connect(db_path)
    db.execute('CREATE TABLE docs(id TEXT PRIMARY KEY, chunk TEXT NOT NULL)')
    tickers = {v['ticker'].upper(): str(v['cik_str']) for v in json.loads(args.tickers.read_text()).values()}
    selected, excluded, eligible = 0, 0, 0
    def records():
        for path, limit, is_dev in ((args.train_source, args.train_contexts, False), (args.source, None, True)):
            with path.open('rb') as source:
                for i, record in enumerate(ijson.items(source, 'item')):
                    if limit is not None and i >= limit:
                        break
                    yield is_dev, record
    with (args.out / 'queries.jsonl').open('w') as queries, (args.out / 'qrels.tsv').open('w') as qrels:
        qrels.write('query-id\tcorpus-id\tscore\n')
        for is_dev, record in records():
            filename = record['filename']
            ticker, year, _ = filename.split('/', 2)
            if ticker.upper() not in tickers:
                excluded += 1
                continue
            facts = [(f'text_{i}', text) for i, text in enumerate(record['pre_text'] + record['post_text'])]
            facts += [(f'table_{i}', row_text(record['table'][0], row)) for i, row in enumerate(record['table'])]
            mapping = {}
            for key, text in facts:
                text = ' '.join(text.split())
                if not re.search(r'[A-Za-z0-9]', text):
                    continue
                doc_id = f'finqa:{filename}#{key}#{hashlib.sha256(text.encode()).hexdigest()[:12]}'
                chunk = dict(ID=doc_id, Ticker=ticker.upper(), CIK=tickers[ticker.upper()], FormType='10-K',
                             FilingYear=int(year), SectionItem=f'FinQA source {filename} {key}', Text=text)
                db.execute('INSERT OR IGNORE INTO docs VALUES (?,?)', (doc_id, json.dumps(chunk)))
                mapping[key] = doc_id
            if not is_dev:
                continue
            gold_keys = record['qa']['gold_inds'].keys()
            if not gold_keys or not all(k in mapping for k in gold_keys):
                excluded += 1
                continue
            eligible += 1
            if selected < args.query_limit:
                qid = record['id']
                queries.write(json.dumps(dict(id=qid, query=record['qa']['question'])) + '\n')
                for key in sorted(gold_keys):
                    qrels.write(f'{qid}\t{mapping[key]}\t1\n')
                selected += 1
    # The previously measured SEC corpus adds authentic, unjudged distractors.
    with args.sec.open() as source:
        for line in source:
            c = json.loads(line)
            db.execute('INSERT INTO docs VALUES (?,?)', (c['ID'], json.dumps(c)))
    db.commit()
    with (args.out / 'chunks.jsonl').open('w') as f:
        for (chunk,) in db.execute('SELECT chunk FROM docs ORDER BY id'):
            f.write(chunk + '\n')
    count = db.execute('SELECT count(*) FROM docs').fetchone()[0]
    db.close()
    provenance = dict(dataset='FinQA train/dev contexts, dev support-fact labels + SEC passages',
                      revision=Path('data/finqa/revision.txt').read_text().strip(), source_sha256=digest(args.source), training_source_sha256=digest(args.train_source), training_context_limit=args.train_contexts,
                      sec_sha256=digest(args.sec), ticker_mapping_sha256=digest(args.tickers),
                      corpus_sha256=digest(args.out / 'chunks.jsonl'), query_sha256=digest(args.out / 'queries.jsonl'),
                      qrels_sha256=digest(args.out / 'qrels.tsv'), documents=count, selected_queries=selected,
                      eligible_dev_questions=eligible, excluded_records=excluded,
                      selection='First eligible dev questions in original order; limit fixed before scoring; no model tuning.',
                      judgments='Binary published supporting-fact labels, not exhaustive relevance judgments; not answer accuracy.',
                      metadata='FinQA filename year is report year, not verified filing date; CIK joined to current SEC ticker map, historical identity not independently audited. Unmatched tickers excluded.',
                      corpus='All eligible dev contexts, all table rows rendered uniformly. No gold strings or QA answers injected. SQLite dedup by source location and text hash. No corpus-sized Python list.')
    (args.out / 'provenance.json').write_text(json.dumps(provenance, indent=2) + '\n')
    print(json.dumps(provenance, indent=2))


if __name__ == '__main__':
    main()
