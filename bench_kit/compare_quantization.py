#!/usr/bin/env python3
"""Verify quantization comparisons from raw rankings, load CSVs and immutable artifacts."""
import argparse
import csv
import json
import math
from pathlib import Path
import statistics
import sys
from compare_runtime import verify, read


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--before', type=Path, required=True)
    ap.add_argument('--after', type=Path, required=True)
    ap.add_argument('--before-index', type=Path, required=True)
    ap.add_argument('--after-index', type=Path, required=True)
    ap.add_argument('--data', type=Path, required=True)
    args = ap.parse_args()
    sys.path.insert(0, str(Path('py').resolve()))
    from common import check_artifacts
    b, bl, br, bc = verify(args.before, args.data)
    a, al, ar, ac = verify(args.after, args.data)
    bm, am = check_artifacts(args.before_index), check_artifacts(args.after_index)
    if bm != b['vector_manifest'] or am != a['vector_manifest']:
        raise ValueError('report/artifact manifest mismatch')
    for key in ('documents', 'dimension', 'nbits', 'metric', 'normalized', 'model', 'model_revision', 'max_length',
                'query_instruction', 'corpus_sha256', 'seed', 'training_count', 'training_iterations'):
        if bm[key] != am[key]:
            raise ValueError('changed embedding/training contract: '+key)
    for name in ('vectors.f32.npy', 'passages.sqlite'):
        if bm['files'][name] != am['files'][name]:
            raise ValueError('raw embedding/store changed')
    if read(args.before/'parameters.json') != read(args.after/'parameters.json'):
        raise ValueError('concurrency or candidate budget changed')
    if any(br[q]['sparse'] != ar[q]['sparse'] for q in br):
        raise ValueError('sparse baseline changed')
    for directory, quality in ((args.before, b), (args.after, a)):
        rows = [json.loads(line) for line in (directory/'evaluation/per-query.jsonl').read_text().splitlines()]
        for row in rows:
            if 'exact_dense_ids' in row:
                recall_from_ids = len(set(row['exact_dense_ids']) & {h['id'] for h in row['rankings']['dense'][:10]})/10
                if not math.isclose(recall_from_ids, row['pq_recall_at_10'], abs_tol=1e-12):
                    raise ValueError('PQ/exact ID recall mismatch or inconsistent dense search prefix')
        recall = statistics.mean(r['pq_recall_at_10'] for r in rows)
        if not math.isclose(recall, quality['pq_recall_at_10_vs_exact_float32'], abs_tol=1e-12):
            raise ValueError('recall aggregation mismatch')
    gold = {}
    with (args.data/'qrels.tsv').open() as source:
        for row in csv.DictReader(source, delimiter='\t'):
            gold.setdefault(row['query-id'], {})[row['corpus-id']] = int(row['score'])
    exact_scores = []
    for line in (args.after/'evaluation/per-query.jsonl').read_text().splitlines():
        row = json.loads(line)
        relevance = gold[row['id']]
        ideal = sum(1/math.log2(i+2) for i in range(min(10, sum(relevance.values()))))
        dcg = sum(relevance.get(doc, 0)/math.log2(i+2) for i, doc in enumerate(row['exact_dense_ids']))
        exact_scores.append(dcg/ideal if ideal else 0)
    exact_ndcg = statistics.mean(exact_scores)
    if not math.isclose(exact_ndcg, a['exact_dense_ndcg_at_10'], abs_tol=1e-12):
        raise ValueError('exact dense nDCG mismatch')
    result = dict(before=str(args.before), after=str(args.after), before_checks=bc, after_checks=ac,
                  unchanged_raw_embeddings_and_store=True, unchanged_concurrency_and_budgets=True,
                  before_subvectors=bm['subvectors'], after_subvectors=am['subvectors'],
                  before_pq_recall=b['pq_recall_at_10_vs_exact_float32'], after_pq_recall=a['pq_recall_at_10_vs_exact_float32'],
                  ndcg_before=b['ndcg_at_10'], ndcg_after=a['ndcg_at_10'], exact_dense_ndcg=exact_ndcg,
                  memory_before=b['vector_memory'], memory_after=a['vector_memory'],
                  load_before=bl, load_after=al)
    (args.after/'independent-comparison.json').write_text(json.dumps(result, indent=2)+'\n')
    print(json.dumps({k:v for k,v in result.items() if not k.startswith(('memory_', 'load_'))}, indent=2))


if __name__ == '__main__':
    main()
