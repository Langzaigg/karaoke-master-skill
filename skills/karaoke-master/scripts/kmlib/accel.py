"""Hardware acceleration: probing and settings (stdlib only).

The skill does not prescribe a per-vendor recipe. It installs a CPU baseline
and gives the agent generic knobs; SKILL.md tells the agent to probe the
machine, try a GPU-capable runtime, verify it and keep whatever is faster.

Settings live in ``<home>/km_config.json → "accel"`` (``km.py accel --set``)
and can be overridden per run with environment variables:

| key | env | used by | values |
|---|---|---|---|
| ``ai_python`` | ``KM_AI_PYTHON`` | every AI subprocess | interpreter of the AI runtime to use |
| ``align_device`` | ``KM_DEVICE`` | SUG forced-alignment worker | any ``torch.device`` string: cpu, cuda, xpu, xpu:1, mps … |
| ``onnx`` | ``KM_ONNX`` | vocal separation (ONNX) | cpu, cuda, dml (DirectML), auto |
| ``dml_device`` | ``KM_DML_DEVICE`` | separation on DirectML | DXGI index, ``igpu`` / ``dgpu`` |
| ``asr_device`` / ``asr_compute`` | ``KM_ASR_DEVICE`` | faster-whisper | cpu / cuda, int8 / float16 … |
| ``encoder`` | ``KM_ENCODER`` | MP4 export | cpu, auto, nvenc, qsv, amf_qvbr, amf_cqp, videotoolbox |
| ``align_jobs`` | ``KM_ALIGN_JOBS`` | parallel alignment workers (opt-in, default 1) | integer |
| ``render_workers`` | ``KM_RENDER_WORKERS`` | subtitle renderer processes (default: half the cores, max 4) | integer |

Every GPU path falls back to CPU when it fails at runtime.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

DEFAULTS = {"ai_python": None, "align_device": "cpu", "onnx": "auto", "dml_device": None,
            "asr_device": "cpu", "asr_compute": "int8", "encoder": "cpu", "align_jobs": None,
            "render_workers": None}
ENV = {"ai_python": "KM_AI_PYTHON", "align_device": "KM_DEVICE", "onnx": "KM_ONNX", "dml_device": "KM_DML_DEVICE",
       "asr_device": "KM_ASR_DEVICE", "asr_compute": "KM_ASR_COMPUTE", "encoder": "KM_ENCODER",
       "align_jobs": "KM_ALIGN_JOBS", "render_workers": "KM_RENDER_WORKERS"}


# ------------------------------------------------------------------ hardware
def list_gpus() -> list[str]:
    names: list[str] = []
    try:
        if os.name == "nt":
            out = subprocess.run(["powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_VideoController).Name"],
                                 capture_output=True, text=True, timeout=20).stdout
        else:
            out = subprocess.run(["sh", "-c", "lspci 2>/dev/null | grep -Ei 'vga|3d|display'"],
                                 capture_output=True, text=True, timeout=20).stdout
        names = [l.strip() for l in out.splitlines() if l.strip()]
    except Exception:
        pass
    if shutil.which("nvidia-smi") and not any("nvidia" in n.lower() for n in names):
        names.append("NVIDIA GPU (nvidia-smi)")
    return names


def dxgi_adapters() -> list[dict]:
    """DXGI adapter list (Windows) in enumeration order — the index DirectML's
    ``device_id`` refers to."""
    if os.name != "nt":
        return []
    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("d1", wintypes.DWORD), ("d2", wintypes.WORD), ("d3", wintypes.WORD), ("d4", ctypes.c_ubyte * 8)]

    class DESC1(ctypes.Structure):
        _fields_ = [("Description", ctypes.c_wchar * 128), ("VendorId", wintypes.UINT), ("DeviceId", wintypes.UINT),
                    ("SubSysId", wintypes.UINT), ("Revision", wintypes.UINT),
                    ("DedicatedVideoMemory", ctypes.c_size_t), ("DedicatedSystemMemory", ctypes.c_size_t),
                    ("SharedSystemMemory", ctypes.c_size_t), ("LuidLow", wintypes.DWORD), ("LuidHigh", wintypes.LONG),
                    ("Flags", wintypes.UINT)]

    iid = GUID(0x770AAE78, 0xF26F, 0x4DBA, (ctypes.c_ubyte * 8)(0xA8, 0x29, 0x25, 0x3C, 0x83, 0xD1, 0xB3, 0x87))
    factory = ctypes.c_void_p()
    try:
        if ctypes.WinDLL("dxgi").CreateDXGIFactory1(ctypes.byref(iid), ctypes.byref(factory)) != 0:
            return []
    except OSError:
        return []

    def vcall(obj, index, restype, *argtypes):
        vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtbl[index])

    out, i = [], 0
    while True:
        adapter = ctypes.c_void_p()
        if vcall(factory, 12, ctypes.c_long, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p))(
                factory, i, ctypes.byref(adapter)) != 0:
            break
        desc = DESC1()
        vcall(adapter, 10, ctypes.c_long, ctypes.POINTER(DESC1))(adapter, ctypes.byref(desc))
        vcall(adapter, 2, wintypes.ULONG)(adapter)
        out.append({"index": i, "name": desc.Description, "vram_mb": desc.DedicatedVideoMemory // 2**20,
                    "software": bool(desc.Flags & 2)})
        i += 1
    vcall(factory, 2, wintypes.ULONG)(factory)
    return out


_RUNTIME_PROBE = r"""
import json, sys
info = {"python": sys.executable}
try:
    import torch
    info["torch"] = torch.__version__
    devs = []
    if torch.cuda.is_available():
        devs += [f"cuda:{i} {torch.cuda.get_device_name(i)}" for i in range(torch.cuda.device_count())]
    if getattr(torch, "xpu", None) is not None and torch.xpu.is_available():
        devs += [f"xpu:{i} {torch.xpu.get_device_name(i)}" for i in range(torch.xpu.device_count())]
    mps = getattr(torch.backends, "mps", None)
    if mps is not None and mps.is_available():
        devs.append("mps")
    info["torch_devices"] = devs
except Exception as exc:
    info["torch_error"] = str(exc)
try:
    import onnxruntime as ort
    info["onnxruntime"] = ort.__version__
    info["onnx_providers"] = ort.get_available_providers()
except Exception as exc:
    info["onnx_error"] = str(exc)
try:
    import ctranslate2
    info["ctranslate2_cuda_devices"] = ctranslate2.get_cuda_device_count()
except Exception as exc:
    info["ctranslate2_error"] = str(exc)
print(json.dumps(info))
"""


def probe_runtime(python: str | Path) -> dict:
    """What the AI runtime can accelerate: torch devices, ONNX providers, CTranslate2 CUDA."""
    try:
        res = subprocess.run([str(python), "-c", _RUNTIME_PROBE], capture_output=True, text=True, timeout=240,
                             encoding="utf-8", errors="replace")
        return json.loads(res.stdout.strip().splitlines()[-1])
    except Exception as exc:
        return {"error": str(exc)}


def ffmpeg_hw_encoders() -> list[str]:
    from .paths import ffmpeg_exe

    try:
        out = subprocess.run([ffmpeg_exe(), "-hide_banner", "-encoders"], capture_output=True, text=True,
                             timeout=30).stdout
    except Exception:
        return []
    return sorted(set(re.findall(r"\b(h264_(?:nvenc|qsv|amf|videotoolbox|vaapi|mf)|hevc_(?:nvenc|qsv|amf))\b", out)))


# ------------------------------------------------------------------ settings
def config_path(home: Path) -> Path:
    return home / "km_config.json"


def load(home: Path) -> dict:
    try:
        return json.loads(config_path(home).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save(home: Path, data: dict) -> None:
    config_path(home).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def profile() -> dict:
    """Effective acceleration settings for runtime code (CPU defaults)."""
    from .paths import km_home

    prof = {**DEFAULTS, **(load(km_home()).get("accel") or {})}
    for key, env in ENV.items():
        if os.environ.get(env):
            prof[key] = os.environ[env]
    return prof


def set_values(home: Path, pairs: list[str]) -> dict:
    cfg = load(home)
    acc = dict(cfg.get("accel") or {})
    for pair in pairs:
        key, _, value = pair.partition("=")
        key = key.strip()
        if key not in DEFAULTS:
            raise SystemExit(f"未知加速设置：{key}（可选 {', '.join(DEFAULTS)}）")
        value = value.strip()
        if value in ("", "default", "none"):
            acc.pop(key, None)
        else:
            acc[key] = int(value) if key in ("align_jobs", "render_workers") else value
    cfg["accel"] = acc
    save(home, cfg)
    return acc


def report(home: Path, ai_python: Path, *, deep: bool = True) -> dict:
    out = {"gpus": list_gpus(), "settings": profile(), "ffmpeg_hw_encoders": ffmpeg_hw_encoders()}
    if os.name == "nt":
        out["dxgi_adapters"] = [a for a in dxgi_adapters() if not a["software"]]
    if deep:
        out["ai_runtime"] = probe_runtime(profile().get("ai_python") or ai_python)
    return out
