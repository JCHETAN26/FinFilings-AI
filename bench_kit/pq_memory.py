#!/usr/bin/env python3
"""Isolated FAISS-only RAM probe: actual native buffers and process RSS are distinct."""
import argparse
import gc
import json
from pathlib import Path
import faiss
import numpy as np
import psutil


def probe(directory, mode):
    directory = Path(directory)
    manifest = json.loads((directory / 'manifest.json').read_text())
    d, count = manifest['dimension'], manifest['documents']
    faiss.omp_set_num_threads(1)
    process = psutil.Process()
    vectors = np.load(directory / 'vectors.f32.npy', mmap_mode='r')
    query = np.array(vectors[0:1], copy=True)
    vectors._mmap.close()
    del vectors
    warm = faiss.IndexFlatIP(d)
    warm.search(query, 1)
    del warm
    gc.collect()
    before = process.memory_info().rss
    if mode == 'pq':
        index = faiss.read_index(str(directory / 'index.pq8.faiss'))
        buffers = {'codes': index.codes.size(), 'centroids': index.pq.centroids.size() * 4}
        for name in ('transposed_centroids', 'centroids_sq_lengths', 'sdc_table'):
            if hasattr(index.pq, name):
                buffers[name] = getattr(index.pq, name).size() * 4
    elif mode == 'raw':
        index = faiss.IndexFlatIP(d)
        vectors = np.load(directory / 'vectors.f32.npy', mmap_mode='r')
        for start in range(0, count, 1024):
            index.add(np.ascontiguousarray(vectors[start:start + 1024]))
        vectors._mmap.close()
        del vectors
        buffers = {'float32_vectors': faiss.rev_swig_ptr(index.get_xb(), count * d).nbytes}
    else:
        raise ValueError('unknown mode')
    index.search(query, 10)  # Touch real index data; both modes include search initialization.
    gc.collect()
    after = process.memory_info().rss
    return dict(mode=mode, documents=index.ntotal, dimension=d, native_buffers_bytes=buffers,
                native_payload_bytes=sum(buffers.values()), rss_before_bytes=before, rss_after_bytes=after,
                observed_rss_delta_bytes=after-before,
                serialized_index_bytes=int(faiss.serialize_index(index).nbytes),
                caveat='Native buffer lengths are exact payload sizes; allocator capacity/headers are not introspectable. RSS delta is observed page-level process memory, not exact index payload. Model and SQLite memory excluded from both isolated probes.')


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--index', default='indexes/vector-phase2')
    ap.add_argument('--mode', choices=['pq','raw'], required=True)
    args = ap.parse_args()
    print(json.dumps(probe(args.index, args.mode)))
