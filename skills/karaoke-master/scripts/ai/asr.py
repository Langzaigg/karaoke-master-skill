"""faster-whisper transcription worker (runs inside the AI runtime venv).

Usage: python asr.py AUDIO OUT_JSON [--model large-v3-turbo] [--language ja]
       [--models-dir DIR] [--device cpu] [--start S --end E]

Prints ``progress:<0-100>:<message>`` lines on stdout and writes::

    {"model": ..., "language": ..., "duration": ...,
     "segments": [{"start", "end", "text", "words": [{"s", "e", "w", "p"}]}]}
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("audio")
    ap.add_argument("out")
    ap.add_argument("--model", default="large-v3-turbo")
    ap.add_argument("--language", default="ja")
    ap.add_argument("--models-dir", default=None)
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--compute-type", default="int8")
    ap.add_argument("--prompt", default=None)
    args = ap.parse_args()

    print("progress:1:加载语音识别模型", flush=True)
    from faster_whisper import WhisperModel

    threads = max(1, (os.cpu_count() or 4) - 1)
    if args.device == "cuda" and os.name == "nt":
        # pip-installed cuBLAS / cuDNN DLLs (nvidia-*-cu12) are not on PATH by default
        import site

        for base in site.getsitepackages():
            for sub in ("nvidia/cublas/bin", "nvidia/cudnn/bin"):
                d = os.path.join(base, sub)
                if os.path.isdir(d):
                    os.add_dll_directory(d)
                    os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
    try:
        model = WhisperModel(args.model, device=args.device, compute_type=args.compute_type,
                             download_root=args.models_dir, cpu_threads=threads)
    except Exception as exc:
        if args.device == "cpu":
            raise
        print(f"progress:2:{args.device} 不可用（{exc}），改用 CPU", flush=True)
        model = WhisperModel(args.model, device="cpu", compute_type="int8",
                             download_root=args.models_dir, cpu_threads=threads)
    print("progress:5:开始识别", flush=True)
    t0 = time.time()
    # Decode with ffmpeg ourselves: faster-whisper's PyAV path breaks on some av releases.
    import subprocess

    import numpy as np

    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", args.audio, "-ac", "1", "-ar", "16000", "-f", "f32le", "-"],
                         capture_output=True, check=True).stdout
    audio = np.frombuffer(raw, dtype=np.float32).copy()
    segments, info = model.transcribe(
        audio,
        language=args.language or None,
        word_timestamps=True,
        # Singing: Silero VAD (trained on speech) drops sung passages, and
        # previous-text conditioning keeps whisper "in the song".
        vad_filter=False,
        condition_on_previous_text=True,
        beam_size=5,
        initial_prompt=args.prompt,
    )
    duration = float(info.duration or 0.0)
    out = []
    for seg in segments:
        words = [{"s": round(w.start, 3), "e": round(w.end, 3), "w": w.word, "p": round(w.probability, 3)}
                 for w in (seg.words or [])]
        out.append({"start": round(seg.start, 3), "end": round(seg.end, 3), "text": seg.text.strip(), "words": words})
        pct = 5 + int(94 * min(1.0, seg.end / duration)) if duration else 50
        print(f"progress:{pct}:已识别 {seg.end:.0f}/{duration:.0f} 秒", flush=True)
    data = {"model": args.model, "language": info.language, "duration": duration,
            "elapsed": round(time.time() - t0, 1), "segments": out}
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    print("progress:100:识别完成", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
