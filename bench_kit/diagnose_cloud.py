#!/usr/bin/env python3
"""Replay failed topical queries and inspect stage-by-stage AMZN candidate ranks."""
import argparse
import json
from pathlib import Path
import urllib.request


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--backend',required=True);p.add_argument('--output',type=Path,required=True);args=p.parse_args()
    audit=json.loads(Path('bench_kit/results/phase3-expanded-20261031/evaluation/audit02.json').read_text())
    queries=[h['query'] for h in audit['hops'] if h['subquery']==1]+['cloud services competition and technology infrastructure operating costs','cloud infrastructure services competition pricing competitive factors','technology infrastructure expenses data centers servers operating costs']
    filters=dict(ticker='AMZN',year=2024,section='',accession='000101872424000008')
    results=[]
    for query in queries:
        request=urllib.request.Request(args.backend+'/search',data=json.dumps(dict(query=query,rerank=True,filters=filters)).encode(),headers={'Content-Type':'application/json'})
        with urllib.request.urlopen(request,timeout=125) as response:result=json.load(response)
        results.append(dict(query=query,filters=filters,result=result))
        print(query,[(h['id'].split('-')[-1],round(h['score'],3)) for h in result['reranked']],flush=True)
    args.output.write_text(json.dumps(results,indent=2)+'\n')


if __name__=='__main__':main()
