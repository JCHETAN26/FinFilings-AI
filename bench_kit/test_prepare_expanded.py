"""Synthetic contract fixtures for preparation; never retrieval benchmark data."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class ExpandedPreparation(unittest.TestCase):
    def test_frozen_inputs_and_duplicate_failure_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);base=root/'base';sec=root/'sec';base.mkdir();(sec/'segments').mkdir(parents=True)
            original=(json.dumps({'ID':'original','Text':'fixture original'})+'\n').encode()
            (base/'chunks.jsonl').write_bytes(original)
            queries=b'{"id":"fixture","query":"original"}\n';qrels=b'query-id\tcorpus-id\tscore\nfixture\toriginal\t1\n'
            (base/'queries.jsonl').write_bytes(queries);(base/'qrels.tsv').write_bytes(qrels)
            provenance={key:hashlib.sha256((base/name).read_bytes()).hexdigest() for name,key in [('chunks.jsonl','corpus_sha256'),('queries.jsonl','query_sha256'),('qrels.tsv','qrels_sha256')]}
            (base/'provenance.json').write_text(json.dumps(provenance))
            (sec/'report.json').write_text(json.dumps({'chunks':1,'filings':[{'fixture':True}],'skipped':[]}))
            def write_sec(identifier):
                raw=(json.dumps({'ID':identifier,'Text':'fixture SEC section'})+'\n').encode()
                (sec/'passages.jsonl').write_bytes(raw)
                (sec/'segments/CORPUS_SHA256').write_text(hashlib.sha256(raw).hexdigest()+'\n')
            def run(out):
                return subprocess.run([sys.executable,'bench_kit/prepare_expanded.py','--base',str(base),'--sec',str(sec),'--out',str(out)],capture_output=True,text=True)
            write_sec('additional');out=root/'out';result=run(out)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual((out/'queries.jsonl').read_bytes(),queries);self.assertEqual((out/'qrels.tsv').read_bytes(),qrels)
            result=json.loads((out/'provenance.json').read_text())
            self.assertEqual(result['documents'],2)
            self.assertEqual(result['document_counts'],{'baseline':1,'new_sec':1})
            self.assertEqual(hashlib.sha256((out/'chunks.jsonl').read_bytes()).hexdigest(),result['corpus_sha256'])
            write_sec('original');failed=root/'failed';self.assertNotEqual(run(failed).returncode,0);self.assertFalse(failed.exists())
            self.assertEqual((base/'chunks.jsonl').read_bytes(),original)
