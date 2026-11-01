#!/usr/bin/env python3
"""Label-free PQ calibration on real non-training document vectors; self hits excluded."""
import argparse
import json
from pathlib import Path
import faiss
import numpy as np


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--indexes', type=Path, nargs='+', required=True)
    ap.add_argument('--out', type=Path, required=True)
    args = ap.parse_args()
    import sys
    sys.path.insert(0, str(Path('py').resolve()))
    from common import check_artifacts, sha256
    faiss.omp_set_num_threads(2)
    manifests = [check_artifacts(p) for p in args.indexes]
    base = manifests[0]
    for m in manifests:
        for key in ('documents', 'dimension', 'corpus_sha256', 'model_revision', 'seed', 'training_count', 'training_iterations'):
            if m[key] != base[key]:
                raise ValueError('calibration source/config mismatch')
        if m['files']['vectors.f32.npy'] != base['files']['vectors.f32.npy']:
            raise ValueError('different source embeddings')
    count = base['documents']
    if not 256 <= base['training_count'] <= min(count, 100000):
        raise ValueError('invalid bounded training count')
    training = np.linspace(0, count-1, base['training_count'], dtype=np.int64)
    eligible_count = count-len(training)
    if eligible_count < 128:
        raise ValueError('need 128 non-training calibration documents')
    # Walk row numbers instead of allocating an array proportional to the corpus.
    targets = np.linspace(0, eligible_count-1, 128, dtype=np.int64)
    chosen, training_pos, eligible_pos = [], 0, 0
    for row in range(count):
        if training_pos < len(training) and row == training[training_pos]:
            training_pos += 1
            continue
        if eligible_pos == targets[len(chosen)]:
            chosen.append(row)
            if len(chosen) == len(targets):
                break
        eligible_pos += 1
    rows = np.asarray(chosen, dtype=np.int64)
    vectors = np.load(args.indexes[0] / 'vectors.f32.npy', mmap_mode='r')
    queries = np.array(vectors[rows], copy=True)
    exact = []
    for row, query in zip(rows, queries):
        retained = []
        for start in range(0, count, 2048):
            scores = vectors[start:start+2048] @ query
            if start <= row < start+len(scores):
                scores[row-start] = -np.inf
            positions = np.argpartition(scores, -10)[-10:]
            retained = sorted(retained + [(float(scores[p]), start+int(p)) for p in positions], reverse=True)[:10]
        exact.append([r for _, r in retained])
    vectors._mmap.close()
    results = []
    for directory, manifest in zip(args.indexes, manifests):
        index = faiss.read_index(str(directory / 'index.pq8.faiss'))
        _, found = index.search(queries, 11)
        recalls = []
        for row, gold, hits in zip(rows, exact, found):
            selected = [int(hit) for hit in hits if hit != row][:10]
            recalls.append(len(set(gold) & set(selected))/10)
        raw = count*base['dimension']*4
        payload = index.codes.size()+index.pq.centroids.size()*4
        results.append(dict(index=str(directory), subvectors=manifest['subvectors'], mean_recall_at_10=float(np.mean(recalls)),
                            per_query_recall=recalls, native_payload_bytes=payload, reduction_percent=100*(1-payload/raw),
                            manifest_sha256=sha256(directory / 'manifest.json')))
    allowed = [r for r in results if r['reduction_percent'] >= 90]
    selected = max(allowed, key=lambda r: (r['mean_recall_at_10'], -r['native_payload_bytes']))
    report = dict(method='128 deterministic non-training document queries, self excluded; no text judgments or FinQA query/qrel use',
                  caveat='Document queries diagnose code distortion; final real search-query recall and relevance are separately evaluated. Identical-text ties can lower ID recall.',
                  query_rows=rows.tolist(), exact_top10_rows=exact, training_rows_excluded=True,
                  selection_rule='highest label-free recall subject to >=90% native vector payload reduction', results=results, selected=selected['index'])
    args.out.write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps({k:v for k,v in report.items() if k not in ('query_rows','exact_top10_rows')}, indent=2))


if __name__ == '__main__':
    main()
