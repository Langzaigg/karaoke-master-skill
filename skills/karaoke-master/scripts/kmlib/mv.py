"""MV designer: generated backgrounds for songs without (or instead of) a video.

Two designs per job, chosen by the background type:

* ``mv`` (AMV) — a themed scene the agent designs for the song: palette, a key
  visual (usually the cover art), motif layers and audio-reactive visualizers;
* ``montage`` (图片混剪) — a beat-synced cut of an image pool (the user's image
  packs, images found online, scenes from the source video, or a mix) with
  visualizer layers on top.

Both are a *scene spec* (JSON) started from a preset. This module renders stills
for confirmation and the full background video, which is used both as the plain
MV and as the karaoke background.

Spec (``render/mv_spec.json`` / ``render/montage_spec.json``)::

    {"preset": "starry_prayer", "theme": "祈愿 · 星空 · 生命", "notes": "...",
     "palette": ["#060A1E", "#1D1650", "#7FD3FF", "#FFB7E6", "#F6E7A1"] | "auto",
     "fps": 30, "layers": [{"type": ..., ...}, ...]}

Colours may reference the palette as ``"$0".."$4"``. Positions/sizes are
fractions of the frame height/width. ``react`` ties a layer to ``bass`` /
``mid`` / ``high`` / ``energy`` (song-section intensity) / ``onset`` with
strength ``k``. Keep the lower third calm (dark ground, ``lyrics_band``) —
karaoke lines live there. Layer catalog: references/recipes.md (MV section).
"""

from __future__ import annotations

import copy
import json
import math
import subprocess
import time
from pathlib import Path

import numpy as np

from .jobstore import JobStore, read_json, write_json
from .media import load_mono
from .paths import ffmpeg_exe, hidden_subprocess_kwargs

FPS = 30
BANDS = 72

# --------------------------------------------------------------------- presets
PRESETS: dict[str, dict] = {
    "starry_prayer": {
        "name": "星空祈愿", "desc": "深蓝星空、极光、月与大树剪影，萤火随低音明灭 —— 祈祷 / 抒情 / 幻想",
        "palette": ["#060A1E", "#241A5C", "#7FD3FF", "#FFB7E6", "#F6E7A1"],
        "layers": [
            {"type": "gradient", "stops": [[0, "#02040C"], [0.5, "$1"], [0.72, "#3B2A72"], [1, "#05060F"]]},
            {"type": "stars", "count": 280, "react": "high", "k": 0.7},
            {"type": "aurora", "colors": ["$2", "$3"], "y": 0.2, "amp": 0.07, "react": "mid", "k": 0.6},
            {"type": "moon", "x": 0.86, "y": 0.15, "r": 0.05, "color": "$4", "glow": 0.7},
            {"type": "spectrum", "style": "horizon", "y": 0.66, "x0": 0.04, "x1": 0.96, "height": 0.2,
             "colors": ["$2", "$3"], "alpha": 0.75},
            {"type": "silhouette", "shape": "hills", "y": 0.66, "color": "#04050D"},
            {"type": "silhouette", "shape": "tree", "x": 0.16, "y": 0.68, "size": 0.62, "color": "#03040A"},
            {"type": "image", "src": "cover", "shape": "circle", "x": 0.62, "y": 0.36, "size": 0.27, "spin": 5,
             "ring": "$2", "optional": True},
            {"type": "pulse", "x": 0.62, "y": 0.36, "r": 0.42, "color": "$3", "react": "bass", "k": 0.4},
            {"type": "particles", "kind": "fireflies", "count": 46, "color": "$4", "react": "bass", "k": 0.9},
            {"type": "lyrics_band", "from": 0.64, "alpha": 0.5},
            {"type": "vignette", "strength": 0.6},
            {"type": "grain", "amount": 0.03},
        ],
    },
    "sakura_spring": {
        "name": "樱花春日", "desc": "粉白天空、远山与漫天樱花，频谱如山脊起伏 —— 恋爱 / 青春 / 毕业",
        "palette": ["#FFE9F1", "#F7B6CF", "#E2678E", "#7A4C8F", "#FFF6D8"],
        "layers": [
            {"type": "gradient", "stops": [[0, "#FCD7E4"], [0.45, "$0"], [0.7, "#F3C3D6"], [1, "#3B2440"]]},
            {"type": "sun", "x": 0.8, "y": 0.2, "r": 0.09, "color": "$4", "glow": 0.8, "react": "energy", "k": 0.3},
            {"type": "clouds", "count": 6, "color": "#FFFFFF", "alpha": 0.5, "y0": 0.08, "y1": 0.35},
            {"type": "silhouette", "shape": "mountains", "y": 0.58, "color": "#C98BAE", "layers": 2},
            {"type": "spectrum", "style": "wave", "y": 0.66, "x0": 0.0, "x1": 1.0, "height": 0.16,
             "colors": ["$2", "$1"], "alpha": 0.8},
            {"type": "silhouette", "shape": "hills", "y": 0.66, "color": "#4A2E52"},
            {"type": "image", "src": "cover", "shape": "rounded", "x": 0.5, "y": 0.33, "size": 0.3, "optional": True},
            {"type": "particles", "kind": "petals", "count": 90, "color": "$1", "react": "mid", "k": 0.6},
            {"type": "lyrics_band", "from": 0.64, "alpha": 0.45},
            {"type": "vignette", "strength": 0.35},
        ],
    },
    "rainy_night": {
        "name": "雨夜", "desc": "冷色雨夜、模糊街灯光斑与雨丝，示波器式波形 —— 失恋 / 孤独 / 回忆",
        "palette": ["#0A0F1A", "#1B2A3D", "#6FA8D6", "#E8C27A", "#B7C7D9"],
        "layers": [
            {"type": "cover_bg", "blur": 1, "dim": 0.75, "optional": True},
            {"type": "gradient", "stops": [[0, "#0A0F1AAA"], [0.6, "#1B2A3DAA"], [1, "#05070CEE"]]},
            {"type": "particles", "kind": "bokeh", "count": 34, "color": "$3", "react": "energy", "k": 0.5},
            {"type": "silhouette", "shape": "city", "y": 0.66, "color": "#070A12", "windows": "$3"},
            {"type": "waveform", "y": 0.5, "height": 0.12, "color": "$4", "alpha": 0.8},
            {"type": "spectrum", "style": "mirror", "y": 0.5, "x0": 0.2, "x1": 0.8, "height": 0.14,
             "colors": ["$2", "$4"], "alpha": 0.35},
            {"type": "particles", "kind": "rain", "count": 260, "color": "#B9D3EE", "react": "high", "k": 0.4},
            {"type": "lyrics_band", "from": 0.62, "alpha": 0.55},
            {"type": "vignette", "strength": 0.65},
            {"type": "grain", "amount": 0.045},
        ],
    },
    "neon_city": {
        "name": "霓虹都市", "desc": "合成器浪潮：条纹落日、霓虹网格地面与城市天际线 —— 电子 / 流行 / 都市",
        "palette": ["#0D0221", "#2B0F54", "#FF2E97", "#2DE2E6", "#FFD319"],
        "layers": [
            {"type": "gradient", "stops": [[0, "#0D0221"], [0.45, "$1"], [0.62, "#AB1F65"], [0.63, "#0D0221"], [1, "#05010D"]]},
            {"type": "stars", "count": 120, "react": "high", "k": 0.4},
            {"type": "sun", "x": 0.5, "y": 0.44, "r": 0.17, "color": "$4", "color2": "$2", "stripes": True,
             "glow": 0.6, "react": "bass", "k": 0.15},
            {"type": "silhouette", "shape": "city", "y": 0.62, "color": "#0B0420", "windows": "$3"},
            {"type": "grid", "horizon": 0.62, "color": "$2", "speed": 0.5, "react": "bass", "k": 0.6},
            {"type": "spectrum", "style": "horizon", "y": 0.62, "x0": 0.0, "x1": 1.0, "height": 0.22,
             "colors": ["$3", "$2"], "alpha": 0.85},
            {"type": "flash", "color": "$3", "react": "onset", "k": 0.12},
            {"type": "lyrics_band", "from": 0.66, "alpha": 0.5},
            {"type": "scanlines", "alpha": 0.08},
            {"type": "vignette", "strength": 0.5},
        ],
    },
    "ocean_summer": {
        "name": "夏日海岸", "desc": "晴空、太阳与随低音起伏的海浪，海面闪光 —— 夏日 / 清爽 / 旅行",
        "palette": ["#7FD6FF", "#2E8BD8", "#0B3D6B", "#FFF2A8", "#FFFFFF"],
        "layers": [
            {"type": "gradient", "stops": [[0, "#3FA9F5"], [0.5, "$0"], [0.58, "#BFEFFF"], [1, "$2"]]},
            {"type": "sun", "x": 0.78, "y": 0.18, "r": 0.07, "color": "$3", "glow": 0.9},
            {"type": "clouds", "count": 5, "color": "#FFFFFF", "alpha": 0.75, "y0": 0.08, "y1": 0.3},
            {"type": "light_rays", "x": 0.78, "y": 0.18, "color": "$3", "count": 10, "react": "energy", "k": 0.4},
            {"type": "image", "src": "cover", "shape": "rounded", "x": 0.32, "y": 0.32, "size": 0.3, "optional": True},
            {"type": "spectrum", "style": "bars", "y": 0.56, "x0": 0.55, "x1": 0.95, "height": 0.16,
             "colors": ["$4", "$0"], "alpha": 0.8},
            {"type": "silhouette", "shape": "waves", "y": 0.58, "color": "$1", "react": "bass", "k": 0.6},
            {"type": "particles", "kind": "sparkles", "count": 50, "color": "#FFFFFF", "y0": 0.6, "y1": 0.8,
             "react": "high", "k": 0.8},
            {"type": "lyrics_band", "from": 0.66, "alpha": 0.45},
        ],
    },
    "winter_snow": {
        "name": "冬夜飞雪", "desc": "冷蓝雪山、极光与雪花，环形频谱如冰晶 —— 冬日 / 思念 / 温柔",
        "palette": ["#0A1630", "#1F3B6E", "#9FE7FF", "#E6F4FF", "#C7B8FF"],
        "layers": [
            {"type": "gradient", "stops": [[0, "#050B1C"], [0.55, "$1"], [1, "#0A0E1E"]]},
            {"type": "stars", "count": 160, "react": "high", "k": 0.5},
            {"type": "aurora", "colors": ["$2", "$4"], "y": 0.18, "amp": 0.05, "react": "mid", "k": 0.5},
            {"type": "silhouette", "shape": "mountains", "y": 0.62, "color": "#2B3F66", "snow": True, "layers": 3},
            {"type": "spectrum", "style": "ring", "x": 0.5, "y": 0.34, "radius": 0.13, "height": 0.12,
             "colors": ["$2", "$3"], "alpha": 0.9},
            {"type": "image", "src": "cover", "shape": "circle", "x": 0.5, "y": 0.34, "size": 0.22, "spin": 4,
             "ring": "$3", "optional": True},
            {"type": "particles", "kind": "snow", "count": 160, "color": "$3", "react": "energy", "k": 0.4},
            {"type": "lyrics_band", "from": 0.64, "alpha": 0.55},
            {"type": "vignette", "strength": 0.55},
        ],
    },
    "sunset_nostalgia": {
        "name": "黄昏怀旧", "desc": "橙紫晚霞、落日与云影，胶片颗粒，柱状频谱 —— 怀旧 / 告别 / 温暖",
        "palette": ["#2A1B3D", "#7B3F61", "#F28C38", "#FFD27D", "#FFF1D6"],
        "layers": [
            {"type": "gradient", "stops": [[0, "#2A1B3D"], [0.35, "$1"], [0.55, "$2"], [0.66, "$3"], [1, "#1A1022"]]},
            {"type": "sun", "x": 0.5, "y": 0.6, "r": 0.11, "color": "$4", "glow": 0.9, "react": "energy", "k": 0.25},
            {"type": "clouds", "count": 7, "color": "#5A2D4F", "alpha": 0.6, "y0": 0.1, "y1": 0.45},
            {"type": "spectrum", "style": "bars", "y": 0.66, "x0": 0.05, "x1": 0.95, "height": 0.18,
             "colors": ["$4", "$2"], "alpha": 0.7, "mirror": True},
            {"type": "silhouette", "shape": "hills", "y": 0.66, "color": "#140B19"},
            {"type": "image", "src": "cover", "shape": "rounded", "x": 0.5, "y": 0.3, "size": 0.26, "optional": True},
            {"type": "particles", "kind": "dust", "count": 60, "color": "$4", "react": "mid", "k": 0.5},
            {"type": "lyrics_band", "from": 0.66, "alpha": 0.5},
            {"type": "vignette", "strength": 0.6},
            {"type": "grain", "amount": 0.06},
        ],
    },
    "cosmic_battle": {
        "name": "燃 · 星海", "desc": "深空星云、放射光束与火花，节拍闪光与环形频谱 —— 战斗 / 热血 / 高燃",
        "palette": ["#05030F", "#3A0F5C", "#FF4D6D", "#FFB703", "#7AE7FF"],
        "layers": [
            {"type": "gradient", "stops": [[0, "#05030F"], [0.5, "$1"], [1, "#04020A"]]},
            {"type": "nebula", "colors": ["$1", "$2", "$4"], "react": "energy", "k": 0.5},
            {"type": "stars", "count": 220, "react": "high", "k": 0.8},
            {"type": "light_rays", "x": 0.5, "y": 0.36, "color": "$3", "count": 16, "react": "bass", "k": 0.7},
            {"type": "spectrum", "style": "ring", "x": 0.5, "y": 0.36, "radius": 0.14, "height": 0.16,
             "colors": ["$2", "$3"], "alpha": 1.0},
            {"type": "image", "src": "cover", "shape": "circle", "x": 0.5, "y": 0.36, "size": 0.24, "spin": 10,
             "ring": "$3", "optional": True},
            {"type": "particles", "kind": "embers", "count": 90, "color": "$3", "react": "bass", "k": 1.0},
            {"type": "flash", "color": "#FFFFFF", "react": "onset", "k": 0.1},
            {"type": "lyrics_band", "from": 0.64, "alpha": 0.6},
            {"type": "vignette", "strength": 0.55},
        ],
    },
    "anime_montage": {
        "name": "标准混剪", "desc": "图片按节拍剪辑：不重复、重复段落沿用；缓慢推拉、交叉淡化、频谱点缀 —— 动画 / 游戏 / 通用",
        "palette": "auto",
        "layers": [
            {"type": "montage", "bars": 2, "transitions": "auto", "dim": 0.08},
            {"type": "particles", "kind": "sparkles", "count": 28, "color": "#FFFFFF", "y0": 0.05, "y1": 0.6,
             "react": "high", "k": 0.6},
            {"type": "lyrics_band", "from": 0.58, "alpha": 0.7},
            {"type": "spectrum", "style": "mirror", "y": 0.07, "x0": 0.3, "x1": 0.7, "height": 0.07,
             "colors": ["$0", "$1"], "alpha": 0.55, "bands": 48},
            {"type": "vignette", "strength": 0.45},
            {"type": "progress", "color": "$0"},
        ],
    },
    "montage_cinematic": {
        "name": "电影感混剪", "desc": "长镜头慢推、交叉淡化、上下遮幅与胶片颗粒，细线波形 —— 抒情 / 叙事 / 回忆",
        "palette": "auto",
        "layers": [
            {"type": "montage", "bars": 4, "transitions": "crossfade", "trans": 0.9, "dim": 0.12},
            {"type": "particles", "kind": "dust", "count": 40, "color": "#FFF3D6", "react": "mid", "k": 0.4},
            {"type": "letterbox", "size": 0.09},
            {"type": "lyrics_band", "from": 0.6, "alpha": 0.55},
            {"type": "waveform", "y": 0.045, "x0": 0.25, "x1": 0.75, "height": 0.02, "color": "#FFFFFF",
             "alpha": 0.5},
            {"type": "vignette", "strength": 0.6},
            {"type": "grain", "amount": 0.045},
        ],
    },
    "montage_beat": {
        "name": "高燃踩点混剪", "desc": "每小节一切、强拍白闪与镜头冲击（画面随鼓点抖动）、余烬粒子与跳动频谱 —— 战歌 / 摇滚 / 燃向",
        "palette": "auto",
        "layers": [
            {"type": "montage", "bars": 1, "min_shot": 1.0, "transitions": "beat", "punch": 0.07, "dim": 0.1},
            {"type": "flash", "color": "#FFFFFF", "react": "onset", "k": 0.25},
            {"type": "particles", "kind": "embers", "count": 70, "color": "$0", "react": "bass", "k": 0.8},
            {"type": "lyrics_band", "from": 0.58, "alpha": 0.72},
            {"type": "spectrum", "style": "bars", "y": 0.11, "x0": 0.2, "x1": 0.8, "height": 0.08,
             "colors": ["$0", "$2"], "alpha": 0.7, "bands": 56},
            {"type": "vignette", "strength": 0.5},
            {"type": "scanlines", "alpha": 0.05},
            {"type": "progress", "color": "$0"},
        ],
    },
    "game_montage": {
        "name": "游戏素材卡点混剪", "desc": "游戏 PV / 实况 / 过场的视频片段按小节卡点硬切（安静段落短交叉淡化），"
                                      "片段动感随音乐能量匹配，副歌重复段沿用首次的镜头 —— 游戏 / 动画 / 视频素材",
        "palette": "auto",
        "layers": [
            {"type": "montage", "bars": 2, "transitions": "auto", "trans": 0.35, "dim": 0.1, "min_shot": 1.2,
             "max_shot": 8.0},
            {"type": "lyrics_band", "from": 0.6, "alpha": 0.62},
            {"type": "vignette", "strength": 0.4},
            {"type": "progress", "color": "$0"},
        ],
    },
    "spectrum_classic": {
        "name": "经典频谱", "desc": "封面模糊铺底 + 旋转封面唱片 + 环形频谱 —— 通用、稳妥",
        "palette": "auto",
        "layers": [
            {"type": "cover_bg", "blur": 1, "dim": 0.65, "optional": True},
            {"type": "pulse", "x": 0.5, "y": 0.4, "r": 0.4, "color": "$0", "react": "bass", "k": 0.45},
            {"type": "particles", "kind": "bokeh", "count": 50, "color": "$1", "react": "bass", "k": 0.7},
            {"type": "spectrum", "style": "ring", "x": 0.5, "y": 0.4, "radius": 0.16, "height": 0.17,
             "colors": ["$0", "$1", "$2"], "alpha": 1.0},
            {"type": "image", "src": "cover", "shape": "circle", "x": 0.5, "y": 0.4, "size": 0.27, "spin": 9,
             "ring": "#FFFFFF", "optional": True},
            {"type": "lyrics_band", "from": 0.62, "alpha": 0.55},
            {"type": "vignette", "strength": 0.6},
            {"type": "progress", "color": "$0"},
        ],
    },
}
DEFAULT_PRESET = "spectrum_classic"
KINDS = ("mv", "montage")  # AMV design / image montage
# bump when the montage planner's output changes for the same inputs (the design
# signature below includes it so a stale rendered background is not reused)
PLANNER_VERSION = 3
DEFAULT_PRESETS = {"mv": DEFAULT_PRESET, "montage": "anime_montage"}
KIND_LABEL = {"mv": "AMV", "montage": "混剪"}


def preset_kind(pid: str) -> str:
    return "montage" if montage_layer(PRESETS[pid]) else "mv"


def catalog(kind: str | None = None) -> list[dict]:
    return [{"id": k, "name": v["name"], "desc": v["desc"], "kind": preset_kind(k)} for k, v in PRESETS.items()
            if kind is None or preset_kind(k) == kind]


def preset_spec(pid: str) -> dict:
    if pid not in PRESETS:
        raise SystemExit(f"未知 MV 预设：{pid}（可选 {', '.join(PRESETS)}）")
    spec = copy.deepcopy(PRESETS[pid])
    spec["preset"] = pid
    return spec


# --------------------------------------------------------------------- audio features
def features(audio: Path, cache: Path | None = None, fps: int = FPS) -> dict:
    """Per-frame audio features (cached as .npz next to the job's render dir)."""
    audio = Path(audio)
    if cache and cache.is_file() and cache.stat().st_mtime >= audio.stat().st_mtime:
        data = np.load(cache)
        if "flux" in data.files:
            return {k: data[k] for k in data.files} | {"sr": int(data["sr"]), "fps": int(data["fps"])}
    sr = 22050
    y = load_mono(audio, sr)
    hop = sr // fps
    n_fft = 4096
    pad = np.pad(y, (n_fft // 2, n_fft // 2))
    frames = 1 + (len(pad) - n_fft) // hop
    window = np.hanning(n_fft).astype(np.float32)
    freqs = np.fft.rfftfreq(n_fft, 1 / sr)
    edges = np.geomspace(40, 11000, BANDS + 1)
    idx = [np.where((freqs >= edges[i]) & (freqs < edges[i + 1]))[0] for i in range(BANDS)]
    idx = [ix if len(ix) else np.array([np.argmin(np.abs(freqs - edges[i]))]) for i, ix in enumerate(idx)]
    raw = np.zeros((frames, BANDS), dtype=np.float32)
    flux = np.zeros(frames, dtype=np.float32)
    prev = None
    for start in range(0, frames, 384):
        stop = min(frames, start + 384)
        segs = np.stack([pad[i * hop:i * hop + n_fft] for i in range(start, stop)]) * window
        mag = np.abs(np.fft.rfft(segs, axis=1)).astype(np.float32)
        for b, ix in enumerate(idx):
            raw[start:stop, b] = mag[:, ix].mean(axis=1)
        logm = np.log1p(mag[:, :1200])
        d = np.diff(logm, axis=0, prepend=logm[:1] if prev is None else prev[None, :])
        flux[start:stop] = np.maximum(d, 0).sum(axis=1)
        prev = logm[-1]
    db = 20 * np.log10(raw + 1e-6)
    lo, hi = np.percentile(db, 10, axis=0), np.percentile(db, 99.3, axis=0)
    norm = np.clip((db - lo) / np.maximum(1e-3, hi - lo), 0, 1) ** 1.5

    def smooth(x, up=0.6, down=0.16):
        """Fast attack, slow release envelope (per column for 2-D input)."""
        out = np.empty_like(x)
        acc = np.array(x[0], dtype=np.float32)
        for t in range(len(x)):
            rate = np.where(x[t] > acc, up, down)
            acc = acc + (x[t] - acc) * rate
            out[t] = acc
        return out

    bands = smooth(norm)
    bass = smooth(norm[:, :10].mean(axis=1), 0.55, 0.12)
    mid = smooth(norm[:, 10:42].mean(axis=1), 0.5, 0.12)
    high = smooth(norm[:, 42:].mean(axis=1), 0.5, 0.15)
    f = (flux - np.median(flux)) / max(1e-6, np.percentile(flux, 98) - np.median(flux))
    f = np.clip(f, 0, 1)
    onset = np.zeros_like(f)
    acc = 0.0
    for t in range(len(f)):
        acc = max(f[t] if f[t] > 0.45 else 0.0, acc * 0.82)
        onset[t] = acc
    rms = np.sqrt(np.mean(norm ** 2, axis=1))
    win = fps * 4
    energy = np.convolve(rms, np.ones(win) / win, mode="same")
    e_lo, e_hi = np.percentile(energy, 5), np.percentile(energy, 97)
    energy = np.clip((energy - e_lo) / max(1e-6, e_hi - e_lo), 0, 1)
    out = {"bands": bands.astype(np.float32), "bass": bass, "mid": mid, "high": high, "onset": onset,
           "flux": f.astype(np.float32), "energy": energy.astype(np.float32), "y": y.astype(np.float32),
           "sr": sr, "fps": fps}
    if cache:
        cache.parent.mkdir(parents=True, exist_ok=True)
        np.savez(cache, **{k: v for k, v in out.items() if isinstance(v, np.ndarray)}, sr=sr, fps=fps)
    return out


def beat_grid(feat: dict) -> dict:
    """Tempo, beats and bar starts (downbeats) from the onset envelope.

    Autocorrelation picks the beat period (60–190 BPM, mild preference around
    120); a grid phase is fitted to the onsets, then every beat is snapped to
    the strongest onset within ±1/6 period so the grid follows tempo drift.
    The bar phase (4/4) is the one whose beats carry the most bass.
    """
    fps = feat["fps"]
    flux = np.asarray(feat["flux"], dtype=np.float64)
    env = flux - np.convolve(flux, np.ones(fps) / fps, mode="same")
    env = np.maximum(env, 0)
    n = len(env)
    lo, hi = int(fps * 60 / 190), int(fps * 60 / 60)
    ac = np.array([np.dot(env[:-lag], env[lag:]) for lag in range(lo, hi + 1)])
    lags = np.arange(lo, hi + 1)
    bpm = 60.0 * fps / lags
    weight = np.exp(-0.5 * (np.log2(bpm / 120.0) / 0.9) ** 2)
    k = int(np.argmax(ac * weight))
    half = int(round(lags[k] / 2))
    if half >= lo and 60.0 * fps / lags[k] < 100 and ac[half - lo] >= 0.75 * ac[k]:
        k = half - lo  # strong peak at half the lag: the real beat is twice as fast
    period = float(lags[k])
    # refine period with sub-frame parabolic fit
    if 0 < k < len(ac) - 1:
        a, b, c = ac[k - 1], ac[k], ac[k + 1]
        den = a - 2 * b + c
        if den != 0:
            period = float(lags[k] + 0.5 * (a - c) / den)
    phases = np.arange(int(period))
    scores = [env[np.arange(p, n, period).astype(int)].sum() for p in phases]
    phase = float(phases[int(np.argmax(scores))])
    beats = []
    t = phase
    win = max(1, int(period / 6))
    while t < n:
        i = int(round(t))
        a, b = max(0, i - win), min(n, i + win + 1)
        j = a + int(np.argmax(env[a:b])) if b > a else i
        snapped = j if env[j] > env[i] * 1.2 else i
        beats.append(snapped)
        t = snapped + period
    beats_arr = np.array(beats)
    bass = np.asarray(feat["bass"])
    bar_scores = [bass[beats_arr[p::4]].mean() if len(beats_arr[p::4]) else 0 for p in range(4)]
    bar_phase = int(np.argmax(bar_scores))
    return {"bpm": round(60.0 * fps / period, 2), "beats": (beats_arr / fps).round(3).tolist(),
            "bars": (beats_arr[bar_phase::4] / fps).round(3).tolist(), "beat_period": period / fps}


def lyric_sections(store: JobStore) -> list[dict]:
    """Sung blocks from the timed lyrics (split at gaps > 3.5 s), each marked as a
    repeat of an earlier block when most of its lines were sung before."""
    import re as _re

    view = read_json(store.dir / "timing" / "timed.json")
    if not view:
        return []
    lines = [l for l in view["lines"] if l.get("start") is not None]
    blocks: list[dict] = []
    for l in lines:
        key = _re.sub(r"[\s\W_ー〜～]+", "", l["text"])
        if blocks and l["start"] - blocks[-1]["end"] <= 3.5:
            b = blocks[-1]
            b["end"] = max(b["end"], l["end"])
            b["keys"].append(key)
        else:
            blocks.append({"start": l["start"], "end": l["end"], "keys": [key]})
    seen: dict[str, int] = {}
    for bi, b in enumerate(blocks):
        src_votes: dict[int, int] = {}
        for key in b["keys"]:
            if key in seen:
                src_votes[seen[key]] = src_votes.get(seen[key], 0) + 1
        if src_votes:
            src, votes = max(src_votes.items(), key=lambda kv: kv[1])
            if votes >= max(2, int(len(b["keys"]) * 0.6)):
                b["repeat_of"] = src
        for key in b["keys"]:
            seen.setdefault(key, bi)
    # a repeated passage is the chorus — and so is the passage it repeats
    for b in blocks:
        if "repeat_of" in b:
            b["kind"] = "chorus"
            blocks[b["repeat_of"]]["kind"] = "chorus"
    return [{"start": round(b["start"], 2), "end": round(b["end"], 2), "lines": len(b["keys"]),
             "repeat_of": b.get("repeat_of"), "kind": b.get("kind", "verse")} for b in blocks]


def lyric_repeats(store: JobStore) -> list[dict]:
    """Runs of consecutive lines that repeat consecutive earlier lines (a chorus
    sung again): [{"start","end","src_start","src_end"}] in seconds."""
    import re as _re

    view = read_json(store.dir / "timing" / "timed.json")
    if not view:
        return []
    seen: dict[str, dict] = {}
    runs: list[dict] = []
    for l in view["lines"]:
        if l.get("start") is None:
            continue
        key = _re.sub(r"[\s\W_ー〜～]+", "", l["text"])
        src = seen.get(key)
        if src is None:
            seen[key] = l
            continue
        if runs and runs[-1]["last_i"] == l["i"] - 1 and runs[-1]["last_src"] == src["i"] - 1:
            runs[-1].update(end=l["end"], src_end=src["end"], last_i=l["i"], last_src=src["i"])
        else:
            runs.append({"start": l["start"], "end": l["end"], "src_start": src["start"], "src_end": src["end"],
                         "last_i": l["i"], "last_src": src["i"]})
    return [{k: round(v, 2) for k, v in r.items() if k in ("start", "end", "src_start", "src_end")} for r in runs]


def plan_montage(store: JobStore, feat: dict, layer: dict, duration: float) -> dict:
    """Beat-synced shot list for the ``montage`` layer.

    * cuts land on bar starts (downbeats); section starts force a cut;
    * shot length: ``bars`` per shot (halved in loud parts, doubled in quiet ones),
      lengthened automatically until the image pool is enough;
    * every image is used once — except that a section repeating an earlier one
      (same lyrics, e.g. the last chorus) reuses that section's images in order;
    * ``pins`` ([{"t": seconds, "asset": id}]) force an image onto a moment.
    """
    from . import mv_assets

    pool = mv_assets.load_pool(store)
    ids = [a["id"] for a in pool if a["id"] not in set(layer.get("exclude", []))]
    # ``images``: an explicit use order laid out by the agent (``mv-clips --list`` →
    # judge by the rules → write the id list). Covers clips too: the planner then plays
    # them in that order (length-fit and source-order guards still apply).
    manual_ids = layer.get("images") if isinstance(layer.get("images"), list) else None
    if manual_ids is not None:
        ids = [i for i in manual_ids if i in ids]
    origins = {a["id"]: mv_assets.origin_of(a) for a in pool}
    clips = {a["id"]: a for a in pool if a.get("kind") == "clip"}
    # one montage module for images and video clips: ``sources`` picks the ingredient
    # types (default both, user uploads and found material freely mixed)
    by_id = {a["id"]: a for a in pool}
    sources = set(layer.get("sources") or ("image", "clip"))
    if sources != {"image", "clip"}:
        ids = [i for i in ids if (i in clips) == ("clip" in sources)]
        clips = {i: c for i, c in clips.items() if "clip" in sources}
    if clips and not isinstance(layer.get("images"), list):
        ids = clip_order(ids, clips, set(layer.get("avoid", [])), layer.get("prefer_tags"))
    grid = beat_grid(feat)
    bars = [b for b in grid["bars"] if b < duration - 0.5]
    beats = [b for b in grid["beats"] if b < duration - 0.5]
    bar_len = 4 * grid["beat_period"]
    if len(bars) < 4:
        bars = list(np.arange(0.0, duration, 4.0))
        beats = list(np.arange(0.0, duration, 1.0))
        bar_len = 4.0
    trans = float(layer.get("trans", 0.5))
    sections = lyric_sections(store)
    repeats = lyric_repeats(store)
    # section-aware pacing: chorus (a repeated passage and its source) / verse /
    # instrumental (sung gaps > 3.5 s, incl. intro/outro). ``section_pace`` scales the
    # shot length per kind, ``section_motion`` steers clip motion per kind; {} disables.
    section_spans: list[tuple[float, float, str]] = []
    prev_end = 0.0
    for sec in sorted(sections, key=lambda x: x["start"]):
        if sec["start"] - prev_end > 3.5:
            section_spans.append((prev_end, sec["start"], "instrumental"))
        section_spans.append((sec["start"], sec["end"], sec.get("kind") or "verse"))
        prev_end = max(prev_end, sec["end"])
    if duration - prev_end > 3.5:
        section_spans.append((prev_end, duration, "instrumental"))
    # energy-based chorus upgrade: the hottest sung block is the song's high point
    # even when its lyrics never repeat verbatim (lyric repeats already marked theirs)
    verse_idx = [i for i, (_, _, k) in enumerate(section_spans) if k == "verse"]
    if verse_idx:
        fps0 = feat["fps"]
        en = np.asarray(feat["energy"])
        means = []
        for i in verse_idx:
            a, b, _ = section_spans[i]
            seg = en[int(a * fps0):int(b * fps0)]
            means.append(float(seg.mean()) if len(seg) else 0.0)
        threshold = sorted(means)[max(0, int(len(means) * 0.7) - (0 if len(means) > 1 else 1))]
        for i, m in zip(verse_idx, means):
            if m >= threshold and m > 0:
                a, b, _ = section_spans[i]
                section_spans[i] = (a, b, "chorus")
    # video clips carry more motion per second than stills: a clip montage runs one
    # notch slower (a calm interlude may hold a single long clip shot), while stills
    # compress the section multipliers so one image never lingers too long
    pace = ({"chorus": 0.75, "verse": 1.5, "instrumental": 3.0} if clips
            else {"chorus": 0.5, "verse": 1.0, "instrumental": 1.25})
    pace.update(layer.get("section_pace") or {})
    sec_motion = {"chorus": "high", "instrumental": "low"}
    sec_motion.update(layer.get("section_motion") or {})
    # per-section tag affinity (images and clips alike): e.g. {"chorus": ["battle"]}
    sec_tags = {k: set(v) for k, v in (layer.get("section_tags") or {}).items()}
    # lyrics-aware tag windows: [{"t0": seconds, "t1": seconds, "tags": [...]}] — the agent
    # reads the lyrics and tags the passages (a desert verse, a hopeful chorus) so shots whose
    # content tags fit the moment are preferred there
    lyric_tag_rules = [(float(t["t0"]), float(t["t1"]), set(t["tags"]))
                       for t in (layer.get("lyric_tags") or []) if t.get("tags")]
    clip_motions = sorted(float(c.get("motion") or 0.01) for c in clips.values())
    motion_hi = clip_motions[int(len(clip_motions) * 0.7)] if clip_motions else 0.02
    motion_lo = clip_motions[int(len(clip_motions) * 0.25)] if clip_motions else 0.005

    def section_of(t: float) -> str:
        for a, b, k in section_spans:
            if a <= t < b:
                return k
        return "verse"

    def want_tags_of(t: float, sec: str) -> set:
        w = set(sec_tags.get(sec) or set())
        for a, b, tags in lyric_tag_rules:
            if a <= t < b:
                w |= tags
        return w

    fps = feat["fps"]
    energy = np.asarray(feat["energy"])
    flux = np.asarray(feat["flux"])
    min_shot, max_shot = float(layer.get("min_shot", 1.5)), float(layer.get("max_shot", 10.0))
    # a pin whose asset left the pool (excluded / removed) is ignored instead of breaking the plan
    pins = sorted((p for p in layer.get("pins", []) if p.get("asset") in ids), key=lambda p: p["t"])

    def nearest_bar(t: float) -> float:
        return min(bars, key=lambda b: abs(b - t))

    forced = sorted({nearest_bar(s["start"] - 0.15) for s in list(sections) + list(repeats) if s["start"] > bar_len})

    margin = 1.6 * trans + 0.15  # the longest crossfade (quiet parts) keeps playing the clip past its cut

    def need_of(s: dict) -> float:
        return s["t1"] - s["t0"] + margin

    def clip_fit(s: dict) -> None:
        """In-point (and slow motion when a clip is short) for a clip shot."""
        c = clips.get(s.get("asset"))
        if c is None:
            return
        need = need_of(s)
        if c["duration"] >= need:
            s["in"], s["speed"] = round((c["duration"] - need) * 0.35, 2), 1.0
        else:
            s["in"], s["speed"] = 0.0, round(max(0.7, c["duration"] / need), 3)

    def long_enough(cid: str, need: float) -> bool:
        """A clip may be slowed to 0.7× at most — a shorter one would freeze on its last frame."""
        return cid not in clips or clips[cid]["duration"] >= 0.7 * need

    def dur_of(i: str) -> float:
        return clips[i]["duration"] if i in clips else 0.0

    def reuse(s: dict, counts: dict, avoid_id) -> str:
        """Fallback for a shot no unused clip can fill: the least-used long-enough
        clip (not the previous shot's); when none is long enough, rotate among the
        longest ones instead of giving every such shot the same clip."""
        ok = [i for i in ids if long_enough(i, need_of(s)) and i != avoid_id]
        if not ok:
            longest = sorted(ids, key=lambda i: -dur_of(i))
            ok = [i for i in longest[:max(3, len(ids) // 4)] if i != avoid_id] or longest[:1]
        return min(ok, key=lambda i: (counts.get(i, 0), -dur_of(i)))

    def take(unused: list[str], s: dict, last: dict) -> str | None:
        """Clips lead whenever one can fill the shot (video first); an image fills the
        gaps no clip can take, or jumps the queue when its tags fit this section.
        None = nothing fits (the shot then repeats a long-enough clip, see fallback)."""
        sec = section_of(s["t0"])
        need = need_of(s)
        usable = [cid for cid in unused if cid in clips and long_enough(cid, need)] if clips else []
        if not usable:
            # no clip can fill this shot — an image fills it (manual order when given)
            cands = [i for i in unused if i not in clips]
            if manual_ids is not None:
                rank = {cid: k for k, cid in enumerate(manual_ids)}
                cands.sort(key=lambda i: rank.get(i, len(manual_ids)))
            img = cands[0] if cands else None
            if img is not None:
                unused.remove(img)
                return img
            return None
        # a section-tagged image may still jump the queue when the words fit it
        want_tags = want_tags_of(s["t0"], sec)
        if want_tags and unused[0] not in clips:
            hit = next((i for i in unused[:6]
                        if i not in clips and set(by_id[i].get("tags") or []) & want_tags), None)
            if hit is not None:
                unused.remove(hit)
                return hit
        e = float(energy[min(len(energy) - 1, int(s["t0"] * fps))])
        m = sec_motion.get(sec)
        want = motion_hi if m == "high" else motion_lo if m == "low" else 0.008 + 0.05 * e
        usable = [cid for cid in unused if long_enough(cid, need)]
        if not usable:
            return None
        # clips from one source appear in source order: candidates that jump backwards
        # only enter the pool when nothing continues (or starts fresh) instead
        lin = last.get("in") or {}
        in_order = [cid for cid in usable
                    if cid not in clips
                    or (clips[cid].get("library_id") or "") not in lin
                    or float(clips[cid].get("t_in") or 0) >= lin.get(clips[cid].get("library_id"), 0.0)]
        if in_order:
            usable = in_order
        if manual_ids is not None:
            # the agent laid out the order by hand: the first still-usable entry wins
            rank = {cid: k for k, cid in enumerate(manual_ids)}
            pick = min(usable, key=lambda cid: rank.get(cid, len(manual_ids)))
            unused.remove(pick)
            return pick
        window = [cid for cid in unused[:14] if cid in usable] or usable[:14]
        best, best_q = window[0], -1e9
        for j, cid in enumerate(window):
            c = clips.get(cid)
            if c is None:
                continue
            fit = 1.0 if c["duration"] >= need else max(0.0, (c["duration"] / need - 0.55) / 0.45)
            match = math.exp(-abs(math.log(max(1e-4, float(c.get("motion") or 0.01)) / want)))
            # clips from one source should appear in source order — jumping back and forth
            # inside a video reads as a editing mistake (a strong tag/motion fit overrules it)
            src = c.get("library_id")
            prev_in = (last.get("in") or {}).get(src) if src else None
            order_term = (0.9 if prev_in is not None and float(c.get("t_in") or 0) >= prev_in
                          else -0.9 if prev_in is not None else 0.0)
            q = (1.3 * fit + 0.8 * match + 0.5 * clip_quality(c, layer.get("prefer_tags")) - 0.3 * j / 14
                 + (0.6 if set(c.get("tags") or []) & want_tags_of(s["t0"], sec) else 0.0)
                 + order_term
                 - (0.6 if c.get("library_id") and c.get("library_id") == last.get("src") else 0.0))
            if q > best_q:
                best, best_q = cid, q
        unused.remove(best)
        return best

    def build(scale: float, order: list[str]):
        cuts = [0.0]
        t = 0.0
        while t < duration - min_shot:
            e = float(energy[min(len(energy) - 1, int(t * fps))])
            n_bars = (layer.get("bars", 2) * scale * (0.5 if e > 0.72 else 2.0 if e < 0.28 else 1.0)
                      * pace.get(section_of(t), 1.0))
            fine = bool(layer.get("beat_cuts")) and n_bars < 1.0  # 卡点 on beats, not only bar starts
            # a calm instrumental passage in a clip montage may hold one long shot
            max_here = max_shot
            if clips and section_of(t) == "instrumental" and pace.get("instrumental", 1.0) >= 2.0:
                span_end = next((b for a, b, k in section_spans
                                 if k == "instrumental" and a <= t < b), t + max_shot)
                max_here = max(max_shot, min(span_end, t + 20.0))
            target = min(t + max(0.5 if fine else 1.0, n_bars) * bar_len, t + max_here)
            nxt = next((f for f in forced if t + min_shot <= f <= target + 0.01), None)
            if nxt is None:
                cands = [b for b in (beats if fine else bars) if t + min_shot <= b <= t + max_here]
                nxt = min(cands, key=lambda b: abs(b - target)) if cands else min(duration, t + max_here)
            if duration - nxt < min_shot:
                break
            cuts.append(round(float(nxt), 3))
            t = nxt
        cuts.append(duration)
        shots = [{"t0": a, "t1": b} for a, b in zip(cuts, cuts[1:]) if b - a > 0.05]
        unused = [i for i in order if i not in {p.get("asset") for p in pins}]
        last: dict = {}
        counts: dict[str, int] = {}
        prev_asset = None
        run_cursor: dict[int, int] = {}
        for s in shots:
            mid = (s["t0"] + s["t1"]) / 2
            pin = next((p for p in pins if s["t0"] <= p["t"] < s["t1"]), None)
            dur = max(0.05, s["t1"] - s["t0"])
            run = next((r for r in repeats
                        if (min(s["t1"], r["end"] + 0.6) - max(s["t0"], r["start"] - 0.6)) / dur >= 0.5), None)
            prev = None
            if run is not None:
                span = max(0.1, run["end"] - run["start"])
                rel = min(max(mid, run["start"]), run["end"]) - run["start"]
                mapped = run["src_start"] + rel * (run["src_end"] - run["src_start"]) / span
                prev = next((x for x in shots if x is not s and x.get("asset") and x["t0"] <= mapped < x["t1"]), None)
                if prev is not None:
                    # repeat shots walk the source passage in order: when the linear map lands
                    # on the shot the previous repeat shot in this run already used (the repeat's
                    # cuts need not align with the source's), take the next source shot instead —
                    # two consecutive shots replaying one clip from the same point look broken
                    idx = shots.index(prev)
                    last_i = run_cursor.get(id(run), -1)
                    if idx <= last_i < len(shots) - 1 \
                            and shots[last_i + 1].get("asset") \
                            and shots[last_i + 1]["t0"] < run["src_end"] - 0.05:
                        idx, prev = last_i + 1, shots[last_i + 1]
                    run_cursor[id(run)] = idx
            if pin and pin.get("asset"):
                s["asset"], s["why"] = pin["asset"], "pin"
                if pin["asset"] in unused:
                    unused.remove(pin["asset"])
            elif prev is not None and long_enough(prev["asset"], need_of(s)):
                s["asset"], s["why"] = prev["asset"], "repeat"
            else:  # (a repeat whose first-occurrence clip is too short for this longer shot takes a new one)
                s["asset"] = take(unused, s, last) if unused else None
                s["why"] = "new" if s["asset"] else "missing"
                if s["asset"] is None and unused:
                    # clips are left but none is long enough: reuse one now (so a later repeat of this
                    # passage finds it) instead of stretching the plan
                    s["asset"], s["why"], s["short"] = reuse(s, counts, prev_asset), "fallback", True
            clip_fit(s)
            if s.get("asset"):
                counts[s["asset"]] = counts.get(s["asset"], 0) + 1
                prev_asset = s["asset"]
            if s.get("asset") in clips:
                last["src"] = clips[s["asset"]].get("library_id")
                if last["src"]:
                    last.setdefault("in", {})[last["src"]] = float(clips[s["asset"]].get("t_in") or 0.0)
        missing = sum(1 for s in shots if s["why"] == "missing")
        return shots, missing

    scale = 1.0
    shots, missing = build(scale, ids)
    ideal = sum(1 for s in shots if s["why"] in ("new", "missing") or s.get("short"))  # unique images the pacing wants
    # a small pool: lengthen the shots (up to ``max_scale``), then repeat the least-used ones;
    # max_scale 1.0 keeps the designed 卡点 pacing and repeats instead of slowing clips down
    while missing and scale * 1.3 <= float(layer.get("max_scale", 4.0)) + 1e-6 and ids:
        scale *= 1.3
        shots, missing = build(scale, ids)
    n_new = sum(1 for s in shots if s["why"] in ("new", "missing") or s.get("short"))
    order = arrange_assets(ids, origins, n_new, layer.get("prefer"))
    if order != ids:
        shots, missing = build(scale, order)
    if missing and ids:  # pool still too small: reuse the least used ones
        counts: dict[str, int] = {}
        for s in shots:
            if s.get("asset"):
                counts[s["asset"]] = counts.get(s["asset"], 0) + 1
        prev_asset = None
        for s in shots:
            if s.get("asset") is None:
                s["asset"], s["why"] = reuse(s, counts, prev_asset), "fallback"
                counts[s["asset"]] = counts.get(s["asset"], 0) + 1
                clip_fit(s)
            prev_asset = s["asset"]
    for k, s in enumerate(shots):
        i0 = int(s["t0"] * fps)
        s["strength"] = round(float(flux[max(0, i0 - 1):i0 + 2].max()) if len(flux) else 0.0, 2)
        s["section_start"] = any(abs(s["t0"] - f) < 0.05 for f in forced)
        s["section_kind"] = section_of((s["t0"] + s["t1"]) / 2)
        s["energy"] = round(float(energy[min(len(energy) - 1, i0)]), 2)
    new = sum(1 for s in shots if s["why"] == "new")
    need_more = sum(1 for s in shots if s["why"] == "fallback")
    by_origin: dict[str, int] = {}
    for s in shots:
        if s["why"] in ("new", "pin") and s.get("asset"):
            by_origin[origins.get(s["asset"], "user")] = by_origin.get(origins.get(s["asset"], "user"), 0) + 1
    return {"bpm": grid["bpm"], "bars_per_shot": round(layer.get("bars", 2) * scale, 2), "shots": shots,
            "sections": sections, "repeats": repeats, "pool": len(ids), "unique_used": new, "ideal_unique": ideal,
            "by_origin": by_origin,
            "repeated_sections": sum(1 for s in shots if s["why"] == "repeat"),
            "fallback_repeats": need_more,
            "clips": len(clips),
            "slow_motion": sum(1 for s in shots if (s.get("speed") or 1.0) < 0.999),
            "note": (f"素材不足：仍有 {need_more} 个镜头重复使用素材，建议再补 {need_more} 个以上" if need_more
                     else "")}


def clip_order(ids: list[str], clips: dict[str, dict], avoid: set[str], prefer_tags=None) -> list[str]:
    """Clip pool order for the planner: best shots of every source first, sources
    taken in turn so one trailer does not fill the whole intro; clips another
    project already used (``avoid``) go last. Images keep their place in front."""
    images = [i for i in ids if i not in clips]
    by_src: dict[str, list[str]] = {}
    for i in ids:
        if i in clips and i not in avoid:
            by_src.setdefault(str(clips[i].get("library_id") or clips[i].get("src")), []).append(i)
    for lst in by_src.values():
        lst.sort(key=lambda i: -clip_quality(clips[i], prefer_tags))
    order: list[str] = []
    queues = sorted(by_src.values(), key=len, reverse=True)
    while any(queues):
        for q in queues:
            if q:
                order.append(q.pop(0))
    late = sorted((i for i in ids if i in clips and i in avoid), key=lambda i: -clip_quality(clips[i], prefer_tags))
    return images + order + late


def clip_quality(c: dict, prefer_tags=None) -> float:
    """Scan score, plus a bonus for shots the agent tagged ``best`` while reviewing the sheets
    and for shots with one of the layer's ``prefer_tags`` (e.g. concert scenes for an idol song)."""
    tags = set(c.get("tags") or [])
    return (float(c.get("score") or 0.5) + (0.25 if "best" in tags else 0.0)
            + (0.2 if prefer_tags and tags & set(prefer_tags) else 0.0))


def arrange_assets(ids: list[str], origins: dict[str, str], n: int, prefer=None) -> list[str]:
    """Order the pool for the planner (shots take images front to back).

    ``prefer`` = origin priority (default user → web → video): when the pool has
    more images than the plan needs, the user's own images are kept first.
    ``"even"`` takes every origin proportionally. The chosen images are then
    interleaved by origin so a mixed pool spreads over the whole song; within an
    origin the pool order (e.g. a numbered image pack) is kept.
    """
    groups: dict[str, list[str]] = {}
    for i in ids:
        groups.setdefault(origins.get(i, "user"), []).append(i)
    if len(groups) <= 1 or n <= 0:
        return list(ids)
    n = min(n, len(ids))
    if prefer == "even":
        quota = {o: round(n * len(g) / len(ids)) for o, g in groups.items()}
    else:
        rank = list(prefer) if isinstance(prefer, list) else ["user", "web", "video"]
        quota, left = {}, n
        for o in sorted(groups, key=lambda o: rank.index(o) if o in rank else len(rank)):
            quota[o] = min(left, len(groups[o]))
            left -= quota[o]
    while sum(quota.values()) < n:  # rounding leftovers
        o = max(groups, key=lambda o: len(groups[o]) - quota[o])
        quota[o] += 1
    keyed = []
    for rank_i, (o, g) in enumerate(groups.items()):
        m = quota.get(o, 0)
        keyed += [((j + 0.5) / m, rank_i, i) for j, i in enumerate(g[:m])]
    chosen = [i for *_, i in sorted(keyed)]
    picked = set(chosen)
    return chosen + [i for i in ids if i not in picked]


def key_moments(feat: dict, start: float, end: float) -> list[float]:
    """Intro, a calm verse moment and the loudest chorus moment (seconds)."""
    fps = feat["fps"]
    e = feat["energy"]
    a, b = int(start * fps), max(int(start * fps) + 1, min(len(e) - 1, int(end * fps)))
    seg = e[a:b]
    peak = a + int(np.argmax(seg))
    calm_zone = seg[: max(1, len(seg) // 2)]
    calm = a + int(np.argmin(np.abs(calm_zone - np.percentile(seg, 40))))
    intro = a + min(len(seg) - 1, int(4 * fps))
    return [round(intro / fps, 2), round(calm / fps, 2), round(peak / fps, 2)]


# --------------------------------------------------------------------- colours
def palette_from_image(path: Path | None) -> list[str]:
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QColor, QImage

    fallback = ["#FF5FA2", "#7C5CFF", "#35D0FF", "#FFD27D", "#FFFFFF"]
    if not path or not Path(path).is_file():
        return fallback
    img = QImage(str(path)).scaled(48, 48, Qt.AspectRatioMode.IgnoreAspectRatio)
    hues = []
    for yy in range(img.height()):
        for xx in range(img.width()):
            h, s, v, _ = QColor(img.pixel(xx, yy)).getHsvF()
            if s > 0.35 and v > 0.35 and h >= 0:
                hues.append(h)
    if len(hues) < 20:
        return fallback
    hist, edges = np.histogram(hues, bins=12, range=(0, 1))
    picked: list[float] = []
    for o in np.argsort(hist)[::-1]:
        h = (edges[o] + edges[o + 1]) / 2
        if all(min(abs(h - p), 1 - abs(h - p)) > 0.12 for p in picked):
            picked.append(h)
        if len(picked) == 3:
            break
    while len(picked) < 3:
        picked.append((picked[-1] + 0.33) % 1.0)
    cols = [QColor.fromHsvF(h, 0.68, 1.0).name() for h in picked]
    return cols + [QColor.fromHsvF(picked[0], 0.25, 1.0).name(), "#FFFFFF"]


class Colors:
    def __init__(self, palette: list[str]):
        self.palette = palette

    def __call__(self, value, alpha: float | None = None):
        from PyQt6.QtGui import QColor

        if isinstance(value, str) and value.startswith("$"):
            idx = int(value[1:])
            value = self.palette[idx % len(self.palette)]
        c = QColor(value) if not isinstance(value, QColor) else QColor(value)
        if isinstance(value, str) and len(value) == 9:  # #RRGGBBAA
            c = QColor(value[:7])
            c.setAlpha(int(value[7:9], 16))
        if alpha is not None:
            c.setAlphaF(max(0.0, min(1.0, alpha * c.alphaF())))
        return c


# --------------------------------------------------------------------- renderer
class MVRenderer:
    def __init__(self, spec: dict, w: int, h: int, feat: dict, *, cover: Path | None, title: str = "",
                 artist: str = "", duration: float | None = None):
        from PyQt6.QtGui import QImage

        self.spec, self.w, self.h, self.f = spec, w, h, feat
        self.title, self.artist = title, artist
        self.n = len(feat["bass"])
        self.duration = duration or self.n / feat["fps"]
        self.cover = None
        if cover and Path(cover).is_file():
            img = QImage(str(cover))
            self.cover = None if img.isNull() else img
        pal = spec.get("palette") or "auto"
        if pal == "auto":
            pal = palette_from_image(cover)
        self.C = Colors(list(pal))
        self.layers = [l for l in spec.get("layers", []) if not (l.get("optional") and self._needs_cover(l)
                                                                  and self.cover is None)]
        self.state: list[dict] = [{} for _ in self.layers]
        self.rng = np.random.default_rng(int(spec.get("seed", 7)))
        for i, layer in enumerate(self.layers):
            init = getattr(self, "_init_" + layer["type"], None)
            if init:
                init(layer, self.state[i])

    @staticmethod
    def _needs_cover(layer: dict) -> bool:
        return layer["type"] == "cover_bg" or (layer["type"] == "image" and layer.get("src", "cover") == "cover")

    # ----------------------------------------------------------------- helpers
    def react(self, layer: dict, i: int) -> float:
        key = layer.get("react")
        if not key:
            return 0.0
        arr = self.f.get(key)
        return float(arr[min(i, len(arr) - 1)]) * float(layer.get("k", 0.5)) if arr is not None else 0.0

    def X(self, v):
        return v * self.w

    def Y(self, v):
        return v * self.h

    def _soft_dot(self, color, size=64):
        from PyQt6.QtCore import QPointF
        from PyQt6.QtGui import QImage, QPainter, QRadialGradient

        key = ("dot", color.rgba(), size)
        cache = self.__dict__.setdefault("_sprites", {})
        if key not in cache:
            img = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
            img.fill(0)
            p = QPainter(img)
            g = QRadialGradient(QPointF(size / 2, size / 2), size / 2)
            c0 = self.C(color)
            g.setColorAt(0, c0)
            c1 = self.C(color)
            c1.setAlpha(int(c0.alpha() * 0.35))
            g.setColorAt(0.35, c1)
            c2 = self.C(color)
            c2.setAlpha(0)
            g.setColorAt(1, c2)
            p.fillRect(0, 0, size, size, g)
            p.end()
            cache[key] = img
        return cache[key]

    # ----------------------------------------------------------------- frame
    def frame(self, img, t: float) -> None:
        from PyQt6.QtGui import QPainter

        i = max(0, min(self.n - 1, int(round(t * self.f["fps"]))))
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        for layer, st in zip(self.layers, self.state):
            draw = getattr(self, "_draw_" + layer["type"], None)
            if draw is None:
                continue
            p.save()
            p.setOpacity(float(layer.get("opacity", 1.0)))
            draw(p, layer, st, t, i)
            p.restore()
        p.end()

    # ----------------------------------------------------------------- backgrounds
    def _init_gradient(self, layer, st):
        from PyQt6.QtGui import QImage, QLinearGradient, QPainter

        img = QImage(self.w, self.h, QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(0)
        p = QPainter(img)
        g = QLinearGradient(0, 0, 0, self.h)
        for pos, col in layer.get("stops", [[0, "$0"], [1, "$1"]]):
            g.setColorAt(float(pos), self.C(col))
        p.fillRect(0, 0, self.w, self.h, g)
        p.end()
        st["img"] = img

    def _draw_gradient(self, p, layer, st, t, i):
        p.drawImage(0, 0, st["img"])

    def _init_cover_bg(self, layer, st):
        from PyQt6.QtCore import Qt
        from PyQt6.QtGui import QColor, QImage, QPainter

        if self.cover is None:
            return
        small = self.cover.scaled(48, 27, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                  Qt.TransformationMode.SmoothTransformation)
        big = small.scaled(int(self.w * 1.12), int(self.h * 1.12), Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                           Qt.TransformationMode.SmoothTransformation)
        p = QPainter(big)
        p.fillRect(big.rect(), QColor(4, 4, 12, int(255 * float(layer.get("dim", 0.6)))))
        p.end()
        st["img"] = big

    def _draw_cover_bg(self, p, layer, st, t, i):
        img = st.get("img")
        if img is None:
            return
        u = t / max(1.0, self.duration)
        dx = (img.width() - self.w) * (0.5 + 0.5 * math.sin(u * math.pi * 1.3))
        dy = (img.height() - self.h) * (0.5 + 0.5 * math.cos(u * math.pi * 0.9))
        p.drawImage(int(-dx), int(-dy), img)

    def _init_nebula(self, layer, st):
        from PyQt6.QtCore import QPointF
        from PyQt6.QtGui import QImage, QPainter, QRadialGradient

        img = QImage(self.w // 2, self.h // 2, QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(0)
        p = QPainter(img)
        cols = layer.get("colors", ["$1", "$2"])
        for k in range(14):
            x, y = self.rng.uniform(0, img.width()), self.rng.uniform(0, img.height() * 0.8)
            r = self.rng.uniform(0.15, 0.4) * img.width()
            g = QRadialGradient(QPointF(x, y), r)
            c = self.C(cols[k % len(cols)], 0.22)
            g.setColorAt(0, c)
            c2 = self.C(cols[k % len(cols)], 0.0)
            g.setColorAt(1, c2)
            p.setBrush(g)
            p.setPen(self.C("#00000000"))
            p.drawEllipse(QPointF(x, y), r, r * 0.6)
        p.end()
        st["img"] = img

    def _draw_nebula(self, p, layer, st, t, i):
        from PyQt6.QtCore import QRectF

        p.setOpacity(0.7 + self.react(layer, i))
        s = 1.0 + 0.04 * math.sin(t * 0.05)
        p.translate(self.w / 2, self.h / 2)
        p.scale(s, s)
        p.rotate(t * 0.4)
        p.drawImage(QRectF(-self.w / 2 * 1.1, -self.h / 2 * 1.1, self.w * 1.1, self.h * 1.1), st["img"])

    # ----------------------------------------------------------------- sky
    def _init_stars(self, layer, st):
        n = int(layer.get("count", 200))
        st["x"] = self.rng.uniform(0, 1, n)
        st["y"] = self.rng.uniform(0, float(layer.get("y1", 0.62)), n) ** 1.3
        st["s"] = self.rng.uniform(0.6, 2.4, n) * self.h / 1080
        st["ph"] = self.rng.uniform(0, math.tau, n)
        st["sp"] = self.rng.uniform(0.6, 2.2, n)

    def _draw_stars(self, p, layer, st, t, i):
        from PyQt6.QtCore import QPointF, Qt

        r = self.react(layer, i)
        p.setPen(Qt.PenStyle.NoPen)
        tw = float(layer.get("twinkle", 0.6))
        alpha = 0.55 + 0.45 * np.sin(st["ph"] + t * st["sp"]) * tw
        for x, y, s, a in zip(st["x"], st["y"], st["s"], alpha):
            c = self.C(layer.get("color", "#FFFFFF"), max(0.05, min(1.0, a + r * 0.5)))
            p.setBrush(c)
            rr = s * (1 + r)
            p.drawEllipse(QPointF(x * self.w, y * self.h), rr, rr)

    def _draw_aurora(self, p, layer, st, t, i):
        from PyQt6.QtCore import QPointF
        from PyQt6.QtGui import QLinearGradient, QPainterPath, QPen

        r = self.react(layer, i)
        cols = layer.get("colors", ["$2", "$3"])
        base = self.Y(float(layer.get("y", 0.2)))
        amp = self.Y(float(layer.get("amp", 0.06))) * (1 + r)
        for k in range(4):
            path = QPainterPath()
            ph = t * (0.12 + 0.05 * k) + k * 1.7
            for j in range(0, 65):
                x = self.w * j / 64
                y = base + k * self.h * 0.03 + amp * math.sin(x / self.w * math.tau * (1.2 + 0.3 * k) + ph) \
                    + amp * 0.4 * math.sin(x / self.w * math.tau * 3.1 - ph * 1.3)
                (path.moveTo if j == 0 else path.lineTo)(QPointF(x, y))
            g = QLinearGradient(0, 0, self.w, 0)
            g.setColorAt(0, self.C(cols[k % len(cols)], 0.0))
            g.setColorAt(0.5, self.C(cols[k % len(cols)], 0.22 + 0.25 * r))
            g.setColorAt(1, self.C(cols[(k + 1) % len(cols)], 0.0))
            pen = QPen(g, self.h * (0.05 - 0.008 * k))
            p.setPen(pen)
            p.drawPath(path)

    def _draw_moon(self, p, layer, st, t, i):
        self._disc(p, layer, i, stripes=False)

    def _draw_sun(self, p, layer, st, t, i):
        self._disc(p, layer, i, stripes=bool(layer.get("stripes")))

    def _disc(self, p, layer, i, stripes: bool):
        from PyQt6.QtCore import QPointF, QRectF, Qt
        from PyQt6.QtGui import QLinearGradient, QRadialGradient

        cx, cy = self.X(float(layer.get("x", 0.5))), self.Y(float(layer.get("y", 0.3)))
        r = self.Y(float(layer.get("r", 0.08))) * (1 + self.react(layer, i) * 0.5)
        glow = float(layer.get("glow", 0.5))
        g = QRadialGradient(QPointF(cx, cy), r * 3.2)
        g.setColorAt(0, self.C(layer.get("color", "$4"), 0.55 * glow))
        g.setColorAt(1, self.C(layer.get("color", "$4"), 0.0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(g)
        p.drawEllipse(QPointF(cx, cy), r * 3.2, r * 3.2)
        if stripes:
            lg = QLinearGradient(0, cy - r, 0, cy + r)
            lg.setColorAt(0, self.C(layer.get("color", "$4")))
            lg.setColorAt(1, self.C(layer.get("color2", "$2")))
            p.setBrush(lg)
            p.drawEllipse(QPointF(cx, cy), r, r)
            p.setBrush(self.C(self.spec.get("stripe_color", "#0D0221")))
            for k in range(6):
                yy = cy + r * (0.1 + 0.15 * k)
                p.drawRect(QRectF(cx - r, yy, 2 * r, r * (0.02 + 0.012 * k)))
        else:
            p.setBrush(self.C(layer.get("color", "$4")))
            p.drawEllipse(QPointF(cx, cy), r, r)

    def _init_clouds(self, layer, st):
        n = int(layer.get("count", 5))
        st["c"] = [(self.rng.uniform(0, 1), self.rng.uniform(layer.get("y0", 0.08), layer.get("y1", 0.3)),
                    self.rng.uniform(0.12, 0.28), self.rng.uniform(0.004, 0.012)) for _ in range(n)]

    def _draw_clouds(self, p, layer, st, t, i):
        from PyQt6.QtCore import QRectF

        dot = self._soft_dot(self.C(layer.get("color", "#FFFFFF"), float(layer.get("alpha", 0.6))), 96)
        for x, y, size, sp in st["c"]:
            cx = ((x + t * sp) % 1.3 - 0.15) * self.w
            cy = y * self.h
            wpx = size * self.w
            for k in range(5):
                ox = (k - 2) * wpx * 0.22
                oy = -abs(k - 2) * wpx * 0.04
                rw = wpx * (0.5 - abs(k - 2) * 0.08)
                p.drawImage(QRectF(cx + ox - rw / 2, cy + oy - rw * 0.3, rw, rw * 0.6), dot)

    def _draw_light_rays(self, p, layer, st, t, i):
        from PyQt6.QtCore import QPointF
        from PyQt6.QtGui import QPainterPath, QRadialGradient

        cx, cy = self.X(float(layer.get("x", 0.5))), self.Y(float(layer.get("y", 0.2)))
        n = int(layer.get("count", 12))
        r = self.react(layer, i)
        length = self.h * 1.2
        g = QRadialGradient(QPointF(cx, cy), length)
        g.setColorAt(0, self.C(layer.get("color", "#FFFFFF"), 0.18 + 0.3 * r))
        g.setColorAt(1, self.C(layer.get("color", "#FFFFFF"), 0.0))
        p.setBrush(g)
        p.setPen(self.C("#00000000"))
        for k in range(n):
            a = k / n * math.tau + t * 0.05
            w = 0.05 + 0.03 * math.sin(k * 1.7)
            path = QPainterPath(QPointF(cx, cy))
            path.lineTo(QPointF(cx + math.cos(a - w) * length, cy + math.sin(a - w) * length))
            path.lineTo(QPointF(cx + math.cos(a + w) * length, cy + math.sin(a + w) * length))
            path.closeSubpath()
            p.drawPath(path)

    # ----------------------------------------------------------------- ground
    def _init_silhouette(self, layer, st):
        from PyQt6.QtCore import QPointF, QRectF
        from PyQt6.QtGui import QImage, QPainter, QPainterPath, QPen

        shape = layer.get("shape", "hills")
        base = self.Y(float(layer.get("y", 0.66)))
        img = QImage(self.w, self.h, QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(0)
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        col = self.C(layer.get("color", "#05060F"))
        if shape in ("hills", "mountains"):
            n_layers = int(layer.get("layers", 1 if shape == "hills" else 3))
            for k in range(n_layers):
                depth = (k + 1) / n_layers
                amp = self.h * (0.05 if shape == "hills" else 0.16) * (1.2 - 0.4 * depth)
                path = QPainterPath(QPointF(0, self.h))
                phases = self.rng.uniform(0, math.tau, 4)
                for j in range(0, 161):
                    x = self.w * j / 160
                    u = x / self.w
                    if shape == "hills":
                        yv = math.sin(u * math.tau * 0.8 + phases[0]) * 0.6 + math.sin(u * math.tau * 1.7 + phases[1]) * 0.4
                    else:
                        yv = (abs(math.sin(u * math.tau * 1.3 + phases[0])) * 0.7 + abs(math.sin(u * math.tau * 3.1 + phases[1])) * 0.3
                              + 0.15 * math.sin(u * math.tau * 9 + phases[2]))
                    y = base - (n_layers - k - 1) * self.h * 0.03 - amp * yv
                    path.lineTo(QPointF(x, y))
                path.lineTo(QPointF(self.w, self.h))
                path.closeSubpath()
                c = self.C(layer.get("color", "#05060F"))
                if n_layers > 1:
                    c = c.lighter(int(100 + 60 * (1 - depth)))
                p.fillPath(path, c)
                if layer.get("snow") and shape == "mountains" and k == 0:
                    p.save()
                    p.setClipPath(path)
                    p.fillRect(QRectF(0, 0, self.w, base - amp * 0.9), self.C("#EEF6FF", 0.75))
                    p.restore()
        elif shape == "city":
            x = 0.0
            win_col = self.C(layer.get("windows", "$3"), 0.85)
            st["windows"] = []
            while x < self.w:
                bw = self.rng.uniform(0.03, 0.07) * self.w
                bh = self.rng.uniform(0.05, 0.22) * self.h
                rect = QRectF(x, base - bh, bw - 2, bh + self.h)
                p.fillRect(rect, col)
                for wy in np.arange(base - bh + 8, base - 6, max(6.0, self.h * 0.014)):
                    for wx in np.arange(x + 5, x + bw - 8, max(6.0, self.w * 0.008)):
                        if self.rng.random() < 0.3 and len(st["windows"]) < 700:
                            st["windows"].append((wx, wy, self.rng.uniform(0, math.tau)))
                x += bw
            p.fillRect(QRectF(0, base, self.w, self.h - base), col)
            st["win_col"] = win_col
        elif shape == "tree":
            p.setPen(QPen(col, 1))
            p.setBrush(col)
            cx = self.X(float(layer.get("x", 0.2)))
            size = self.Y(float(layer.get("size", 0.6)))
            hill = QPainterPath(QPointF(cx - size * 1.2, base + 4))
            hill.quadTo(QPointF(cx, base - size * 0.12), QPointF(cx + size * 1.2, base + 4))
            hill.lineTo(QPointF(cx + size * 1.2, self.h))
            hill.lineTo(QPointF(cx - size * 1.2, self.h))
            p.fillPath(hill, col)
            p.fillRect(QRectF(0, base, self.w, self.h - base), col)
            st["leaf"] = size * 0.022
            self._branch(p, col, cx, base - size * 0.06, -90.0, size * 0.3, size * 0.07, 0, st["leaf"])
        elif shape == "waves":
            st["base"] = base
        p.end()
        st["img"] = img

    def _branch(self, p, col, x, y, ang, length, width, depth, leaf):
        """Recursive tree; branch tips end in foliage clusters."""
        from PyQt6.QtCore import QPointF, Qt
        from PyQt6.QtGui import QPen

        if depth > 8 or length < 2:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(col)
            for _ in range(3):
                ox, oy = self.rng.uniform(-1, 1) * leaf * 1.5, self.rng.uniform(-1, 0.6) * leaf
                p.drawEllipse(QPointF(x + ox, y + oy), leaf * self.rng.uniform(1.2, 2.2), leaf * self.rng.uniform(0.9, 1.5))
            return
        x2 = x + math.cos(math.radians(ang)) * length
        y2 = y + math.sin(math.radians(ang)) * length
        pen = QPen(col, max(1.0, width))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.drawLine(QPointF(x, y), QPointF(x2, y2))
        spread = 24 + self.rng.uniform(-8, 10) + depth * 1.5
        for d in (-spread, spread * self.rng.uniform(0.6, 1.1)):
            self._branch(p, col, x2, y2, ang + d + self.rng.uniform(-6, 6), length * self.rng.uniform(0.7, 0.82),
                         width * 0.66, depth + 1, leaf)

    def _draw_silhouette(self, p, layer, st, t, i):
        from PyQt6.QtCore import QPointF, QRectF
        from PyQt6.QtGui import QPainterPath

        if layer.get("shape") == "waves":
            base = st["base"]
            r = self.react(layer, i)
            col = self.C(layer.get("color", "$1"))
            for k in range(4):
                path = QPainterPath(QPointF(0, self.h))
                amp = self.h * (0.012 + 0.008 * k) * (1 + r)
                for j in range(0, 97):
                    x = self.w * j / 96
                    y = base + k * self.h * 0.03 + amp * math.sin(x / self.w * math.tau * (2 + k) + t * (0.8 + 0.3 * k))
                    path.lineTo(QPointF(x, y))
                path.lineTo(QPointF(self.w, self.h))
                p.fillPath(path, col.darker(100 + 25 * k))
            return
        p.drawImage(0, 0, st["img"])
        if layer.get("shape") == "city" and st.get("windows"):
            hi = float(self.f["high"][i])
            p.setPen(self.C("#00000000"))
            base_col = st["win_col"].name()
            ww, wh = max(2.0, self.w * 0.003), max(3.0, self.h * 0.005)
            for wx, wy, ph in st["windows"]:
                a = 0.35 + 0.65 * (0.5 + 0.5 * math.sin(ph + t * 0.7)) * (0.6 + 0.4 * hi)
                p.fillRect(QRectF(wx, wy, ww, wh), self.C(base_col, a))

    def _draw_grid(self, p, layer, st, t, i):
        from PyQt6.QtCore import QPointF
        from PyQt6.QtGui import QPen

        hz = self.Y(float(layer.get("horizon", 0.62)))
        col = layer.get("color", "$2")
        r = self.react(layer, i)
        pen = QPen(self.C(col, 0.55 + 0.4 * r), max(1.0, self.h * 0.0018))
        p.setPen(pen)
        vx = self.w / 2
        for k in range(-14, 15):
            p.drawLine(QPointF(vx + k * self.w * 0.02, hz), QPointF(vx + k * self.w * 0.16, self.h))
        speed = float(layer.get("speed", 0.5)) * (1 + r)
        off = (t * speed) % 1.0
        for k in range(12):
            u = ((k + off) / 12) ** 2.2
            y = hz + (self.h - hz) * u
            p.drawLine(QPointF(0, y), QPointF(self.w, y))

    # ----------------------------------------------------------------- key visual
    def _draw_image(self, p, layer, st, t, i):
        from PyQt6.QtCore import QPointF, QRectF, Qt
        from PyQt6.QtGui import QImage, QPainterPath, QPen

        src = self.cover
        if layer.get("src") not in (None, "cover"):
            if "img" not in st:
                st["img"] = QImage(str(layer["src"]))
            src = st["img"]
        if src is None or src.isNull():
            return
        cx, cy = self.X(float(layer.get("x", 0.5))), self.Y(float(layer.get("y", 0.4)))
        size = self.Y(float(layer.get("size", 0.3)))
        bass = float(self.f["bass"][i])
        pulse = 1.0 + 0.04 * bass
        u = t / max(1.0, self.duration)
        zoom = 1.0 + 0.08 * u  # slow Ken Burns
        if "scaled" not in st:
            st["scaled"] = src.scaled(int(size * 1.25), int(size * 1.25), Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                      Qt.TransformationMode.SmoothTransformation)
        im = st["scaled"]
        p.translate(cx, cy)
        p.scale(pulse, pulse)
        shape = layer.get("shape", "circle")
        clip = QPainterPath()
        if shape == "circle":
            p.rotate((t * float(layer.get("spin", 0))) % 360)
            clip.addEllipse(QPointF(0, 0), size / 2, size / 2)
        else:
            clip.addRoundedRect(QRectF(-size / 2, -size / 2, size, size), size * 0.06, size * 0.06)
            shadow = self._soft_dot(self.C("#000000", 0.7), 64)
            p.drawImage(QRectF(-size * 0.62, -size * 0.55, size * 1.24, size * 1.24), shadow)
        p.save()
        p.setClipPath(clip)
        scale = size * zoom * 1.02 / max(1, min(im.width(), im.height()))  # short side covers the shape
        dw, dh = im.width() * scale, im.height() * scale
        p.drawImage(QRectF(-dw / 2, -dh / 2, dw, dh), im)
        p.restore()
        ring = layer.get("ring")
        if ring:
            p.setPen(QPen(self.C(ring, 0.75), max(2.0, size * 0.012)))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(clip)

    # ----------------------------------------------------------------- visualizers
    def _colmix(self, cols: list, frac: float):
        from PyQt6.QtGui import QColor

        qs = [self.C(c) for c in cols] or [QColor("#FFFFFF")]
        if len(qs) == 1:
            return QColor(qs[0])
        pos = max(0.0, min(0.999, frac)) * (len(qs) - 1)
        a, b = qs[int(pos)], qs[int(pos) + 1]
        f = pos - int(pos)
        return QColor(int(a.red() + (b.red() - a.red()) * f), int(a.green() + (b.green() - a.green()) * f),
                      int(a.blue() + (b.blue() - a.blue()) * f))

    def _draw_spectrum(self, p, layer, st, t, i):
        from PyQt6.QtCore import QPointF, QRectF, Qt
        from PyQt6.QtGui import QLinearGradient, QPainterPath, QPen

        bands = self.f["bands"][i]
        n = int(layer.get("bands", 64))
        vals = np.interp(np.linspace(0, len(bands) - 1, n), np.arange(len(bands)), bands)
        style = layer.get("style", "bars")
        cols = layer.get("colors", ["$2", "$3"])
        alpha = float(layer.get("alpha", 0.85))
        height = self.Y(float(layer.get("height", 0.18)))
        if style == "ring":
            cx, cy = self.X(float(layer.get("x", 0.5))), self.Y(float(layer.get("y", 0.4)))
            base_r = self.Y(float(layer.get("radius", 0.15))) * (1 + 0.05 * float(self.f["bass"][i]))
            total = n * 2
            for pass_i, (wmul, a) in enumerate(((3.0, 0.25), (1.0, 1.0))):
                for k in range(total):
                    v = vals[k if k < n else total - 1 - k]
                    ang = -math.pi / 2 + (k + 0.5) / total * math.tau
                    c = self._colmix(cols, k / total)
                    c.setAlphaF(alpha * a)
                    pen = QPen(c, max(1.5, self.h * 0.0045 * wmul))
                    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                    p.setPen(pen)
                    r2 = base_r + 3 + v * height
                    p.drawLine(QPointF(cx + math.cos(ang) * base_r, cy + math.sin(ang) * base_r),
                               QPointF(cx + math.cos(ang) * r2, cy + math.sin(ang) * r2))
            return
        x0, x1 = self.X(float(layer.get("x0", 0.1))), self.X(float(layer.get("x1", 0.9)))
        y = self.Y(float(layer.get("y", 0.65)))
        if layer.get("mirror") or style == "mirror":
            vals = np.concatenate([vals[::-1], vals])
        m = len(vals)
        step = (x1 - x0) / m
        if style == "wave":
            for back, a in ((1, 0.35), (0, 1.0)):
                path = QPainterPath(QPointF(x0, y))
                for k, v in enumerate(vals):
                    xx = x0 + (k + 0.5) * step
                    yy = y - v * height * (1.15 if back else 1.0) - (self.h * 0.01 if back else 0)
                    path.lineTo(QPointF(xx, yy))
                path.lineTo(QPointF(x1, y))
                path.closeSubpath()
                g = QLinearGradient(0, y - height, 0, y)
                c0 = self._colmix(cols, 0.0)
                c0.setAlphaF(alpha * a)
                c1 = self._colmix(cols, 1.0)
                c1.setAlphaF(alpha * a * 0.4)
                g.setColorAt(0, c0)
                g.setColorAt(1, c1)
                p.fillPath(path, g)
            return
        bw = max(1.5, step * 0.62)
        for k, v in enumerate(vals):
            c = self._colmix(cols, k / max(1, m - 1))
            c.setAlphaF(alpha)
            xx = x0 + (k + 0.5) * step
            hh = 2 + v * height
            if style == "mirror":
                p.fillRect(QRectF(xx - bw / 2, y - hh / 2, bw, hh), c)
                continue
            p.fillRect(QRectF(xx - bw / 2, y - hh, bw, hh), c)
            if style == "horizon":
                c2 = self._colmix(cols, k / max(1, m - 1))
                c2.setAlphaF(alpha * 0.22)
                p.fillRect(QRectF(xx - bw / 2, y, bw, hh * 0.45), c2)

    def _draw_waveform(self, p, layer, st, t, i):
        from PyQt6.QtCore import QPointF
        from PyQt6.QtGui import QPainterPath, QPen

        y = self.f["y"]
        sr = self.f["sr"]
        c = int(t * sr)
        win = 2048
        seg = y[max(0, c - win // 2): c + win // 2]
        if len(seg) < 32:
            return
        pts = seg[:: max(1, len(seg) // 256)]
        base = self.Y(float(layer.get("y", 0.5)))
        amp = self.Y(float(layer.get("height", 0.1)))
        x0, x1 = self.X(float(layer.get("x0", 0.1))), self.X(float(layer.get("x1", 0.9)))
        path = QPainterPath()
        peak = max(1e-3, float(np.max(np.abs(pts))))
        scale = min(1.0, 0.6 / peak) if peak < 0.6 else 1.0
        for k, v in enumerate(pts):
            env = math.sin(math.pi * k / (len(pts) - 1))
            pt = QPointF(x0 + (x1 - x0) * k / (len(pts) - 1), base - float(v) * scale * amp * env)
            (path.moveTo if k == 0 else path.lineTo)(pt)
        for wmul, a in ((4.0, 0.2), (1.0, 1.0)):
            p.setPen(QPen(self.C(layer.get("color", "#FFFFFF"), float(layer.get("alpha", 0.8)) * a),
                          max(1.0, self.h * 0.002 * wmul)))
            p.drawPath(path)

    def _draw_pulse(self, p, layer, st, t, i):
        from PyQt6.QtCore import QPointF, Qt
        from PyQt6.QtGui import QRadialGradient

        v = self.react(layer, i)
        if v <= 0.01:
            return
        cx, cy = self.X(float(layer.get("x", 0.5))), self.Y(float(layer.get("y", 0.4)))
        r = self.Y(float(layer.get("r", 0.4)))
        g = QRadialGradient(QPointF(cx, cy), r)
        g.setColorAt(0, self.C(layer.get("color", "$0"), min(1.0, v)))
        g.setColorAt(1, self.C(layer.get("color", "$0"), 0.0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(g)
        p.drawEllipse(QPointF(cx, cy), r, r)

    def _draw_flash(self, p, layer, st, t, i):
        v = self.react(layer, i)
        if v > 0.01:
            p.fillRect(0, 0, self.w, self.h, self.C(layer.get("color", "#FFFFFF"), min(0.5, v)))

    # ----------------------------------------------------------------- particles
    def _init_particles(self, layer, st):
        n = int(layer.get("count", 60))
        st.update(x=self.rng.uniform(0, 1, n), y=self.rng.uniform(0, 1, n), s=self.rng.uniform(0.5, 1.5, n),
                  ph=self.rng.uniform(0, math.tau, n), sp=self.rng.uniform(0.6, 1.4, n))

    def _draw_particles(self, p, layer, st, t, i):
        from PyQt6.QtCore import QPointF, QRectF, Qt
        from PyQt6.QtGui import QPainterPath, QPen

        kind = layer.get("kind", "bokeh")
        r = self.react(layer, i)
        col = layer.get("color", "#FFFFFF")
        y0, y1 = float(layer.get("y0", 0.0)), float(layer.get("y1", 1.0))
        k_h = self.h / 1080
        xs, ys, ss, phs, sps = st["x"], st["y"], st["s"], st["ph"], st["sp"]
        if kind == "rain":
            pen = QPen(self.C(col, 0.35 + 0.3 * r), max(1.0, 1.2 * k_h))
            p.setPen(pen)
            for x, y, s, sp in zip(xs, ys, ss, sps):
                yy = ((y + t * 1.6 * sp) % 1.1 - 0.05) * self.h
                xx = ((x - t * 0.18 * sp) % 1.0) * self.w
                ln = 26 * s * k_h
                p.drawLine(QPointF(xx, yy), QPointF(xx - ln * 0.25, yy + ln))
            return
        if kind == "petals":
            p.setPen(Qt.PenStyle.NoPen)
            for x, y, s, ph, sp in zip(xs, ys, ss, phs, sps):
                yy = ((y + t * 0.06 * sp) % 1.1 - 0.05) * self.h
                xx = ((x - t * 0.035 * sp + 0.03 * math.sin(t * 0.8 + ph)) % 1.0) * self.w
                sz = 17 * s * k_h * (1 + 0.5 * r)
                p.save()
                p.translate(xx, yy)
                p.rotate((t * 60 * sp + ph * 57) % 360)
                p.scale(1.0, 0.55 + 0.45 * math.sin(t * 2 * sp + ph))
                c = self.C(col, 0.85)
                c = c.lighter(int(100 + 25 * math.sin(ph)))
                p.setBrush(c)
                path = QPainterPath(QPointF(0, -sz))
                path.cubicTo(QPointF(sz, -sz * 0.6), QPointF(sz * 0.7, sz * 0.8), QPointF(0, sz))
                path.cubicTo(QPointF(-sz * 0.7, sz * 0.8), QPointF(-sz, -sz * 0.6), QPointF(0, -sz))
                p.drawPath(path)
                p.restore()
            return
        if kind == "sparkles":
            p.setPen(Qt.PenStyle.NoPen)
            for x, y, s, ph, sp in zip(xs, ys, ss, phs, sps):
                a = max(0.0, math.sin(t * 2.2 * sp + ph)) ** 6 * (0.6 + r)
                if a < 0.03:
                    continue
                xx, yy = x * self.w, (y0 + (y1 - y0) * y) * self.h
                sz = 7 * s * k_h * (0.6 + a)
                p.setBrush(self.C(col, min(1.0, a)))
                path = QPainterPath(QPointF(xx, yy - sz))
                for ang in range(1, 8):
                    rr = sz if ang % 2 == 0 else sz * 0.22
                    a2 = -math.pi / 2 + ang * math.pi / 4
                    path.lineTo(QPointF(xx + math.cos(a2) * rr, yy + math.sin(a2) * rr))
                path.closeSubpath()
                p.drawPath(path)
            return
        # soft round particles: fireflies, snow, bokeh, embers, dust, bubbles
        cfg = {"fireflies": (0.012, -0.004, 9, True), "snow": (0.004, 0.05, 6, False),
               "bokeh": (0.004, -0.012, 46, False), "embers": (0.01, -0.09, 7, True),
               "dust": (0.006, -0.008, 4, True), "bubbles": (0.004, -0.04, 14, False)}
        wander, vy, base_size, flicker = cfg.get(kind, cfg["bokeh"])
        dot = self._soft_dot(self.C(col), 64)
        for x, y, s, ph, sp in zip(xs, ys, ss, phs, sps):
            yy = (y0 + (y1 - y0) * ((y + t * vy * sp) % 1.0)) * self.h
            xx = ((x + wander * 4 * math.sin(t * 0.3 * sp + ph)) % 1.0) * self.w
            a = 0.55 + 0.45 * math.sin(t * 1.6 * sp + ph) if flicker else 0.7
            a = max(0.0, min(1.0, a * (0.6 + r)))
            sz = base_size * s * k_h * (1 + r * 0.8)
            p.setOpacity(a * (0.5 if kind == "bokeh" else 1.0))
            p.drawImage(QRectF(xx - sz, yy - sz, sz * 2, sz * 2), dot)

    # ----------------------------------------------------------------- montage
    def _init_montage(self, layer, st):
        plan = layer.get("_plan") or {}
        st["shots"] = plan.get("shots", [])
        st["starts"] = [s["t0"] for s in st["shots"]]
        st["files"] = layer.get("_files", {})
        st["clips"] = layer.get("_clips", {})
        st["readers"] = {}
        st["cache"] = {}
        st["order"] = []

    def close(self) -> None:
        """Stop the clip decoders (video-clip montages)."""
        for st in self.state:
            for rd in (st.get("readers") or {}).values():
                rd.close()
            if st.get("readers"):
                st["readers"].clear()

    def _clip_frame(self, layer, st, k, t):
        """Frame of the clip that fills shot ``k`` at time ``t`` (sequential reads;
        a jump backwards reopens the decoder)."""
        from .mv_clips import ClipReader

        shot = st["shots"][k]
        clip = st["clips"][shot["asset"]]
        fps = float(layer.get("_fps") or 30)
        speed = float(shot.get("speed") or 1.0)
        j = max(0, int(round((t - shot["t0"]) * fps)))
        rd = st["readers"].get(k)
        if rd is None or j < rd.pos:
            if rd is not None:
                rd.close()
            start = float(clip["t_in"]) + float(shot.get("in") or 0.0) + j / fps * speed
            rd = ClipReader(clip["src"], start, self.w, self.h, fps, crop=clip.get("crop"), speed=speed, first_index=j,
                            end=float(clip["t_out"]))
            st["readers"][k] = rd
        for old in [x for x in st["readers"] if x < k - 1 or x > k + 1]:
            st["readers"].pop(old).close()
        return rd.frame(j)

    def _shot_image(self, layer, st, aid):
        """Frame-sized (×1.2 for Ken Burns headroom) version of an asset; images
        whose aspect differs a lot get a blurred cover fill behind a contained copy."""
        from PyQt6.QtCore import QRectF, Qt
        from PyQt6.QtGui import QColor, QImage, QPainter

        if aid in st["cache"]:
            return st["cache"][aid]
        path = st["files"].get(aid)
        src = QImage(str(path)) if path else QImage()
        W, H = int(self.w * 1.2), int(self.h * 1.2)
        out = QImage(W, H, QImage.Format.Format_RGB32)
        out.fill(QColor("#000000"))
        if not src.isNull():
            p = QPainter(out)
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            ratio = (src.width() / max(1, src.height())) / (self.w / self.h)
            if layer.get("fit") == "contain" or ratio < 0.8 or ratio > 1.35:
                blur = src.scaled(48, 27, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                  Qt.TransformationMode.SmoothTransformation).scaled(
                    W, H, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation)
                p.drawImage((W - blur.width()) // 2, (H - blur.height()) // 2, blur)
                p.fillRect(0, 0, W, H, QColor(0, 0, 0, 90))
                fitted = src.scaled(int(W * 0.92), int(H * 0.92), Qt.AspectRatioMode.KeepAspectRatio,
                                    Qt.TransformationMode.SmoothTransformation)
            else:
                fitted = src.scaled(W, H, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                    Qt.TransformationMode.SmoothTransformation)
            p.drawImage(QRectF((W - fitted.width()) / 2, (H - fitted.height()) / 2, fitted.width(), fitted.height()),
                        fitted)
            p.end()
        st["cache"][aid] = out
        st["order"].append(aid)
        while len(st["order"]) > 4:
            st["cache"].pop(st["order"].pop(0), None)
        return out

    def _kb(self, k: int, u: float) -> tuple[float, float, float]:
        """Deterministic Ken Burns for shot ``k`` at progress ``u``: (scale, dx, dy)."""
        r = np.random.default_rng(1000 + k)
        z0, z1 = (1.0, 1.12) if r.random() < 0.6 else (1.12, 1.0)
        ax, ay = r.uniform(-1, 1, 2)
        s = z0 + (z1 - z0) * u
        return s, ax * 0.035 * (u - 0.5), ay * 0.025 * (u - 0.5)

    def _draw_shot(self, p, layer, st, k, t, extra_scale=1.0, opacity=1.0):
        from PyQt6.QtCore import QRectF

        shot = st["shots"][k]
        if shot.get("asset") in st["clips"]:  # video clip: the frame already fills the picture
            img = self._clip_frame(layer, st, k, t)
            if img is None:
                return
            u = max(0.0, min(1.0, (t - shot["t0"]) / max(0.01, shot["t1"] - shot["t0"])))
            s = extra_scale * (1.0 + float(layer.get("clip_zoom", 0.0)) * u)
            w, h = self.w * s, self.h * s
            p.setOpacity(opacity)
            p.drawImage(QRectF((self.w - w) / 2, (self.h - h) / 2, w, h), img)
            return
        img = self._shot_image(layer, st, shot.get("asset"))
        u = (t - shot["t0"]) / max(0.01, shot["t1"] - shot["t0"])
        s, dx, dy = self._kb(k, max(0.0, min(1.0, u)))
        s *= extra_scale
        w, h = img.width() / 1.2 * s, img.height() / 1.2 * s
        p.setOpacity(opacity)
        p.drawImage(QRectF((self.w - w) / 2 + dx * self.w, (self.h - h) / 2 + dy * self.h, w, h), img)

    def _draw_montage(self, p, layer, st, t, i):
        import bisect

        shots = st["shots"]
        if not shots:
            return
        k = max(0, bisect.bisect_right(st["starts"], t) - 1)
        shot = shots[k]
        since = t - shot["t0"]
        # beat "punch" (zoom kick on onsets) is opt-in: on still images it reads as twitching
        punch = float(layer.get("punch", 0.0)) * float(self.f["onset"][i])
        mode = layer.get("transitions", "auto")
        dur = float(layer.get("trans", 0.5))
        if mode == "auto" and shot.get("asset") in st["clips"]:
            # video clips: hard cut on the beat (卡点); quiet parts / section starts ease in
            # (`cut_energy` raises the bar for hard cuts in gentle songs)
            gate = float(layer.get("cut_energy", 0.4))
            if shot.get("energy", 0.5) < gate or (shot.get("section_start") and shot.get("energy", 0.5) < gate + 0.2):
                mode = "crossfade"
                dur *= 1.6 if shot.get("energy", 0.5) < 0.25 else 1.0
            else:
                mode = "cut"
        elif mode == "auto":
            # smooth: every cut is a crossfade, longer at section starts and in quiet parts
            mode = "crossfade"
            if shot.get("section_start") or shot.get("energy", 0.5) < 0.3:
                dur *= 1.6
        elif mode == "beat":
            # energetic: snap zoom at section starts, white flash on strong hits, hard cuts
            if shot.get("section_start"):
                mode = "zoom"
            elif shot.get("energy", 0.5) < 0.3:
                mode = "crossfade"
            elif shot.get("strength", 0) > 0.55:
                mode = "flash"
            else:
                mode = "cut"
        if mode == "crossfade" and k > 0 and since < dur:
            self._draw_shot(p, layer, st, k - 1, t)
            self._draw_shot(p, layer, st, k, t, 1.0 + punch, opacity=since / dur)
        elif mode == "zoom" and since < 0.35:
            self._draw_shot(p, layer, st, k, t, 1.0 + 0.18 * (1 - since / 0.35) ** 2 + punch)
        else:
            self._draw_shot(p, layer, st, k, t, 1.0 + punch)
        p.setOpacity(1.0)
        if mode in ("flash", "zoom") and since < 0.25:
            p.fillRect(0, 0, self.w, self.h, self.C("#FFFFFF", 0.45 * (1 - since / 0.25) ** 2))
        if layer.get("dim"):
            p.fillRect(0, 0, self.w, self.h, self.C("#000000", float(layer["dim"])))

    # ----------------------------------------------------------------- finishing
    def _draw_lyrics_band(self, p, layer, st, t, i):
        from PyQt6.QtGui import QLinearGradient

        y0 = self.Y(float(layer.get("from", 0.64)))
        g = QLinearGradient(0, y0, 0, self.h)
        g.setColorAt(0, self.C("#000000", 0.0))
        g.setColorAt(0.35, self.C("#000000", float(layer.get("alpha", 0.5)) * 0.8))
        g.setColorAt(1, self.C("#000000", float(layer.get("alpha", 0.5))))
        p.fillRect(0, int(y0), self.w, self.h - int(y0), g)

    def _init_vignette(self, layer, st):
        from PyQt6.QtCore import QPointF
        from PyQt6.QtGui import QImage, QPainter, QRadialGradient

        img = QImage(self.w, self.h, QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(0)
        p = QPainter(img)
        g = QRadialGradient(QPointF(self.w / 2, self.h * 0.45), self.w * 0.72)
        g.setColorAt(0.55, self.C("#000000", 0.0))
        g.setColorAt(1, self.C("#000000", float(layer.get("strength", 0.5))))
        p.fillRect(0, 0, self.w, self.h, g)
        p.end()
        st["img"] = img

    def _draw_vignette(self, p, layer, st, t, i):
        p.drawImage(0, 0, st["img"])

    def _init_grain(self, layer, st):
        from PyQt6.QtGui import QImage

        imgs = []
        w2, h2 = self.w // 2, self.h // 2
        amount = float(layer.get("amount", 0.04))
        for _ in range(4):
            noise = (self.rng.random((h2, w2)) * 255).astype(np.uint8)
            a = np.full((h2, w2), int(255 * amount), dtype=np.uint8)
            rgba = np.dstack([noise, noise, noise, a]).copy()
            img = QImage(rgba.data, w2, h2, w2 * 4, QImage.Format.Format_RGBA8888).copy()
            imgs.append(img)
        st["imgs"] = imgs

    def _draw_grain(self, p, layer, st, t, i):
        from PyQt6.QtCore import QRectF

        p.drawImage(QRectF(0, 0, self.w, self.h), st["imgs"][i % 4])

    def _init_scanlines(self, layer, st):
        from PyQt6.QtGui import QImage, QPainter

        img = QImage(self.w, self.h, QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(0)
        p = QPainter(img)
        c = self.C("#000000", float(layer.get("alpha", 0.08)) * 4)
        for y in range(0, self.h, 4):
            p.fillRect(0, y, self.w, 1, c)
        p.end()
        st["img"] = img

    def _draw_letterbox(self, p, layer, st, t, i):
        hh = self.Y(float(layer.get("size", 0.09)))
        col = self.C(layer.get("color", "#000000"))
        p.fillRect(0, 0, self.w, int(hh), col)
        p.fillRect(0, int(self.h - hh), self.w, int(hh) + 1, col)

    def _draw_scanlines(self, p, layer, st, t, i):
        p.drawImage(0, 0, st["img"])

    def _draw_progress(self, p, layer, st, t, i):
        from PyQt6.QtCore import QRectF

        hh = max(3.0, self.h * 0.004)
        p.fillRect(QRectF(0, self.h - hh, self.w, hh), self.C("#FFFFFF", 0.15))
        p.fillRect(QRectF(0, self.h - hh, self.w * t / max(1.0, self.duration), hh), self.C(layer.get("color", "$0"), 0.9))

    def _draw_title(self, p, layer, st, t, i):
        from PyQt6.QtCore import QRectF, Qt
        from PyQt6.QtGui import QFont

        t0, t1 = layer.get("show", [0, 8])
        if not (t0 <= t <= t1):
            return
        a = min(1.0, (t - t0) / 0.8, (t1 - t) / 0.8)
        f = QFont(layer.get("font", "Yu Gothic UI"))
        f.setPixelSize(int(self.h * float(layer.get("size", 0.045))))
        f.setWeight(QFont.Weight.Bold)
        p.setFont(f)
        p.setPen(self.C(layer.get("color", "#FFFFFF"), a))
        y = self.Y(float(layer.get("y", 0.5)))
        p.drawText(QRectF(0, y, self.w, self.h * 0.08), Qt.AlignmentFlag.AlignHCenter,
                   str(layer.get("text", "{title}")).replace("{title}", self.title).replace("{artist}", self.artist))
        f.setPixelSize(int(self.h * float(layer.get("size", 0.045)) * 0.6))
        f.setWeight(QFont.Weight.Normal)
        p.setFont(f)
        p.setPen(self.C(layer.get("color", "#FFFFFF"), a * 0.75))
        p.drawText(QRectF(0, y + self.h * 0.065, self.w, self.h * 0.06), Qt.AlignmentFlag.AlignHCenter,
                   str(layer.get("sub", "{artist}")).replace("{title}", self.title).replace("{artist}", self.artist))


# --------------------------------------------------------------------- job glue
def design_kind(st: dict) -> str:
    """Which design the job's background uses: ``montage`` or ``mv`` (AMV)."""
    bg = ((st.get("options") or {}).get("background") or {}).get("type")
    return "montage" if bg == "montage" else "mv"


def spec_path(store: JobStore, kind: str = "mv") -> Path:
    return store.path("render", "montage_spec.json" if kind == "montage" else "mv_spec.json")


def load_spec(store: JobStore, kind: str = "mv") -> dict:
    spec = read_json(spec_path(store, kind))
    return spec or preset_spec(DEFAULT_PRESETS[kind])


def save_spec(store: JobStore, spec: dict, kind: str | None = None) -> str:
    """Write a design; the kind follows the spec (a montage layer = 图片混剪)."""
    kind = kind or ("montage" if montage_layer(spec) else "mv")
    write_json(spec_path(store, kind), spec)
    return kind


def _cover(store: JobStore, st: dict) -> Path | None:
    for rel in ((st.get("song") or {}).get("cover"), st.get("media", {}).get("cover")):
        if rel:
            p = Path(rel)
            p = p if p.is_absolute() else store.abs(rel)
            if p.is_file():
                return p
    return None


def _output_wh(st: dict) -> tuple[int, int]:
    res = str((st.get("options") or {}).get("resolution") or "1920x1080")
    w, h = (int(x) for x in res.lower().split("x"))
    return w, h


def _output_fps(st: dict, spec: dict) -> int:
    """The spec may pin a frame rate; otherwise follow the karaoke output (30 / 60)."""
    if spec.get("fps"):
        return int(spec["fps"])
    fps = int((st.get("options") or {}).get("fps") or 60)
    return fps if fps in (30, 60) else 60


def publish_montage_plan(store: JobStore, plan: dict | None) -> None:
    """Write the cut plan next to the render and a compact copy into the job
    (the page draws it as a filmstrip).  ``None`` = the design has no montage."""
    if plan is None:
        store.update(lambda s: s.setdefault("previews", {}).update(montage=None))
        return
    write_json(store.path("render", "montage_plan.json"), plan)
    summary = {k: plan.get(k) for k in ("bpm", "bars_per_shot", "pool", "unique_used", "ideal_unique",
                                        "repeated_sections", "fallback_repeats", "note", "by_origin")}
    summary["shots"] = [{"t0": round(s["t0"], 3), "t1": round(s["t1"], 3), "asset": s.get("asset"), "why": s.get("why")}
                        for s in plan["shots"]]
    summary["repeats"] = [{k: round(r[k], 2) for k in ("start", "end", "src_start", "src_end")}
                          for r in plan.get("repeats") or []]
    summary["needed"] = sum(1 for s in plan["shots"] if s.get("why") != "repeat")
    store.update(lambda s: s.setdefault("previews", {}).update(montage=summary))


def montage_layer(spec: dict) -> dict | None:
    return next((l for l in spec.get("layers", []) if l.get("type") == "montage"), None)


def refresh_montage_plan(store: JobStore) -> dict | None:
    st = store.load()
    layer = montage_layer(load_spec(store, "montage"))
    if layer is None:
        publish_montage_plan(store, None)
        return None
    plan = plan_montage(store, job_features(store, st), layer, float(st["media"]["source"]["duration"]))
    publish_montage_plan(store, plan)
    return plan


def make_renderer(store: JobStore, st: dict, spec: dict, w: int, h: int, feat: dict,
                  publish: bool = True, fps: int = FPS) -> MVRenderer:
    song = st.get("song") or {}
    duration = float(st["media"]["source"]["duration"])
    spec = copy.deepcopy(spec)
    for layer in spec.get("layers", []):
        if layer.get("type") == "montage":
            from . import mv_assets

            plan = plan_montage(store, feat, layer, duration)
            pool_d = mv_assets.pool_dir(store)
            pool = mv_assets.load_pool(store)
            layer["_plan"] = plan
            layer["_files"] = {a["id"]: str(pool_d / a["file"]) for a in pool if a.get("file")}
            layer["_clips"] = {a["id"]: a for a in pool if a.get("kind") == "clip"}
            layer["_fps"] = fps
            if publish:  # gallery thumbnails of other presets must not replace the project's plan
                publish_montage_plan(store, plan)
    return MVRenderer(spec, w, h, feat, cover=_cover(store, st), title=song.get("title") or "",
                      artist=song.get("artist") or "", duration=duration)


def job_features(store: JobStore, st: dict) -> dict:
    audio = store.abs(st["media"]["audio"])
    return features(audio, cache=store.path("render", "mv_features.npz"))


def _pool_empty(store: JobStore) -> bool:
    from . import mv_assets

    return not mv_assets.load_pool(store)


def render_stills(store: JobStore, *, kind: str | None = None, spec: dict | None = None,
                  times: list[float] | None = None, width: int = 1280) -> list[str]:
    """Three key moments (intro / verse / loudest chorus) of one design."""
    from .render import qt_app

    qt_app()
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QImage

    st = store.load()
    kind = kind or design_kind(st)
    spec = spec or load_spec(store, kind)
    meta = {"preset": spec.get("preset"), "name": spec.get("name"), "theme": spec.get("theme"),
            "notes": spec.get("notes"), "palette": spec.get("palette")}
    if montage_layer(spec) and _pool_empty(store):
        store.update(lambda s: s.setdefault("previews", {}).setdefault("designs", {}).update(
            {kind: {**meta, "stills": [], "need_assets": True}}))
        return []
    feat = job_features(store, st)
    seg = st.get("segment") or {}
    dur = float(st["media"]["source"]["duration"])
    times = times or key_moments(feat, float(seg.get("start", 0)), float(seg.get("end", dur)))
    w, h = width, int(width * 9 / 16)
    r = make_renderer(store, st, spec, w, h, feat)
    out = []
    rev = int(time.time())
    try:
        for k, t in enumerate(times):
            img = QImage(w, h, QImage.Format.Format_RGB32)
            img.fill(0)
            r.frame(img, t)
            path = store.path("previews", f"{kind}_still_{k}.jpg")
            img.save(str(path), quality=88)
            out.append(f"previews/{kind}_still_{k}.jpg?v={rev}")
    finally:
        r.close()
    still_full = store.path("render", f"{kind}_still.jpg")  # karaoke previews use this as background
    QImage(str(store.dir / out[-1].split("?")[0])).scaled(*_output_wh(st), Qt.AspectRatioMode.IgnoreAspectRatio,
                                                         Qt.TransformationMode.SmoothTransformation).save(str(still_full), quality=90)

    def apply(s: dict) -> None:
        s.setdefault("previews", {}).setdefault("designs", {})[kind] = {**meta, "stills": out, "times": times}
        s["media"].setdefault("design_still", {})[kind] = store.rel(still_full)

    store.update(apply)
    return out


def render_gallery(store: JobStore, kind: str | None = None) -> dict:
    """One still per preset of a design kind at the chorus moment (stage-1 gallery)."""
    from .render import qt_app

    qt_app()
    from PyQt6.QtGui import QImage

    st = store.load()
    kind = kind or design_kind(st)
    feat = job_features(store, st)
    seg = st.get("segment") or {}
    dur = float(st["media"]["source"]["duration"])
    t = key_moments(feat, float(seg.get("start", 0)), float(seg.get("end", dur)))[-1]
    out = {}
    rev = int(time.time())
    for item in catalog(kind):
        pid = item["id"]
        r = make_renderer(store, st, preset_spec(pid), 640, 360, feat, publish=False)
        img = QImage(640, 360, QImage.Format.Format_RGB32)
        img.fill(0)
        r.frame(img, t)
        r.close()
        img.save(str(store.path("previews", f"preset_{pid}.jpg")), quality=85)
        out[pid] = f"previews/preset_{pid}.jpg?v={rev}"
    store.update(lambda s: s.setdefault("previews", {}).setdefault("design_gallery", {}).update({kind: out})
                 or s["previews"].update(mv_catalog=catalog()))
    return out


def design_sig(store: JobStore, st: dict, kind: str, spec: dict | None = None) -> str:
    """Everything the rendered background depends on (design, size, frame rate,
    audio, image pool and timed lyrics for montages)."""
    import hashlib

    spec = spec if spec is not None else load_spec(store, kind)
    w, h = _output_wh(st)
    parts = [json.dumps(spec, sort_keys=True, ensure_ascii=False), f"{w}x{h}@{_output_fps(st, spec)}",
             str(st["media"].get("audio")), f"planner:{PLANNER_VERSION}"]
    if montage_layer(spec):
        from . import mv_assets

        parts.append(",".join(a["id"] for a in mv_assets.load_pool(store)))
        view = read_json(store.dir / "timing" / "timed.json") or {}
        # the cut plan follows the lyric times / repeats (compared without spaces and punctuation,
        # like lyric_repeats), not singer colours, styling or a spelling fix
        import re as _re

        parts.append(json.dumps([(round(l.get("start") or 0, 2), round(l.get("end") or 0, 2),
                                  _re.sub(r"[\s\W_ー〜～]+", "", l.get("text") or "").lower())
                                 for l in view.get("lines") or []], ensure_ascii=False))
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]


def design_video(store: JobStore, st: dict, kind: str) -> Path | None:
    """The rendered background video of ``kind`` if it matches the current design."""
    rec = ((st.get("media") or {}).get("designs") or {}).get(kind) or {}
    if not rec.get("video") or not store.abs(rec["video"]).is_file():
        return None
    return store.abs(rec["video"]) if rec.get("sig") == design_sig(store, st, kind) else None


def render_video(store: JobStore, *, kind: str | None = None, seconds: float | None = None,
                 out: Path | None = None) -> Path:
    """Full background video of one design with the 原唱 audio muxed in (single process)."""
    from .render import qt_app

    qt_app()
    from PyQt6.QtGui import QImage

    st = store.load()
    kind = kind or design_kind(st)
    spec = load_spec(store, kind)
    if montage_layer(spec) and _pool_empty(store):
        raise SystemExit("混剪的素材池是空的：请先上传图包、导入视频片段（mv-clips）或让 Agent 搜集素材")
    sig = design_sig(store, st, kind, spec)
    feat = job_features(store, st)
    w, h = _output_wh(st)
    fps = _output_fps(st, spec)
    r = make_renderer(store, st, spec, w, h, feat, fps=fps)
    total = int(r.duration * fps) if seconds is None else min(int(r.duration * fps), int(seconds * fps))
    out = out or store.path("render", f"{kind}.mp4" if seconds is None else f"{kind}_clip.mp4")
    from .analysis import timing_audio

    audio = timing_audio(store, st)  # Hi-Res / original audio when available
    cmd = [ffmpeg_exe(), "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "bgr0", "-s", f"{w}x{h}", "-r", str(fps),
           "-i", "-", "-i", str(audio), "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "veryfast",
           "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "320k", "-shortest", "-movflags", "+faststart",
           str(out)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE, **hidden_subprocess_kwargs())
    img = QImage(w, h, QImage.Format.Format_RGB32)
    t0 = time.time()
    last = 0.0
    label = KIND_LABEL[kind]
    try:
        for k in range(total):
            img.fill(0)
            r.frame(img, k / fps)
            ptr = img.constBits()
            ptr.setsize(img.sizeInBytes())
            proc.stdin.write(bytes(ptr))
            now = time.time()
            if now - last > 1.0:
                last = now
                frac = (k + 1) / total
                eta = (now - t0) / frac - (now - t0)
                store.update(lambda s: s["media"].update(design_progress=round(frac, 3)))
                store.set_status(f"正在渲染{label}背景 {frac:.0%}（剩余约 {eta:.0f} 秒）", "working")
        proc.stdin.close()
        err = proc.stderr.read().decode("utf-8", "replace")
        if proc.wait() != 0:
            raise RuntimeError(f"{label}视频编码失败：" + err[-500:])
    except Exception:
        proc.kill()
        raise
    finally:
        r.close()
    if seconds is None:
        store.update(lambda s: s["media"].setdefault("designs", {}).update({kind: {"video": store.rel(out), "sig": sig,
                                                                                     "fps": fps}})
                     or s["media"].update(design_progress=1.0))
    store.log(f"{label}背景已渲染：{out.name}（{total} 帧 · {fps}fps，{time.time() - t0:.0f} 秒）")
    store.set_status(f"{label}背景已渲染（{time.time() - t0:.0f} 秒）", None)
    return out
