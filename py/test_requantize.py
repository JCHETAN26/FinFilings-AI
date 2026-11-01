"""Real FAISS round-trip regression tests; generated vectors are unit fixtures, never metrics."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

import faiss
import numpy as np
from common import sha256
from requantize import rebuild
from service import Retrieval


class Requantization(unittest.TestCase):
    def test_rebuild_preserves_source_and_rejects_manifest_mismatch(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, target = root/'source', root/'target'
            source.mkdir()
            rng = np.random.default_rng(42)
            vectors = rng.normal(size=(10240, 16)).astype('float32')
            vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
            np.save(source/'vectors.f32.npy', vectors, allow_pickle=False)
            with sqlite3.connect(source/'passages.sqlite') as db:
                db.execute('CREATE TABLE passages(row_index INTEGER PRIMARY KEY, id TEXT, text TEXT, metadata TEXT)')
                db.executemany('INSERT INTO passages VALUES (?, ?, ?, ?)', ((i, str(i), 'unit fixture', '{}') for i in range(len(vectors))))
            faiss.omp_set_num_threads(1)
            index = faiss.IndexPQ(16, 2, 8, faiss.METRIC_INNER_PRODUCT)
            index.pq.cp.niter = 1
            index.train(vectors)
            index.add(vectors)
            faiss.write_index(index, str(source/'index.pq8.faiss'))
            np.save(source/'centroids.npy', faiss.vector_to_array(index.pq.centroids), allow_pickle=False)
            manifest = dict(documents=len(vectors), dimension=16, subvectors=2, nbits=8, metric='inner_product', normalized=True,
                            seed=42, training_iterations=1, training_count=len(vectors),
                            files={name:sha256(source/name) for name in ('vectors.f32.npy','passages.sqlite','index.pq8.faiss','centroids.npy')})
            (source/'manifest.json').write_text(json.dumps(manifest))
            result = rebuild(source, target, 4, 1)
            rebuilt = faiss.read_index(str(target/'index.pq8.faiss'))
            self.assertEqual((rebuilt.d, rebuilt.pq.M, rebuilt.pq.nbits, rebuilt.ntotal), (16, 4, 8, len(vectors)))
            for name in ('vectors.f32.npy', 'passages.sqlite'):
                self.assertEqual(result['files'][name], manifest['files'][name])
            self.assertEqual(sha256(source/'index.pq8.faiss'), manifest['files']['index.pq8.faiss'])
            self.assertTrue(np.isfinite(rebuilt.search(vectors[:2], 10)[0]).all())
            with self.assertRaises(FileExistsError):
                rebuild(source, target, 4, 1)
            with self.assertRaises(ValueError):
                rebuild(source, root/'invalid', 3, 1)
            self.assertFalse((root/'invalid').exists())
            result['subvectors'] = 2
            (target/'manifest.json').write_text(json.dumps(result))
            # Must reject before model loading, even though file checksums are intact.
            with self.assertRaisesRegex(ValueError, 'matching trained PQ8'):
                Retrieval(target, 'missing-model-lock', 1)


if __name__ == '__main__':
    unittest.main()
