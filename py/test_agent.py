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
from agent.models import Draft, QuoteClaim, Plan, Critique
from agent.provenance import ProvenanceRegistry
from agent.synthesizer import ground, escape_markdown
from agent.state_machine import Agent, merge_evidence
from agent.provider import ClaudeJSON
from common import open_store


class Grounding(unittest.TestCase):
    def setUp(self):
        self.registry=ProvenanceRegistry(Path('data/sec/chunks.jsonl'),Path('data/sec/provenance.json'))
        self.addCleanup(self.registry.close)
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

    def test_partial_context_preserves_useful_passages_with_fixed_bound(self):
        evidence=[]
        with Path('data/sec/chunks.jsonl').open() as source:
            for _ in range(7):
                chunk=json.loads(next(source))
                hit=dict(id=chunk['ID'],text=chunk['Text'],metadata={k:v for k,v in chunk.items() if k not in ('Text','Tokens')})
                evidence.append(self.registry.verify(hit,self.filters))
        merged=merge_evidence(evidence[:2],evidence[2:])
        self.assertEqual([item.id for item in merged],[item.id for item in evidence[:5]])
        self.assertEqual(merge_evidence(evidence[:2],evidence[:5]),evidence[:5])
        self.assertEqual(merge_evidence([],evidence[:5]),evidence[:5])
        for retained,fresh in [(evidence[:3],[]),([],evidence[:6]),([evidence[0],evidence[0]],[])]:
            with self.assertRaises(ValueError):merge_evidence(retained,fresh)
        with self.assertRaises(ValidationError):
            Critique(score=3,rationale='partial',rewritten_query='missing topic',retain_ids=[item.id for item in evidence[:3]])


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




class ExpandedProvenance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        provenance=Path('bench_kit/results/phase3-expanded-20261031/agent-provenance.json')
        if not provenance.exists():
            raise unittest.SkipTest('requires enrolled expanded SEC corpus')
        cls.registry=ProvenanceRegistry(Path('data/sec-scale-v2-evaluation-20261031/chunks.jsonl'),provenance)
        cls.addClassCleanup(cls.registry.close)

    def test_new_filing_section_identity_and_exact_content(self):
        self.assertEqual(len(self.registry.sources),152)
        items=[]
        with self.registry.corpus.open() as stream:
            for line in stream:
                chunk=json.loads(line)
                if chunk['Ticker']=='AAPL' and chunk['FilingYear']==2025 and chunk['ID'].endswith('passage-000000'):
                    items.append(chunk)
        self.assertEqual({c['SectionItem'] for c in items},{'Item 1A','Item 7','Item 8'})
        tags=[]
        for chunk in items:
            filters=FilingFilter(ticker='AAPL',year=2025,section=chunk['SectionItem'])
            hit=dict(id=chunk['ID'],text=chunk['Text'],metadata={k:v for k,v in chunk.items() if k not in ('Text','Tokens')})
            evidence=self.registry.verify(hit,filters)
            self.assertEqual(evidence.accession,'0000320193-25-000079')
            self.assertIn(chunk['ID'].split('#')[1],evidence.tag)
            tags.append(evidence.tag)
            with self.assertRaises(ValueError):self.registry.verify(dict(hit,text=hit['text']+' fabricated'),filters)
        self.assertEqual(len(set(tags)),3)
        catalog=next(s for s in self.registry.catalog() if s['accession']=='000032019325000079')
        self.assertEqual(catalog['sections'],['Item 1A','Item 7','Item 8'])

    def test_missing_exact_section_does_not_borrow_newer_year(self):
        filters=self.registry.resolve_filter(FilingFilter(ticker='MSFT',year=2024,section='Item 1A'))
        self.assertEqual(filters.accession,'000095017024087843')
        self.assertNotIn('Item 1A',self.registry.sections[filters.accession])
        self.assertEqual(self.registry.resolve_filter(FilingFilter(ticker='AAPL',year=2025)).accession,'000032019325000079')


if __name__=='__main__': unittest.main()
