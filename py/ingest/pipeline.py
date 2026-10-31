"""Automated manifest ingestion: SEC fetch → disk parser → immutable Go shards."""
import argparse
import json
import logging
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import tempfile
from urllib.parse import urlparse
from .edgar import EdgarClient
from .sections import Filing, SectionParser


def fsync_file(path: Path) -> None:
    with path.open('rb') as file:
        os.fsync(file.fileno())


def run(args: argparse.Namespace) -> dict:
    output=args.output.resolve()
    output.parent.mkdir(parents=True,exist_ok=True)
    if output.exists() or output.is_symlink():
        raise FileExistsError(output)
    lock=output.with_name(output.name+'.ingest-lock')
    descriptor=os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    os.close(descriptor)
    logger=logging.getLogger(__name__)
    process=None;database=None
    try:
        with tempfile.TemporaryDirectory(prefix='.ingestion-',dir=output.parent) as temporary:
            work=Path(temporary);build=work/'build'
            database=sqlite3.connect(work/'seen.sqlite')
            database.execute('PRAGMA cache_size=-1024')
            database.execute('CREATE TABLE filings(accession TEXT PRIMARY KEY)')
            client=None;filings=0;chunks=0
            with (work/'writer.log').open('wb') as errors, (work/'catalog.json').open('wb') as result, (work/'provenance.jsonl').open('w') as provenance:
                process=subprocess.Popen([str(args.go_binary.resolve()),'-output',str(build),'-docs-per-shard',str(args.docs_per_shard),
                    '-docs-per-segment',str(args.docs_per_segment),'-segment-mib',str(args.segment_mib)],
                    stdin=subprocess.PIPE,stdout=result,stderr=errors,start_new_session=True)
                try:
                    with args.manifest.open('rb') as manifest:
                        while line:=manifest.readline(65537):
                            if len(line)>65536:
                                raise ValueError('manifest record exceeds 64 KiB')
                            record=json.loads(line)
                            if set(record)-{'ticker','cik','year','accession','form','input','url','format'}:
                                raise ValueError('unknown manifest field')
                            if bool(record.get('input')) == bool(record.get('url')):
                                raise ValueError('provide exactly one input or url per filing')
                            filing=Filing(record['ticker'],record['cik'],record['year'],record['accession'],record.get('form','10-K'))
                            filing.validate()
                            try:
                                database.execute('INSERT INTO filings VALUES (?)',(filing.accession,));database.commit()
                            except sqlite3.IntegrityError as exc:
                                raise ValueError('duplicate accession in manifest: '+filing.accession) from exc
                            download=None
                            if record.get('url'):
                                expected_path=f'/Archives/edgar/data/{int(filing.cik)}/{filing.accession}/'
                                if not urlparse(record['url']).path.startswith(expected_path):
                                    raise ValueError('filing URL does not match CIK/accession')
                                if client is None:
                                    client=EdgarClient(args.user_agent,rate=args.rate)
                                raw=work/(filing.accession+'.raw')
                                download=client.fetch(record['url'],raw)
                                if not urlparse(download['url']).path.startswith(expected_path):
                                    raise ValueError('redirected filing URL does not match CIK/accession')
                            else:
                                raw=Path(record['input']).resolve()
                            if raw.stat().st_size > 128 << 20:
                                raise ValueError('filing exceeds 128 MiB')
                            parsed=work/'passages.jsonl'
                            report=SectionParser(args.words).parse(raw,parsed,filing,record.get('format','html'),work)
                            if download and report['raw_sha256']!=download['sha256']:
                                raise ValueError('download/parser source hash mismatch')
                            if process.stdin is None:
                                raise RuntimeError('missing Go ingestion pipe')
                            with parsed.open('rb') as source:
                                shutil.copyfileobj(source,process.stdin,length=32768)
                            process.stdin.flush()
                            provenance.write(json.dumps(dict(filing=filing.__dict__,download=download,parser=report),sort_keys=True)+'\n');provenance.flush()
                            parsed.unlink()
                            if download:raw.unlink()
                            filings+=1;chunks+=report['chunks']
                            logger.info('filing_complete accession=%s chunks=%d sections=%s',filing.accession,report['chunks'],list(report['sections']))
                    if filings==0:
                        raise ValueError('empty ingestion manifest')
                    process.stdin.close()
                    code=process.wait(timeout=300)
                    if code:
                        errors.flush()
                        with (work/'writer.log').open('rb') as failure:
                            failure.seek(max(0,(work/'writer.log').stat().st_size-4096))
                            detail=failure.read().decode(errors='replace')
                        raise RuntimeError(f'Go shard writer failed with exit {code}: {detail}')
                except BrokenPipeError as exc:
                    process.wait(timeout=10);errors.flush()
                    with (work/'writer.log').open('rb') as failure:
                        failure.seek(max(0,(work/'writer.log').stat().st_size-4096))
                        detail=failure.read().decode(errors='replace')
                    raise RuntimeError('Go writer rejected the chunk stream: '+detail) from exc
                finally:
                    if process.poll() is None:
                        os.killpg(process.pid,signal.SIGTERM)
                        try:process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            os.killpg(process.pid,signal.SIGKILL);process.wait()
                    if process.stdin is not None and not process.stdin.closed:
                        try:process.stdin.close()
                        except BrokenPipeError:pass
                    database.close();database=None
            with (build/'catalog.json').open() as source:
                catalog=json.load(source)
            if catalog['documents']!=chunks:
                raise ValueError('parser/shard document counts disagree')
            for source,name in [(work/'provenance.jsonl','provenance.jsonl'),(work/'writer.log','ingest.log')]:
                source.rename(build/name);fsync_file(build/name)
            summary=dict(format_version=1,filings=filings,chunks=chunks,sections=['Item 1A','Item 7','Item 8'],
                         rate_limit_per_second=args.rate,parser='deterministic-canonical-heading-v1',shards=len(catalog['shards']))
            (build/'ingestion.json').write_text(json.dumps(summary,indent=2)+'\n');fsync_file(build/'ingestion.json')
            directory=os.open(build,os.O_RDONLY)
            try:os.fsync(directory)
            finally:os.close(directory)
            if output.exists() or output.is_symlink():raise FileExistsError(output)
            build.rename(output)
            directory=os.open(output.parent,os.O_RDONLY)
            try:os.fsync(directory)
            finally:os.close(directory)
            return summary
    finally:
        if database is not None:
            database.close()
        lock.unlink(missing_ok=True)


def main() -> None:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest',type=Path,required=True,help='streamed filing metadata JSONL; local paths are relative to current directory')
    parser.add_argument('--output',type=Path,required=True,help='fresh published shard-root directory')
    parser.add_argument('--go-binary',type=Path,default=Path('models/bin/ingest'))
    parser.add_argument('--user-agent',default=os.environ.get('SEC_USER_AGENT',''))
    parser.add_argument('--rate',type=float,default=5)
    parser.add_argument('--words',type=int,default=256)
    parser.add_argument('--docs-per-shard',type=int,default=2048)
    parser.add_argument('--docs-per-segment',type=int,default=256)
    parser.add_argument('--segment-mib',type=int,default=8)
    args=parser.parse_args()
    if not 0<args.rate<=10 or not 32<=args.words<=1024:
        parser.error('rate must be >0 and <=10; words must be 32..1024')
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(name)s %(message)s')
    try:
        print(json.dumps(run(args),sort_keys=True),flush=True)
    except Exception:
        logging.exception('ingestion_failed')
        raise SystemExit(1)


if __name__=='__main__':main()
