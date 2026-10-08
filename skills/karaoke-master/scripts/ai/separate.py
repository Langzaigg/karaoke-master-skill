"""Vocal separation worker (runs inside the AI runtime venv).

Usage: python separate.py INPUT OUT_DIR MODEL_DIR [--provider auto|cpu|cuda|dml]

Same engine and model as StrangeUtaGame's separator (audio-separator +
UVR-MDX-NET-Inst_HQ_3.onnx), but the ONNX execution provider is chosen here so
DirectML (AMD / Intel GPUs on Windows) works without torch_directml.
Prints ``progress:<0-100>:<msg>`` lines and finally ``done:<vocals>|<instrumental>``.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import shutil
import sys

MODEL = "UVR-MDX-NET-Inst_HQ_3.onnx"


class _Progress(logging.Handler):
    pat = re.compile(r"(\d{1,3})%\|")

    def emit(self, record):
        msg = record.getMessage()
        m = self.pat.search(msg)
        if m:
            print(f"progress:{10 + int(int(m.group(1)) * 0.85)}:分离处理中", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("out_dir")
    ap.add_argument("model_dir")
    ap.add_argument("--provider", default="auto")
    ap.add_argument("--dml-device", default=None,
                    help="DirectML adapter: index, or igpu (minimum_power) / dgpu (high_performance)")
    args = ap.parse_args()
    print("progress:2:初始化分离引擎", flush=True)
    logging.basicConfig(level=logging.WARNING)
    import onnxruntime as ort
    from audio_separator.separator import Separator

    sep = Separator(model_file_dir=args.model_dir, output_dir=args.out_dir, output_format="WAV")
    avail = ort.get_available_providers()
    wanted = {"dml": "DmlExecutionProvider", "cuda": "CUDAExecutionProvider"}
    chosen = None
    if args.provider in ("auto", "cuda") and "CUDAExecutionProvider" in avail:
        chosen = "CUDAExecutionProvider"
    elif args.provider in ("auto", "dml") and "DmlExecutionProvider" in avail:
        chosen = "DmlExecutionProvider"
    elif args.provider in wanted:
        print(f"progress:3:{wanted[args.provider]} 不可用，改用 CPU", flush=True)
    if chosen == "DmlExecutionProvider" and args.dml_device:
        opts = {}
        if args.dml_device.isdigit():
            opts["device_id"] = int(args.dml_device)
        else:
            opts["performance_preference"] = {"igpu": "minimum_power", "dgpu": "high_performance"}.get(
                args.dml_device, args.dml_device)
            opts["device_filter"] = "gpu"
        sep.onnx_execution_provider = [(chosen, opts), "CPUExecutionProvider"]
        print(f"progress:4:DirectML 适配器选择 {opts}", flush=True)
    elif chosen:
        sep.onnx_execution_provider = [chosen, "CPUExecutionProvider"]
    print(f"provider:{(sep.onnx_execution_provider or ['CPUExecutionProvider'])[0]}", flush=True)
    print("progress:6:加载分离模型", flush=True)
    sep.load_model(model_filename=MODEL)
    # tqdm writes chunk progress to stderr; mirror it as progress lines
    import tqdm

    orig_update = tqdm.tqdm.update

    def update(self, n=1):
        res = orig_update(self, n)
        if self.total:
            print(f"progress:{10 + int(85 * self.n / self.total)}:分离处理中 {self.n}/{self.total}", flush=True)
        return res

    tqdm.tqdm.update = update
    outputs = sep.separate(args.input)
    stem = os.path.splitext(os.path.basename(args.input))[0]
    vocals = next((f for f in outputs if "vocals" in f.lower()), None)
    inst = next((f for f in outputs if "instrumental" in f.lower()), None)
    if vocals is None:
        print("error:未找到人声输出轨 " + repr(outputs), flush=True)
        return 1
    dst = os.path.join(args.out_dir, stem + "_人声.wav")
    shutil.move(os.path.join(args.out_dir, os.path.basename(vocals)), dst)
    inst_path = os.path.join(args.out_dir, os.path.basename(inst)) if inst else ""
    print("progress:100:分离完成", flush=True)
    print(f"done:{dst}|{inst_path}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
