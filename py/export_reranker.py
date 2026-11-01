#!/usr/bin/env python3
"""Export the pinned MiniLM checkpoint to a checksummed FP32 CPU ONNX graph."""
import argparse
import json
from pathlib import Path
import shutil
import torch
from transformers import AutoModelForSequenceClassification
from common import CROSS, lock_model, sha256


class Logits(torch.nn.Module):
    def __init__(self, model):
        super().__init__()
        self.model = model

    def forward(self, input_ids, attention_mask, token_type_ids):
        return self.model(input_ids=input_ids, attention_mask=attention_mask, token_type_ids=token_type_ids).logits


def export(model_lock: str, output: Path) -> None:
    lock = lock_model(model_lock, CROSS)
    output.mkdir(parents=True, exist_ok=False)
    try:
        torch.set_num_threads(2)
        model = AutoModelForSequenceClassification.from_pretrained(lock['path'], local_files_only=True,
                    use_safetensors=True, attn_implementation='eager').eval()
        sample = torch.ones((2, 16), dtype=torch.long)
        with torch.inference_mode():
            torch.onnx.export(Logits(model), (sample, sample, torch.zeros_like(sample)), str(output/'unfused.onnx'),
                    input_names=['input_ids','attention_mask','token_type_ids'], output_names=['logits'],
                    dynamic_axes={name:{0:'batch',1:'sequence'} for name in ('input_ids','attention_mask','token_type_ids')}
                                 | {'logits':{0:'batch'}}, opset_version=17, dynamo=False)
        import onnx
        from onnxruntime.transformers.optimizer import optimize_model
        from onnxruntime.transformers.fusion_options import FusionOptions
        options = FusionOptions('bert')
        options.enable_gelu_approximation = False
        optimized = optimize_model(str(output/'unfused.onnx'), model_type='bert',
                     num_heads=model.config.num_attention_heads, hidden_size=model.config.hidden_size,
                     optimization_options=options, opt_level=1, use_gpu=False)
        optimized.save_model_to_file(str(output/'model.onnx'))
        (output/'unfused.onnx').unlink()
        onnx.checker.check_model(str(output/'model.onnx'))
        manifest = dict(format_version=1, model=CROSS, model_revision=lock['revision'],
                        weights_sha256=lock['weights_sha256'], dtype='float32', opset=17,
                        max_length=512, fused_operator_statistics=optimized.get_fused_operator_statistics(), files={'model.onnx':sha256(output/'model.onnx')})
        (output/'manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')
        print(json.dumps(manifest, indent=2), flush=True)
    except BaseException:
        shutil.rmtree(output)
        raise


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--model-lock',default='data/phase2-models.json')
    ap.add_argument('--out',type=Path,required=True)
    args=ap.parse_args()
    export(args.model_lock,args.out)
