#!/usr/bin/env python3
"""Append real SEC passages while freezing published Phase 2 evaluation inputs."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3


def sha256(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda:source.read(1<<20),b''):digest.update(block)
    return digest.hexdigest()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base',type=Path,default=Path('data/phase2'))
    parser.add_argument('--sec',type=Path,required=True,help='completed Go ingestion output')
    parser.add_argument('--out',type=Path,required=True,help='fresh evaluation data directory')
    args=parser.parse_args()
    if args.out.exists():raise FileExistsError(args.out)
    base=json.loads((args.base/'provenance.json').read_text())
    for filename,key in [('chunks.jsonl','corpus_sha256'),('queries.jsonl','query_sha256'),('qrels.tsv','qrels_sha256')]:
        if sha256(args.base/filename)!=base[key]:raise ValueError('base evaluation input changed: '+filename)
    sec_report=json.loads((args.sec/'report.json').read_text())
    sec_sha=sha256(args.sec/'passages.jsonl')
    if (args.sec/'segments/CORPUS_SHA256').read_text().strip()!=sec_sha:raise ValueError('SEC passage fingerprint mismatch')
    args.out.mkdir(parents=True)
    counts={};digest=hashlib.sha256()
    try:
        with sqlite3.connect(args.out/'preparation.sqlite') as db,(args.out/'chunks.jsonl').open('wb') as target:
            db.execute('PRAGMA cache_size=-4096')
            db.execute('CREATE TABLE ids(id TEXT PRIMARY KEY)')
            for name,path in [('baseline',args.base/'chunks.jsonl'),('new_sec',args.sec/'passages.jsonl')]:
                count=0
                with path.open('rb') as source:
                    while line:=source.readline((4<<20)+1):
                        if len(line)>4<<20:raise ValueError('oversized passage record')
                        chunk=json.loads(line)
                        if not chunk.get('ID') or not chunk.get('Text'):raise ValueError('empty ID/text')
                        db.execute('INSERT INTO ids VALUES (?)',(chunk['ID'],))
                        target.write(line);digest.update(line);count+=1
                counts[name]=count
            if counts['new_sec']!=sec_report['chunks']:raise ValueError('SEC document count mismatch')
        for filename in ('queries.jsonl','qrels.tsv'):shutil.copyfile(args.base/filename,args.out/filename)
        provenance=base | dict(dataset='Frozen FinQA/Phase 2 benchmark plus additional live SEC 10-K sections',
            documents=sum(counts.values()),corpus_sha256=digest.hexdigest(),base_corpus_sha256=base['corpus_sha256'],
            document_counts=counts,new_sec_passages_sha256=sec_sha,new_sec_report_sha256=sha256(args.sec/'report.json'),
            new_sec_filings=len(sec_report['filings']),new_sec_skipped=len(sec_report.get('skipped',[])),
            evaluation_notes=['The original 100 query IDs and published qrels are byte-identical; newly ingested SEC passages are additional retrieval candidates.',
                              'New SEC passages have no exhaustive human relevance labels. Report nDCG only against the frozen published FinQA judgments, not as a new SEC-only quality benchmark.'])
        (args.out/'provenance.json').write_text(json.dumps(provenance,indent=2)+'\n')
        print(json.dumps(provenance,indent=2))
    except Exception:
        shutil.rmtree(args.out)
        raise


if __name__=='__main__':main()
