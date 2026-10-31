#!/usr/bin/env python3
"""Stream BGE embeddings to float32 mmap and a disk passage store; train real FAISS PQ8."""
import argparse
import json
from pathlib import Path
import sqlite3
import time
import faiss
import numpy as np
from common import BGE, Encoder, sha256
from vector_cache import VectorCache


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--corpus', type=Path, default=Path('data/phase2/chunks.jsonl'))
    ap.add_argument('--out', type=Path, default=Path('indexes/vector-phase2'))
    ap.add_argument('--model-lock', default='data/phase2-models.json')
    ap.add_argument('--batch-size', type=int, default=32)
    ap.add_argument('--threads', type=int, default=4)
    ap.add_argument('--reuse-index', type=Path, action='append', default=[], help='reuse checksum-verified vectors only when ID, text, and model configuration match (max 4)')
    ap.add_argument('--subvectors', type=int, default=48)
    ap.add_argument('--train-limit', type=int, default=20000)
    ap.add_argument('--train-iterations', type=int, default=25)
    args = ap.parse_args()
    if len(args.reuse_index) > 4:
        ap.error("at most four reuse indexes")
    if not 1 <= args.batch_size <= 128 or not 256 <= args.train_limit <= 100000 or not 1 <= args.train_iterations <= 100:
        ap.error('invalid bounded batch/training limits')
    if args.out.exists():
        raise FileExistsError(f'output must be fresh: {args.out}')
    args.out.mkdir(parents=True)
    started = time.perf_counter()
    source_hash = sha256(args.corpus)
    with args.corpus.open('rb') as f:
        count = sum(1 for line in f if line.strip())
    if count < 256:
        raise ValueError('PQ8 requires at least 256 real training passages; never duplicate inputs')
    encoder = Encoder(args.model_lock, args.threads)
    dimension = encoder.dimension
    if args.subvectors < 1 or dimension % args.subvectors:
        raise ValueError('subvector count must divide embedding dimension')
    vectors = np.lib.format.open_memmap(args.out / 'vectors.f32.npy', mode='w+', dtype='float32', shape=(count, dimension))
    db = sqlite3.connect(args.out / 'passages.sqlite')
    db.execute('PRAGMA cache_size=-4096')
    db.execute('CREATE TABLE passages(row_index INTEGER PRIMARY KEY, id TEXT UNIQUE NOT NULL, text TEXT NOT NULL, metadata TEXT NOT NULL)')
    cache = VectorCache(args.reuse_index, encoder.lock["revision"], dimension, encoder.max_length)
    cache_provenance = cache.provenance()
    batch, offset, reused = [], 0, 0
    def flush():
        nonlocal offset, reused
        pending = []
        for i, chunk in enumerate(batch):
            vector = cache.find(chunk['ID'], chunk['Text'])
            if vector is None:
                pending.append(i)
            else:
                vectors[offset+i] = vector
                reused += 1
        if pending:
            encoded = encoder.encode([batch[i]['Text'] for i in pending])
            for i, vector in zip(pending, encoded):
                vectors[offset+i] = vector
        for i, chunk in enumerate(batch):
            db.execute('INSERT INTO passages VALUES (?,?,?,?)', (offset+i, chunk['ID'], chunk['Text'], json.dumps({k:v for k,v in chunk.items() if k not in ('Text','Tokens')})))
        offset += len(batch)
        if offset % (args.batch_size * 25) == 0:
            db.commit()
            print(f'embedded {offset}/{count} (reused {reused})', flush=True)
        batch.clear()
    try:
        with args.corpus.open() as f:
            for number, line in enumerate(f, 1):
                if not line.strip():
                    continue
                if len(line) > 4 << 20:
                    raise ValueError(f'line {number} exceeds 4 MiB')
                c = json.loads(line)
                if not c.get('ID') or not c.get('Text') or len(c['Text']) > 1 << 20:
                    raise ValueError(f'invalid passage at line {number}')
                batch.append(c)
                if len(batch) == args.batch_size:
                    flush()
        if batch:
            flush()
        if offset != count or sha256(args.corpus) != source_hash:
            raise ValueError('corpus changed during ingestion')
        vectors.flush()
        db.commit()
    finally:
        cache.close()
        db.close()
    embedding_seconds = time.perf_counter() - started
    faiss.omp_set_num_threads(args.threads)
    index = faiss.IndexPQ(dimension, args.subvectors, 8, faiss.METRIC_INNER_PRODUCT)
    index.pq.cp.seed = 20261030
    index.pq.cp.niter = args.train_iterations
    train_count = min(count, args.train_limit)
    # Deterministic coverage of the corpus; bounded float32 training buffer.
    chosen = np.linspace(0, count - 1, train_count, dtype=np.int64)
    training = np.ascontiguousarray(vectors[chosen])
    train_started = time.perf_counter()
    index.train(training)
    del training
    for start in range(0, count, args.batch_size):
        index.add(np.ascontiguousarray(vectors[start:start + args.batch_size]))
    faiss.write_index(index, str(args.out / 'index.pq8.faiss'))
    centroids = faiss.vector_to_array(index.pq.centroids).reshape(args.subvectors, 256, dimension // args.subvectors)
    np.save(args.out / 'centroids.npy', centroids, allow_pickle=False)
    manifest = dict(format_version=1, documents=count, dimension=dimension, subvectors=args.subvectors, nbits=8,
                    metric='inner_product', normalized=True, model=BGE, model_revision=encoder.lock['revision'],
                    query_instruction='Represent this sentence for searching relevant passages: ', max_length=512,
                    corpus_sha256=source_hash, reused_embeddings=reused, newly_encoded_embeddings=count-reused, embedding_reuse_sources=cache_provenance, training_count=train_count, recommended_training_count=39*256,
                    adequate_training_count=train_count >= 39*256, seed=20261030,
                    training_iterations=args.train_iterations, embedding_seconds=embedding_seconds,
                    pq_train_add_seconds=time.perf_counter()-train_started, total_seconds=time.perf_counter()-started,
                    files={name:sha256(args.out / name) for name in ('vectors.f32.npy','passages.sqlite','index.pq8.faiss','centroids.npy')})
    (args.out / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(json.dumps(manifest, indent=2), flush=True)


if __name__ == '__main__':
    main()
