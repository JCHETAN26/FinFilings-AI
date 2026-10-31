"""Reuse checks against actual saved BGE embeddings, never synthetic vectors."""
import json
from pathlib import Path
import unittest
import numpy as np
from common import open_store
from vector_cache import VectorCache

class CachedVectors(unittest.TestCase):
    def test_id_text_and_model_must_all_match(self):
        directory=Path('indexes/vector-phase2')
        if not directory.exists():self.skipTest('requires actual Phase 2 BGE artifacts')
        manifest=json.loads((directory/'manifest.json').read_text())
        db=open_store(directory/'passages.sqlite')
        try:identifier,text=db.execute('SELECT id,text FROM passages WHERE row_index=0').fetchone()
        finally:db.close()
        with VectorCache([directory],manifest['model_revision'],manifest['dimension']) as cache:
            actual=cache.find(identifier,text)
            expected=np.load(directory/'vectors.f32.npy',mmap_mode='r')
            try:np.testing.assert_array_equal(actual,expected[0])
            finally:expected._mmap.close()
            self.assertIsNone(cache.find(identifier,text+' changed'))
            self.assertIsNone(cache.find('unknown-source-id',text))
            self.assertEqual(cache.provenance()[0]['corpus_sha256'],manifest['corpus_sha256'])
        self.assertEqual(cache.sources,[])
        with self.assertRaises(ValueError):VectorCache([directory],'incorrect-model-revision',manifest['dimension'])
        with self.assertRaises(ValueError):VectorCache([directory],manifest['model_revision'],manifest['dimension'],128)
        with self.assertRaises(ValueError):VectorCache([directory]*5,manifest['model_revision'],manifest['dimension'])
