#!/usr/bin/env python3
"""Independently recheck saved audit traces against streamed original corpus bytes."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import statistics


def digest(path):
    h=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1<<20),b''):h.update(block)
    return h.hexdigest()


def verify(directory,corpus):
    manifest=json.loads((directory/'agent-provenance.json').read_text())
    assert digest(corpus)==manifest['corpus_sha256'],'corpus checksum mismatch'
    sources={s['accession']:s for s in manifest['sources']}
    paths=sorted((directory/'evaluation').glob('audit[0-9][0-9].json'))
    assert paths, 'no saved audit traces'
    results=[json.loads(path.read_text()) for path in paths]
    needed={e['id'] for r in results for h in r['hops'] for e in h['evidence']}
    assert len(needed)<=450,'trace evidence exceeds bounded ten-case protocol'
    originals={}
    with corpus.open('rb') as stream:
        while line:=stream.readline(4*1024*1024+1):
            assert len(line)<=4*1024*1024,'oversized record'
            chunk=json.loads(line)
            if chunk['ID'] in needed:
                assert chunk['ID'] not in originals,'duplicate source ID'
                originals[chunk['ID']]=chunk
    assert set(originals)==needed,'missing original evidence'
    records=[]
    for path,result in zip(paths,results):
        audit_id=path.stem
        assert result['states']==['Plan','Retrieve_Evaluate','Synthesize']
        queries=result['plan']['subqueries'];assert 2<=len(queries)<=3
        accepted={};hop_rows=[]
        for pos,query in enumerate(queries):
            hops=[h for h in result['hops'] if h['subquery']==pos]
            assert 1<=len(hops)<=3
            assert [h['attempt'] for h in hops]==list(range(len(hops)))
            previous={}
            for h in hops:
                carried=h.get('retained_evidence_ids',[])
                assert len(carried)<=2 and len(set(carried))==len(carried)
                current={e['id']:e for e in h['evidence']}
                assert len(current)==len(h['evidence'])<=5
                assert all(identifier in previous and current.get(identifier)==previous[identifier] for identifier in carried), 'retention changed evidence'
                previous=current
                assert h['filters']==query['filters'],'rewrite changed scope'
                assert 1<=h['score']<=5
                assert not h['accepted'] or (h['score']>=4 and h['evidence'])
                for e in h['evidence']:
                    original=originals[e['id']];metadata={k:v for k,v in original.items() if k not in ('Text','Tokens')}
                    assert e['text']==original['Text'] and e['metadata']==metadata
                    m=re.fullmatch(r'(\d{18})#(?:(item(?:1A|7|8))#)?passage-(\d{6})',e['id']);assert m
                    accession,section,number=m.groups();source=sources[accession]
                    assert e['accession']==f'{accession[:10]}-{accession[10:12]}-{accession[12:]}'
                    assert e['url']==source['url']
                    suffix=f'{section}-p{number}' if section else f'p{number}'
                    assert e['tag']==f"{source['ticker']}-{source['filing_year']}-{source['form'].replace('-','')}-{suffix}"
                    for key,value in [('Ticker',source['ticker']),('CIK',source['cik']),('FormType',source['form']),('FilingYear',source['filing_year'])]:assert metadata[key]==value
                    for key,field in [('Ticker','ticker'),('FilingYear','year'),('SectionItem','section')]:
                        if query['filters'][field]:assert metadata[key]==query['filters'][field]
                    if query['filters']['accession']:assert accession==query['filters']['accession']
                    if h['accepted']:accepted[e['id']]=e
                hop_rows.append(dict(subquery=pos,attempt=h['attempt'],query=h['query'],score=h['score'],accepted=h['accepted'],evidence_count=len(h['evidence']),retained_evidence_ids=h.get('retained_evidence_ids',[]),rationale=h['rationale']))
        for c in result['citations']:
            e=accepted[c['chunk_id']]
            assert c['quote'] in ' '.join(originals[c['chunk_id']]['Text'].split())
            assert all(c[k]==e[k] for k in ['tag','accession','url'])
            assert f"[{c['tag']}]({c['url']})" in result['markdown']
            assert f"`{c['chunk_id']}`" in result['markdown'] and f"`{c['accession']}`" in result['markdown']
        assert (directory/'evaluation'/f'{audit_id}.md').read_text().rstrip()==result['markdown'].rstrip()
        cited={c['chunk_id'] for c in result['citations']}
        coverage=[any(e['id'] in cited for h in result['hops'] if h['subquery']==pos and h['accepted'] for e in h['evidence']) for pos in range(len(queries))]
        records.append(dict(id=audit_id,prompt=result['prompt'],citation_checks='pass',citations=len(result['citations']),subqueries=len(queries),cited_subqueries=sum(coverage),retrieval_calls=len(result['hops']),retries=sum(h['attempt']>0 for h in result['hops']),coverage='full' if all(coverage) else 'partial' if any(coverage) else 'abstain',flags=result['flags'],seconds=result['elapsed_seconds'],hops=hop_rows,models=sorted({m for call in result['model_calls'] for m in call['models']})))
    report=dict(corpus_sha256=manifest['corpus_sha256'],registered_filings=len(sources),cases=records,verified_citations=sum(r['citations'] for r in records),unique_source_passages=len(originals),retries=sum(r['retries'] for r in records),retrieval_calls=sum(r['retrieval_calls'] for r in records),median_seconds=statistics.median(r['seconds'] for r in records),scope='Exact bytes, immutable filters, bounded retries, inline source identity and quote verification; coverage is structural subquery coverage, not human semantic answer accuracy.')
    (directory/'independent-verification.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({k:v for k,v in report.items() if k!='cases'},indent=2))
    return report


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--directory',type=Path,required=True);p.add_argument('--corpus',type=Path,default=Path('data/sec-scale-v2-evaluation-20261031/chunks.jsonl'));a=p.parse_args();verify(a.directory,a.corpus)
