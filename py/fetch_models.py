#!/usr/bin/env python3
"""Fetch pinned, safetensors-only real model snapshots (no remote custom code)."""
import argparse
import json
from pathlib import Path
from huggingface_hub import snapshot_download
from common import BGE, CROSS, sha256


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--out', type=Path, default=Path('data/phase2-models.json'))
    ap.add_argument('--cache-dir', default='/tmp/finfilings-hf-cache/hub')
    args = ap.parse_args()
    revisions = {BGE:'5c38ec7c405ec4b44b94cc5a9bb96e735b38267a', CROSS:'233902d25c440f23af6f7d6e94d2946bac0bee0a'}
    lock = {}
    for name, revision in revisions.items():
        path = snapshot_download(name, revision=revision, cache_dir=args.cache_dir,
                                 allow_patterns=['config.json','model.safetensors','tokenizer.json','tokenizer_config.json','special_tokens_map.json','vocab.txt'])
        weights = Path(path) / 'model.safetensors'
        if not weights.is_file():
            raise FileNotFoundError(weights)
        lock[name] = dict(revision=revision, path=path, weights_sha256=sha256(weights))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.out.with_suffix('.tmp')
    temporary.write_text(json.dumps(lock, indent=2) + '\n')
    temporary.replace(args.out)
    print(json.dumps(lock, indent=2))


if __name__ == '__main__':
    main()
