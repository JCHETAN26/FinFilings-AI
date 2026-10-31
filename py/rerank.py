"""Bounded real MiniLM inference over disk-backed candidate passages."""
import math
import json
from contextlib import closing
import torch
from transformers import AutoModelForSequenceClassification, AutoTokenizer
from common import CROSS, lock_model, open_store


class Reranker:
    def __init__(self, model_lock, store_path, batch_size=8):
        if not 1 <= batch_size <= 32:
            raise ValueError('reranker batch size must be 1..32')
        self.lock = lock_model(model_lock, CROSS)
        self.tokenizer = AutoTokenizer.from_pretrained(self.lock['path'], local_files_only=True)
        self.model = AutoModelForSequenceClassification.from_pretrained(self.lock['path'], local_files_only=True, use_safetensors=True).eval()
        if self.model.config.num_labels != 1:
            raise ValueError('expected one real relevance logit per query/passage pair')
        self.store_path, self.batch_size = store_path, batch_size

    @torch.inference_mode()
    def rank(self, query, ids, k=10):
        if not 1 <= len(ids) <= 50 or len(set(ids)) != len(ids) or not 1 <= k <= 10:
            raise ValueError('rerank requires 1..50 unique IDs and top-k 1..10')
        scores = []
        # Retain only this bounded candidate set, never the document collection.
        with closing(open_store(self.store_path)) as db:
            texts = []
            for identifier in ids:
                row = db.execute('SELECT text FROM passages WHERE id=?', (identifier,)).fetchone()
                if row is None:
                    raise KeyError(f'unknown candidate ID: {identifier}')
                texts.append(row[0])
        # Tokenize exactly once, truncate identically, then bucket by length.
        # Attention masking and final score/ID sorting preserve rank semantics.
        encoded = self.tokenizer([query] * len(texts), texts, padding=False, truncation=True, max_length=512)
        order = sorted(range(len(ids)), key=lambda i: (len(encoded['input_ids'][i]), i))
        padded_tokens = 0
        for start in range(0, len(order), self.batch_size):
            positions = order[start:start + self.batch_size]
            features = [{key: values[i] for key, values in encoded.items()} for i in positions]
            inputs = self.tokenizer.pad(features, padding=True, return_tensors='pt')
            padded_tokens += inputs['input_ids'].numel()
            logits = self.model(**inputs).logits.reshape(-1).cpu().tolist()
            for i, score in zip(positions, logits):
                if not math.isfinite(score):
                    raise ValueError('non-finite relevance score')
                scores.append(dict(id=ids[i], score=score))
        self.last_work = dict(pairs=len(ids), unpadded_tokens=sum(len(x) for x in encoded['input_ids']),
                              padded_tokens=padded_tokens)
        ranked = sorted(scores, key=lambda hit: (-hit['score'], hit['id']))[:k]
        with closing(open_store(self.store_path)) as db:
            for hit in ranked:
                row = db.execute('SELECT text,metadata FROM passages WHERE id=?', (hit['id'],)).fetchone()
                if row is None:
                    raise KeyError(hit['id'])
                hit['text'], hit['metadata'] = row[0], json.loads(row[1])
        return ranked
