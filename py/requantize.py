#!/usr/bin/env python3
"""Rebuild immutable PQ8 artifacts from verified disk-backed embeddings, without inference."""
import argparse
import json
from pathlib import Path
import shutil
import time

import faiss
import numpy as np
from common import check_artifacts, sha256


def rebuild(source: Path, output: Path, subvectors: int, threads: int = 2) -> dict:
    manifest = check_artifacts(source)
    d, count = manifest['dimension'], manifest['documents']
    if subvectors < 1 or d % subvectors or not 1 <= threads <= 8:
        raise ValueError('subvectors must divide dimension; threads must be 1..8')
    if count < 256 or manifest['nbits'] != 8 or manifest['metric'] != 'inner_product' or not manifest['normalized']:
        raise ValueError('expected at least 256 normalized inner-product PQ8 vectors')
    output.mkdir(parents=True, exist_ok=False)
    started = time.perf_counter()
    try:
        vectors = np.load(source / 'vectors.f32.npy', mmap_mode='r', allow_pickle=False)
        try:
            if vectors.shape != (count, d) or vectors.dtype != np.float32:
                raise ValueError('raw vector shape/dtype mismatch')
            faiss.omp_set_num_threads(threads)
            index = faiss.IndexPQ(d, subvectors, 8, faiss.METRIC_INNER_PRODUCT)
            index.pq.cp.seed = manifest['seed']
            index.pq.cp.niter = manifest['training_iterations']
            # Preserve the original training sample to isolate the effect of code width.
            train_count = manifest['training_count']
            if not 256 <= train_count <= min(count, 100000):
                raise ValueError('invalid bounded training count')
            chosen = np.linspace(0, count - 1, train_count, dtype=np.int64)
            training = np.ascontiguousarray(vectors[chosen])
            if not np.isfinite(training).all():
                raise ValueError('nonfinite training vectors')
            index.train(training)
            del training
            for start in range(0, count, 1024):
                batch = np.ascontiguousarray(vectors[start:start + 1024])
                if not np.isfinite(batch).all() or not np.allclose(np.linalg.norm(batch, axis=1), 1, atol=1e-4):
                    raise ValueError('invalid normalized source vectors')
                index.add(batch)
            faiss.write_index(index, str(output / 'index.pq8.faiss'))
            np.save(output / 'centroids.npy', faiss.vector_to_array(index.pq.centroids).reshape(subvectors, 256, d // subvectors), allow_pickle=False)
        finally:
            vectors._mmap.close()
        for name in ('vectors.f32.npy', 'passages.sqlite'):
            shutil.copyfile(source / name, output / name)
        updated = dict(manifest)
        updated.update(subvectors=subvectors, pq_train_add_seconds=time.perf_counter()-started,
                       requantization=dict(source_manifest_sha256=sha256(source / 'manifest.json'), threads=threads,
                                           method='same deterministic training sample; code width only',
                                           seconds=time.perf_counter()-started))
        updated['files'] = {name: sha256(output / name) for name in manifest['files']}
        for name in ('vectors.f32.npy', 'passages.sqlite'):
            if updated['files'][name] != manifest['files'][name]:
                raise ValueError('source changed during copy')
        (output / 'manifest.json').write_text(json.dumps(updated, indent=2)+'\n')
        check_artifacts(output)
        return updated
    except BaseException:
        shutil.rmtree(output)
        raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--subvectors', type=int, required=True)
    parser.add_argument('--threads', type=int, default=2)
    args = parser.parse_args()
    print(json.dumps(rebuild(args.source, args.out, args.subvectors, args.threads), indent=2), flush=True)
