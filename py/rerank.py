"""Bounded real MiniLM inference over disk-backed candidate passages."""
import math
import json
import time
from contextlib import closing
from pathlib import Path
import torch
from transformers import AutoModelForSequenceClassification, AutoTokenizer
from common import CROSS, lock_model, open_store, check_artifacts


class Reranker:
    def __init__(self, model_lock, store_path, batch_size=None, onnx_directory=None, threads=2):
        if batch_size is None:
            batch_size = 1 if onnx_directory is not None else 8
        if not 1 <= batch_size <= 32:
            raise ValueError('reranker batch size must be 1..32')
        self.lock = lock_model(model_lock, CROSS)
        self.tokenizer = AutoTokenizer.from_pretrained(self.lock['path'], local_files_only=True)
        self.backend, self.device = 'torch', 'cpu'
        self.artifact_sha256 = self.lock['weights_sha256']
        if onnx_directory is None:
            self.model = AutoModelForSequenceClassification.from_pretrained(self.lock['path'], local_files_only=True, use_safetensors=True).eval()
            if self.model.config.num_labels != 1:
                raise ValueError('expected one real relevance logit per query/passage pair')
        else:
            if not 1 <= threads <= 32:
                raise ValueError('ONNX threads must be 1..32')
            manifest = check_artifacts(onnx_directory)
            if (manifest.get('model') != CROSS or manifest.get('model_revision') != self.lock['revision']
                    or manifest.get('weights_sha256') != self.lock['weights_sha256']
                    or manifest.get('dtype') != 'float32' or manifest.get('max_length') != 512):
                raise ValueError('ONNX artifact does not match the pinned FP32 reranker')
            import onnxruntime as ort
            options = ort.SessionOptions()
            options.intra_op_num_threads = threads
            options.inter_op_num_threads = 1
            options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            options.add_session_config_entry('session.intra_op.allow_spinning', '0')
            options.add_session_config_entry('session.inter_op.allow_spinning', '0')
            self.session = ort.InferenceSession(str(Path(onnx_directory)/'model.onnx'), options, providers=['CPUExecutionProvider'])
            self.backend = 'onnx-fp32'
            self.artifact_sha256 = manifest['files']['model.onnx']
        self.store_path, self.batch_size = store_path, batch_size

    @torch.inference_mode()
    def rank(self, query, ids, k=10):
        if not 1 <= len(ids) <= 50 or len(set(ids)) != len(ids) or not 1 <= k <= 10:
            raise ValueError('rerank requires 1..50 unique IDs and top-k 1..10')
        began = time.perf_counter()
        phase = dict(fetch_seconds=0., tokenize_seconds=0., padding_seconds=0., inference_seconds=0., output_seconds=0.)
        scores = []
        # Retain only this bounded candidate set, never the document collection.
        with closing(open_store(self.store_path)) as db:
            texts = []
            for identifier in ids:
                row = db.execute('SELECT text FROM passages WHERE id=?', (identifier,)).fetchone()
                if row is None:
                    raise KeyError(f'unknown candidate ID: {identifier}')
                texts.append(row[0])
        phase['fetch_seconds'] = time.perf_counter()-began
        token_started = time.perf_counter()
        # Tokenize exactly once, truncate identically, then bucket by length.
        # Attention masking and final score/ID sorting preserve rank semantics.
        encoded = self.tokenizer([query] * len(texts), texts, padding=False, truncation=True, max_length=512)
        order = sorted(range(len(ids)), key=lambda i: (len(encoded['input_ids'][i]), i))
        phase['tokenize_seconds'] = time.perf_counter()-token_started
        padded_tokens = 0
        for start in range(0, len(order), self.batch_size):
            positions = order[start:start + self.batch_size]
            features = [{key: values[i] for key, values in encoded.items()} for i in positions]
            padding_started = time.perf_counter()
            inputs = self.tokenizer.pad(features, padding=True, return_tensors='np' if self.backend == 'onnx-fp32' else 'pt')
            phase['padding_seconds'] += time.perf_counter()-padding_started
            inference_started = time.perf_counter()
            if self.backend == 'onnx-fp32':
                padded_tokens += inputs['input_ids'].size
                logits = self.session.run(['logits'], dict(inputs))[0].reshape(-1).tolist()
            else:
                padded_tokens += inputs['input_ids'].numel()
                logits = self.model(**inputs).logits.reshape(-1).cpu().tolist()
            phase['inference_seconds'] += time.perf_counter()-inference_started
            if len(logits) != len(positions):
                raise ValueError('reranker returned the wrong number of logits')
            for i, score in zip(positions, logits):
                if not math.isfinite(score):
                    raise ValueError('non-finite relevance score')
                scores.append(dict(id=ids[i], score=score))
        output_started = time.perf_counter()
        self.last_work = dict(pairs=len(ids), unpadded_tokens=sum(len(x) for x in encoded['input_ids']),
                              padded_tokens=padded_tokens)
        ranked = sorted(scores, key=lambda hit: (-hit['score'], hit['id']))[:k]
        with closing(open_store(self.store_path)) as db:
            for hit in ranked:
                row = db.execute('SELECT text,metadata FROM passages WHERE id=?', (hit['id'],)).fetchone()
                if row is None:
                    raise KeyError(hit['id'])
                hit['text'], hit['metadata'] = row[0], json.loads(row[1])
        phase['output_seconds'] = time.perf_counter()-output_started
        self.last_work['timings'] = phase
        self.last_work['total_seconds'] = time.perf_counter()-began
        return ranked
