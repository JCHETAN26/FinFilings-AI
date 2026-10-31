"""Shared bounded model inference, immutable artifact checks, and disk passage store."""
import hashlib
import json
from pathlib import Path
import sqlite3
import numpy as np
import torch
from transformers import AutoModel, AutoTokenizer

BGE = 'BAAI/bge-small-en-v1.5'
CROSS = 'cross-encoder/ms-marco-MiniLM-L6-v2'


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda: f.read(1 << 20), b''):
            h.update(b)
    return h.hexdigest()


def lock_model(lock_path, name):
    lock = json.loads(Path(lock_path).read_text())[name]
    if not lock.get('revision') or not Path(lock['path']).is_dir():
        raise ValueError(f'missing pinned local snapshot: {name}')
    if not lock.get('weights_sha256') or sha256(Path(lock['path']) / 'model.safetensors') != lock['weights_sha256']:
        raise ValueError(f'model weights checksum mismatch: {name}')
    return lock


class Encoder:
    def __init__(self, model_lock, threads=4, max_length=512):
        if not 1 <= threads <= 32 or not 16 <= max_length <= 512:
            raise ValueError('invalid inference limits')
        torch.set_num_threads(threads)
        self.lock = lock_model(model_lock, BGE)
        self.tokenizer = AutoTokenizer.from_pretrained(self.lock['path'], local_files_only=True)
        self.model = AutoModel.from_pretrained(self.lock['path'], local_files_only=True, use_safetensors=True).eval()
        self.max_length = max_length
        self.dimension = self.model.config.hidden_size

    @torch.inference_mode()
    def encode(self, texts, query=False):
        if not 1 <= len(texts) <= 128:
            raise ValueError('embedding batch must contain 1..128 texts')
        if query:
            texts = ['Represent this sentence for searching relevant passages: ' + text for text in texts]
        inputs = self.tokenizer(texts, padding=True, truncation=True, max_length=self.max_length, return_tensors='pt')
        vectors = self.model(**inputs).last_hidden_state[:, 0]
        vectors = torch.nn.functional.normalize(vectors, p=2, dim=1)
        output = np.ascontiguousarray(vectors.cpu().numpy(), dtype=np.float32)
        if not np.isfinite(output).all():
            raise ValueError('non-finite embedding')
        return output


def open_store(path):
    connection = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
    connection.execute('PRAGMA query_only=ON')
    connection.execute('PRAGMA cache_size=-4096')
    return connection


def check_artifacts(directory):
    directory = Path(directory)
    manifest = json.loads((directory / 'manifest.json').read_text())
    for name, expected in manifest['files'].items():
        if Path(name).name != name or sha256(directory / name) != expected:
            raise ValueError(f'artifact checksum mismatch: {name}')
    return manifest
