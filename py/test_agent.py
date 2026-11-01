"""Adversarial contracts plus real on-disk PQ filter correctness (no model mocks)."""
import asyncio
from contextlib import closing
import json
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parent))
from pydantic import ValidationError
from agent.filters import FilingFilter
from agent.models import Draft, QuoteClaim, Plan
from agent.provenance import ProvenanceRegistry
from agent.synthesizer import ground, escape_markdown
from agent.state_machine import Agent
from agent.provider import ClaudeJSON
from common import open_store


class Grounding(unittest.TestCase):
    def setUp(self):
        self.registry=ProvenanceRegistry(Path('data/sec/chunks.jsonl'),Path('data/sec/provenance.json'))
        with Path('data/sec/chunks.jsonl').open() as source:
            self.chunk=json.loads(next(source))
        self.hit=dict(id=self.chunk['ID'],text=self.chunk['Text'],metadata={k:v for k,v in self.chunk.items() if k not in ('Text','Tokens')})
        self.filters=FilingFilter(ticker='AAPL',year=2023)
        self.evidence=self.registry.verify(self.hit,self.filters)

    def test_exact_quote_and_unknown_id_and_fabricated_claim(self):
        quote=self.chunk['Text'][:100]
        draft=Draft(claims=[QuoteClaim(chunk_id=self.chunk['ID'],quote=quote),
            QuoteClaim(chunk_id='invented',quote=quote), QuoteClaim(chunk_id=self.chunk['ID'],quote='Fabricated revenue increased by 999 percent during the filing period.')])
        citations,flags=ground(draft,[self.evidence])
        self.assertEqual(len(citations),1); self.assertEqual(len(flags),2)
        self.assertEqual(citations[0].accession,'0000320193-23-000106')

    def test_tampered_content_and_metadata_rejected(self):
        for hit in (dict(self.hit,text=self.hit['text']+' invented sentence'),dict(self.hit,metadata={**self.hit['metadata'],'FilingYear':2025})):
            with self.assertRaises(ValueError): self.registry.verify(hit,self.filters)

    def test_wrong_scope_and_forged_accession_rejected(self):
        with self.assertRaises(ValueError): self.registry.verify(self.hit,FilingFilter(ticker='MSFT',year=2024))
        with self.assertRaises(ValueError): self.registry.resolve_filter(FilingFilter(ticker='AAPL',year=2023,accession='0'*18))
        missing=self.registry.resolve_filter(FilingFilter(ticker='AAPL',year=2025))
        self.assertEqual(missing.year,2025);self.assertEqual(missing.accession,'')

    def test_cover_page_fiscal_period_is_distinct_from_filing_year(self):
        amazon=next(source for source in self.registry.catalog() if source['ticker']=='AMZN')
        self.assertEqual(amazon['filing_year'],2024)
        self.assertEqual(amazon['fiscal_period_end'],'December 31 , 2023')

    def test_missing_accession_cannot_become_verified(self):
        hit=dict(self.hit,id='finqa:example',metadata={**self.hit['metadata'],'ID':'finqa:example'})
        self.assertIsNone(self.registry.verify(hit,self.filters))

    def test_checksum_failure_is_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            corpus=Path(directory)/'source';corpus.write_text('tampered')
            with self.assertRaises(ValueError): ProvenanceRegistry(corpus,Path('data/sec/provenance.json'))

    def test_bounds_and_markdown_injection(self):
        for args in (dict(max_retries=3),dict(concurrency=4),dict(threshold=6)):
            with self.assertRaises(ValueError): Agent(None,None,self.registry,**args)
        with self.assertRaises(ValidationError): Plan(subqueries=[])
        with self.assertRaises(ValidationError): FilingFilter(section='Item 1A\nmalicious')
        self.assertIn(r'\[click\]',escape_markdown('[click](https://evil.invalid)'))


class PQFilter(unittest.TestCase):
    def test_pq_block_filter_matches_full_pq_ranking(self):
        self.check_pq_filter(Path('indexes/vector-phase2'))

    def test_pq128_block_filter_matches_full_pq_ranking(self):
        self.check_pq_filter(Path('indexes/vector-sec-scale-v2-pq128-20261031'))

    def check_pq_filter(self, directory):
        # Actual saved PQ8 codes and trained centroids. No fake embeddings.
        import faiss
        import numpy as np
        from service import Retrieval
        if not directory.exists(): self.skipTest('requires acquired Phase 2 index')
        retrieval=object.__new__(Retrieval)
        retrieval.index=faiss.read_index(str(directory/'index.pq8.faiss'))
        retrieval.store=directory/'passages.sqlite'
        faiss.omp_set_num_threads(1)
        vector=np.load(directory/'vectors.f32.npy',mmap_mode='r')[0:1].copy()
        filters=FilingFilter(ticker='AAPL',year=2023,accession='000032019323000106')
        scores,ids=retrieval.filtered_search(vector,10,filters)
        baseline_scores,baseline_ids=retrieval.index.search(vector,retrieval.index.ntotal)
        selected=[]
        with closing(open_store(retrieval.store)) as db:
            for score,position in zip(baseline_scores[0],baseline_ids[0]):
                metadata=json.loads(db.execute('SELECT metadata FROM passages WHERE row_index=?',(int(position),)).fetchone()[0])
                if filters.matches(metadata): selected.append((float(score),int(position)))
                if len(selected)==10: break
        self.assertEqual(ids[0].tolist(),[p for _,p in selected])
        np.testing.assert_allclose(scores[0],[s for s,_ in selected],rtol=1e-6,atol=1e-6)
        empty_scores,empty_ids=retrieval.filtered_search(vector,10,FilingFilter(ticker='MISSING'))
        self.assertEqual(empty_ids.shape,(1,0))
        # The 2048 boundary must not affect correctness when every indexed row matches.
        all_scores,all_ids=retrieval.filtered_search(vector,10,FilingFilter())
        np.testing.assert_allclose(all_scores[0],baseline_scores[0][:10],rtol=1e-6,atol=1e-6)


class ProviderTransport(unittest.IsolatedAsyncioTestCase):
    async def test_missing_executable_is_explicit(self):
        model=ClaudeJSON(executable='/nonexistent/finfilings-model')
        with self.assertRaises(FileNotFoundError):
            await model.generate(Draft,'transport failure test',{})
        self.assertEqual(model.calls,[])

    async def test_process_timeout_cleans_up_child(self):
        # Transport-only fixture; it never generates model output.
        import os
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); fixture=root/'waiter'; pidfile=root/'pid'
            fixture.write_text('#!/usr/bin/python3\nimport os,time\nfrom pathlib import Path\nPath('+repr(str(pidfile))+').write_text(str(os.getpid()))\ntime.sleep(60)\n')
            fixture.chmod(0o700)
            model=ClaudeJSON(executable=str(fixture),timeout=1,concurrency=1)
            with self.assertRaises(asyncio.TimeoutError):
                await model.generate(Draft,'transport timeout test',{})
            with self.assertRaises(ProcessLookupError):
                os.kill(int(pidfile.read_text()),0)
            model.executable='/nonexistent/finfilings-model'
            with self.assertRaises(FileNotFoundError):
                await asyncio.wait_for(model.generate(Draft,'transport test',{}),2)


if __name__=='__main__': unittest.main()
