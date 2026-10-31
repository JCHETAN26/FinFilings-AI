"""Ingestion contracts. Generated text is confined to labeled unit fixtures."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parent))
from ingest.edgar import EdgarClient
from ingest.sections import Filing,SectionParser


def fixture_text() -> str:
    # Synthetic section-boundary fixture, never used for performance metrics.
    lines=['TABLE OF CONTENTS','Item 1A. Risk Factors','Item 7. Management’s Discussion and Analysis of Financial Condition and Results of Operations','Item 8. Financial Statements and Supplementary Data',
        'Item 1. Business','fixture business '*90,'Item 1A. Risk Factors','riskmarker risk operations '*160,
        'Item 1B. Unresolved Staff Comments','fixture end risk','Item 7.',
        'Management’s Discussion and Analysis of Financial Condition and Results of Operations','revenuemarker revenue operations '*160,
        'Item 7A. Quantitative and Qualitative Disclosures About Market Risk','excludedmarker currency '*80,
        'Item 8. Financial Statements and Supplementary Data','financialmarker statements assets '*160,
        'Item 9. Changes in and Disagreements with Accountants on Accounting and Financial Disclosure','excludedmarker end']
    return '\n'.join(lines)


class SectionContracts(unittest.TestCase):
    def test_plain_toc_boundaries_chunk_tags_and_determinism(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);raw=root/'filing.txt';raw.write_text(fixture_text())
            filing=Filing('AAPL','320193',2023,'000032019323000106')
            hashes=[]
            for round in range(2):
                output=root/f'chunks{round}';report=SectionParser(64).parse(raw,output,filing,'text')
                self.assertEqual(report['raw_sha256'],hashlib.sha256(raw.read_bytes()).hexdigest())
                hashes.append(hashlib.sha256(output.read_bytes()).hexdigest())
                records=[json.loads(line) for line in output.read_text().splitlines()]
                self.assertEqual(len({c['ID'] for c in records}),len(records))
                self.assertEqual(set(c['SectionItem'] for c in records),{'Item 1A','Item 7','Item 8'})
                for c in records:
                    self.assertLessEqual(len(c['Text'].split()),64)
                    self.assertNotIn('excludedmarker',c['Text'])
                    for tag,marker in [('Item 1A','riskmarker'),('Item 7','revenuemarker'),('Item 8','financialmarker')]:
                        if marker in c['Text']:self.assertEqual(c['SectionItem'],tag)
            self.assertEqual(*hashes)

    def test_html_links_hidden_text_and_crlf_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);raw=root/'filing.html';output=root/'out'
            body=''.join('<div>'+line+'</div>\r\n' for line in fixture_text().splitlines()[4:])
            raw.write_bytes(('<html><body><div style="display:none">hiddenmarker Item 1A Risk Factors</div><ix:hidden>hiddenmarker</ix:hidden><table><tr><td><a href="#risk">Item 1A. Risk Factors</a></td></tr></table>'+body+'</body></html>').encode())
            report=SectionParser().parse(raw,output,Filing('AAPL','320193',2023,'000032019323000106'))
            self.assertEqual(report['raw_sha256'],hashlib.sha256(raw.read_bytes()).hexdigest())
            self.assertNotIn('hiddenmarker',output.read_text())

    def test_missing_ambiguous_and_oversized_fail_without_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for i,text in enumerate(['No verified headings',fixture_text()+'\nItem 1A. Risk Factors\n'+'duplicate risk '*160,
                                      '<div>'+'x'*(2<<20)+'</div>']):
                raw=root/f'raw{i}';raw.write_text(text);out=root/f'out{i}'
                with self.assertRaises(ValueError):SectionParser().parse(raw,out,Filing('AAPL','320193',2023,'000032019323000106'),'html' if i==2 else 'text')
                self.assertFalse(out.exists())


class EdgarContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.starts=[];cls.headers=[];cls.retry=0;cls.request_lock=threading.Lock();cls.cooldown=threading.Event()
        class Handler(BaseHTTPRequestHandler):
            # Real loopback protocol fixture; never represents SEC data.
            def do_GET(self):
                with cls.request_lock:
                    cls.starts.append(time.monotonic());cls.headers.append(self.headers.get('User-Agent'))
                if self.path=='/redirect':
                    self.send_response(302);self.send_header('Location','https://example.invalid/');self.end_headers();return
                if self.path=='/cooldown':
                    cls.cooldown.set()
                    self.send_response(429);self.send_header('Retry-After','1');self.end_headers();return
                if self.path=='/retry':
                    cls.retry+=1
                    if cls.retry==1:
                        self.send_response(429);self.send_header('Retry-After','1');self.end_headers();return
                if self.path in ('/403','/404'):self.send_response(int(self.path[1:]));self.end_headers();return
                body=b'protocol-only test fixture'*10
                self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
            def log_message(self,format,*args):return
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        cls.thread=threading.Thread(target=cls.server.serve_forever);cls.thread.start()
        cls.base=f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.thread.join();cls.server.server_close()

    def client(self,**kwargs):
        return EdgarClient('FinFilingsTests test@example.com',rate=10,allowed_hosts=('127.0.0.1',),allow_http=True,**kwargs)

    def test_shared_rate_headers_and_atomic_files(self):
        client=self.client()
        with tempfile.TemporaryDirectory() as directory:
            start=len(self.starts)
            with ThreadPoolExecutor(max_workers=4) as pool:
                results=list(pool.map(lambda i:client.fetch(self.base+'/ok',Path(directory)/f'{i}.txt'),range(12)))
            starts=sorted(self.starts[start:])
            self.assertEqual(len(starts),12)
            self.assertGreaterEqual(starts[-1]-starts[0],1.09)
            for t in starts:self.assertLessEqual(sum(t-1<x<=t for x in starts),10)
            self.assertTrue(all(h=='FinFilingsTests test@example.com' for h in self.headers[start:]))
            self.assertTrue(all(r['bytes']>0 and len(r['sha256'])==64 for r in results))
            self.assertFalse(list(Path(directory).glob('*.download')))
            with self.assertRaises(FileExistsError):client.fetch(self.base+'/ok',Path(directory)/'0.txt')

    def test_retry_after_and_rejections(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);client=self.client();started=time.monotonic()
            result=client.fetch(self.base+'/retry',root/'retry')
            self.assertEqual(result['attempts'],2);self.assertGreaterEqual(time.monotonic()-started,1.)
            for endpoint,expected in [('/redirect',ValueError),('/404',RuntimeError),('/403',RuntimeError)]:
                with self.assertRaises(expected):client.fetch(self.base+endpoint,root/endpoint[1:])
            with self.assertRaises(ValueError):self.client(max_bytes=4).fetch(self.base+'/ok',root/'large')
            self.assertFalse((root/'large').exists());self.assertFalse(list(root.glob('*.download')))
        for options in [dict(user_agent='no contact'),dict(user_agent='Project test@example.com',rate=11)]:
            with self.assertRaises(ValueError):EdgarClient(**options)
        with self.assertRaises(ValueError):EdgarClient('Project test@example.com').fetch('http://www.sec.gov/data',Path('/tmp/never'))

    def test_retry_after_is_shared_before_next_dispatch(self):
        client=self.client(retries=0)
        with tempfile.TemporaryDirectory() as directory, ThreadPoolExecutor(max_workers=2) as pool:
            start=len(self.starts)
            first=pool.submit(client.fetch,self.base+'/cooldown',Path(directory)/'first')
            self.assertTrue(self.cooldown.wait(2))
            second=pool.submit(client.fetch,self.base+'/ok',Path(directory)/'second')
            with self.assertRaises(RuntimeError):first.result()
            self.assertGreater(second.result()['bytes'],0)
            self.assertGreaterEqual(self.starts[start+1]-self.starts[start],.99)



class PipelineContracts(unittest.TestCase):
    def test_atomic_success_duplicate_rejection_and_cleanup(self):
        from argparse import Namespace
        from ingest.pipeline import run
        binary=Path('models/bin/ingest').resolve()
        if not binary.exists():self.skipTest('build models/bin/ingest first')
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);raw=root/'raw.txt';raw.write_text(fixture_text())
            metadata=dict(ticker='AAPL',cik='320193',year=2023,accession='000032019323000106',input=str(raw),format='text')
            manifest=root/'manifest.jsonl';manifest.write_text(json.dumps(metadata)+'\n')
            args=Namespace(output=root/'published',manifest=manifest,go_binary=binary,user_agent='',rate=5,words=64,docs_per_shard=4,docs_per_segment=2,segment_mib=1)
            result=run(args)
            self.assertEqual(result['filings'],1);self.assertGreater(result['shards'],1)
            self.assertTrue((args.output/'catalog.json').exists());self.assertTrue((args.output/'provenance.jsonl').exists())
            with self.assertRaises(FileExistsError):run(args)
            args.output=root/'duplicate';manifest.write_text((json.dumps(metadata)+'\n')*2)
            with self.assertRaises(ValueError):run(args)
            self.assertFalse(args.output.exists());self.assertFalse(list(root.glob('*.ingest-lock')))
            self.assertFalse(list(root.glob('.ingestion-*')))

if __name__=='__main__':unittest.main()
