#!/usr/bin/env python3
"""Download pinned published FinQA data and a current SEC CIK/ticker snapshot."""
import argparse
import hashlib
from pathlib import Path
import time
import urllib.request


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--user-agent',required=True,help='project identifier and contact email for SEC requests')
    ap.add_argument('--out',type=Path,default=Path('data/finqa'))
    args=ap.parse_args()
    args.out.mkdir(parents=True,exist_ok=True)
    revision='0f16e2867befa6840783e58be38c9efb9229d742'
    for name,expected in [('train.json','49f237eb9779b569473b26b08048867d04635a7cc39ad6a7a5664c55bb428db6'),('dev.json','a847fb7e0d61a3125a1e2909852df6b89f1ee64d2c5ff1bf689e332214deee51'),('company_tickers.json',None)]:
        target=args.out/name
        if not target.exists():
            url=(f'https://raw.githubusercontent.com/czyssrs/FinQA/{revision}/dataset/{name}' if name!='company_tickers.json' else 'https://www.sec.gov/files/company_tickers.json')
            req=urllib.request.Request(url,headers={'User-Agent':args.user_agent})
            partial=target.with_suffix('.download')
            try:
                with urllib.request.urlopen(req,timeout=60) as response,partial.open('wb') as output:
                    size=0
                    for block in iter(lambda:response.read(65536),b''):
                        size+=len(block)
                        if size>128<<20:
                            raise ValueError('source exceeds 128 MiB')
                        output.write(block)
                partial.replace(target)
            finally:
                partial.unlink(missing_ok=True)
            time.sleep(1)
        digest=hashlib.sha256()
        with target.open('rb') as source:
            for block in iter(lambda:source.read(65536),b''):
                digest.update(block)
        if expected and digest.hexdigest()!=expected:
            raise ValueError(f'pinned source checksum failed: {name}')
        print(name,digest.hexdigest())
    (args.out/'revision.txt').write_text(revision+'\n')


if __name__=='__main__':
    main()
