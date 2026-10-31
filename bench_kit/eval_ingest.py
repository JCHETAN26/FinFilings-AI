#!/usr/bin/env python3
"""Real multi-MB SEC parse/index/mmap replay with finite resource-retention checks."""
import argparse
import gc
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import tracemalloc
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'py'))
from ingest.sections import Filing,SectionParser


def digest(path: Path) -> str:
    checksum=hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda:source.read(65536),b''):checksum.update(block)
    return checksum.hexdigest()


def fds() -> int:
    return len(list(Path('/proc/self/fd').iterdir()))


def rss() -> int:
    return int(Path('/proc/self/statm').read_text().split()[1])*os.sysconf('SC_PAGE_SIZE')


def main() -> None:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,default=Path('data/sec/MSFT-000095017024087843.html'))
    parser.add_argument('--output',type=Path,default=Path('bench_kit/results/ingest/real'))
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=False)
    expected='ee55571f4ec7f56874706add491553a10c701ad6e355532dcc736debd5ccfcdf'
    if args.source.stat().st_size<2<<20 or digest(args.source)!=expected:
        raise ValueError('expected acquired 6.86 MB Microsoft 2024 10-K; raw source identity mismatch')
    filing=Filing('MSFT','789019',2024,'000095017024087843')
    base_fds=fds();observations=[];first_retained=None;chunks_sha=None
    tracemalloc.start(1)
    with tempfile.TemporaryDirectory(prefix='finfilings-real-ingest-') as temporary:
        root=Path(temporary);chunks=root/'chunks.jsonl'
        for round in range(6):
            chunks.unlink(missing_ok=True);tracemalloc.reset_peak();start=time.perf_counter()
            report=SectionParser().parse(args.source,chunks,filing,spool_parent=root)
            if report['raw_sha256']!=expected:raise ValueError('raw checksum mismatch after parsing')
            checksum=digest(chunks)
            if chunks_sha is None:chunks_sha=checksum
            elif checksum!=chunks_sha:raise ValueError('nondeterministic passage bytes')
            gc.collect();current,peak=tracemalloc.get_traced_memory()
            if first_retained is None:first_retained=current
            if current>first_retained+(1<<20):raise ValueError('retained Python allocations grew beyond 1 MiB tolerance')
            if fds()!=base_fds:raise ValueError('parser leaked a file descriptor')
            observation=dict(round=round+1,seconds=time.perf_counter()-start,retained_traced_bytes=current,
                peak_traced_bytes=peak,rss_bytes=rss(),file_descriptors=fds(),chunks=report['chunks'])
            observations.append(observation);print(json.dumps(observation),flush=True)
        tracemalloc.stop()
        env=dict(os.environ,FINFILINGS_REAL_CHUNKS=str(chunks),FINFILINGS_MEMORY_REPORT=str((args.output/'go-memory.json').resolve()),
            GOCACHE='/tmp/finfilings-go-cache',CCACHE_DIR='/tmp/finfilings-ccache',CCACHE_TEMPDIR='/tmp/finfilings-ccache-tmp')
        command=['go','test','./pkg/ingest','-run','^TestRealFilingRepeatedIngestion$','-count=1','-v']
        result=subprocess.run(command,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
        (args.output/'go-real.log').write_text(result.stdout+'\nexit_code='+str(result.returncode)+'\n')
        if result.returncode:raise RuntimeError(result.stdout)
    final=dict(source=str(args.source),raw_bytes=args.source.stat().st_size,raw_sha256=expected,chunk_sha256=chunks_sha,
        parser=report,python_rounds=observations,go_memory_report='go-memory.json',
        scope='Six Python parse/cleanup rounds and six Go rebuild/mmap/search/close/GC rounds. Finite checks detect observed descriptor/mapping/goroutine leaks and bounded retained-heap growth; they do not prove indefinite leak freedom.')
    (args.output/'report.json').write_text(json.dumps(final,indent=2)+'\n')
    print(json.dumps(final,indent=2),flush=True)


if __name__=='__main__':main()
