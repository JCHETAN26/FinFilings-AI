"""Read-only reuse of verified BGE vectors; an ID alone never authorizes reuse."""
from pathlib import Path
import numpy as np
from common import BGE, check_artifacts, open_store


class VectorCache:
    def __init__(self, directories, revision, dimension, max_length=512):
        if len(directories) > 4:
            raise ValueError('at most four embedding reuse indexes')
        self.sources = []
        try:
            for directory in directories:
                directory = Path(directory)
                manifest = check_artifacts(directory)
                if (manifest['model'] != BGE or manifest['model_revision'] != revision or
                    manifest['dimension'] != dimension or manifest['max_length'] != max_length or
                    manifest.get('normalized') is not True):
                    raise ValueError('incompatible cached embedding model/configuration')
                vectors = np.load(directory/'vectors.f32.npy', mmap_mode='r', allow_pickle=False)
                if vectors.shape != (manifest['documents'], dimension) or vectors.dtype != np.float32:
                    vectors._mmap.close()
                    raise ValueError('invalid cached embedding array')
                try:
                    store = open_store(directory/'passages.sqlite')
                except Exception:
                    vectors._mmap.close()
                    raise
                self.sources.append((directory, manifest, vectors, store))
        except Exception:
            self.close()
            raise

    def find(self, identifier, text):
        for _, _, vectors, store in self.sources:
            row = store.execute('SELECT row_index,text FROM passages WHERE id=?', (identifier,)).fetchone()
            if row is not None and row[1] == text:
                if not 0 <= row[0] < len(vectors):
                    raise ValueError('cached vector row outside array')
                vector = vectors[row[0]]
                if not np.isfinite(vector).all() or not np.isclose(np.linalg.norm(vector), 1, atol=1e-4):
                    raise ValueError('invalid cached embedding vector')
                return vector
        return None

    def provenance(self):
        return [dict(directory=str(path), corpus_sha256=manifest['corpus_sha256'],
                     vector_sha256=manifest['files']['vectors.f32.npy']) for path, manifest, _, _ in self.sources]

    def close(self):
        for _, _, vectors, store in self.sources:
            store.close()
            vectors._mmap.close()
        self.sources.clear()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
