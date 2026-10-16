"""ffmpeg based media helpers: probe, audio extraction, browser proxies, peaks."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import numpy as np

from .paths import ffmpeg_exe, hidden_subprocess_kwargs

AUDIO_EXTS = {".wav", ".flac", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wma", ".ape", ".tta", ".alac", ".aiff", ".aif"}
VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".flv", ".webm", ".m4v", ".ts", ".wmv", ".mpg", ".mpeg"}


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          **hidden_subprocess_kwargs(), **kw)


def probe(path: str | Path) -> dict:
    cmd = [ffmpeg_exe("ffprobe"), "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)]
    res = run(cmd)
    if res.returncode != 0:
        raise RuntimeError(f"ffprobe 失败：{res.stderr.strip()}")
    data = json.loads(res.stdout)
    fmt = data.get("format", {})
    info: dict = {
        "path": str(path),
        "duration": float(fmt.get("duration") or 0.0),
        "format": fmt.get("format_name"),
        "tags": fmt.get("tags", {}),
        "has_video": False,
        "has_audio": False,
    }
    for s in data.get("streams", []):
        if s.get("codec_type") == "video" and not info["has_video"]:
            if (s.get("disposition") or {}).get("attached_pic"):
                info["cover_stream"] = s.get("index")
                continue
            info["has_video"] = True
            info["video_codec"] = s.get("codec_name")
            info["width"] = int(s.get("width") or 0)
            info["height"] = int(s.get("height") or 0)
            num, _, den = (s.get("r_frame_rate") or "0/1").partition("/")
            try:
                info["fps"] = round(float(num) / float(den or 1), 3)
            except ZeroDivisionError:
                info["fps"] = 0.0
        elif s.get("codec_type") == "audio" and not info["has_audio"]:
            info["has_audio"] = True
            info["audio_codec"] = s.get("codec_name")
            info["sample_rate"] = int(s.get("sample_rate") or 0)
            info["channels"] = int(s.get("channels") or 0)
    return info


FPS_OPTIONS = (24, 25, 30, 50, 60, 120)
"""Common output frame rates offered in the UI."""

SOURCE_FPS_ALIASES = {"source", "auto", "material", "素材", "跟随素材", "维持素材"}
"""``options.fps`` values meaning "follow the source video's own frame rate"."""

_NTSC_SNAP = ((23.976, 24), (29.97, 30), (59.94, 60), (119.88, 120))


def normalize_fps(value: object) -> int:
    """Frame rate as a positive int; 23.976 → 24, 29.97 → 30 …; 0 when unusable."""
    try:
        fps = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
    if fps <= 0:
        return 0
    for rate, snapped in _NTSC_SNAP:
        if abs(fps - rate) < 0.02:
            return snapped
    return max(1, int(round(fps)))


def source_fps(st: dict) -> int:
    """Frame rate of the job's source video (0 for audio-only / unknown)."""
    src = (st.get("media") or {}).get("source") or {}
    return normalize_fps(src.get("fps"))


def resolve_fps(st: dict, value: object = None, default: int = 60) -> int:
    """Output frame rate for a job.

    ``value`` (default: ``options.fps``) may be a number, ``"source"`` /
    ``"跟随素材"`` to echo the source video, or empty — anything unusable falls
    back to ``default``.
    """
    if value is None:
        value = (st.get("options") or {}).get("fps")
    if isinstance(value, str) and value.strip().lower() in SOURCE_FPS_ALIASES:
        return source_fps(st) or default
    return normalize_fps(value) or default


def extract_audio(src: str | Path, dst: str | Path, *, sample_rate: int = 44100, channels: int = 2,
                  start: float | None = None, end: float | None = None) -> Path:
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [ffmpeg_exe(), "-y", "-v", "error"]
    if start is not None:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(src)]
    if end is not None:
        cmd += ["-t", f"{max(0.01, end - (start or 0.0)):.3f}"]
    cmd += ["-vn", "-ac", str(channels), "-ar", str(sample_rate), "-c:a", "pcm_s16le", str(dst)]
    res = run(cmd)
    if res.returncode != 0:
        raise RuntimeError(f"提取音频失败：{res.stderr.strip()}")
    return dst


def extract_cover(src: str | Path, dst: str | Path) -> Path | None:
    res = run([ffmpeg_exe(), "-y", "-v", "error", "-i", str(src), "-an", "-map", "0:v:0", "-frames:v", "1", str(dst)])
    return Path(dst) if res.returncode == 0 and Path(dst).exists() else None


def thumbnail(src: str | Path, dst: str | Path, t: float, width: int = 640) -> Path:
    res = run([ffmpeg_exe(), "-y", "-v", "error", "-ss", f"{t:.2f}", "-i", str(src), "-frames:v", "1",
               "-vf", f"scale={width}:-2", "-q:v", "3", str(dst)])
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip())
    return Path(dst)


def browser_proxy(src: str | Path, dst: str | Path, info: dict) -> Path:
    """Return a file the browser can play: the source itself when it is
    already H.264/AAC MP4, otherwise a 720p proxy transcode."""
    src = Path(src)
    if (src.suffix.lower() in (".mp4", ".m4v") and info.get("video_codec") == "h264"
            and info.get("audio_codec") in ("aac", "mp3", None)):
        return src
    dst = Path(dst)
    cmd = [ffmpeg_exe(), "-y", "-v", "error", "-i", str(src), "-vf", "scale=-2:'min(720,ih)'",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "24", "-c:a", "aac", "-b:a", "160k",
           "-movflags", "+faststart", str(dst)]
    res = run(cmd)
    if res.returncode != 0:
        raise RuntimeError(f"生成浏览器预览视频失败：{res.stderr.strip()}")
    return dst


def load_mono(path: str | Path, sr: int = 16000) -> np.ndarray:
    """Decode any media to mono float32 at ``sr`` via ffmpeg."""
    cmd = [ffmpeg_exe(), "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"]
    proc = subprocess.run(cmd, capture_output=True, **hidden_subprocess_kwargs())
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", "replace"))
    return np.frombuffer(proc.stdout, dtype=np.float32).copy()


def peaks(path: str | Path, per_second: int = 50) -> dict:
    """Waveform overview (max abs per bucket, 0..1) for the web timeline."""
    sr = 8000
    mono = load_mono(path, sr)
    hop = max(1, sr // per_second)
    n = len(mono) // hop
    if n == 0:
        return {"per_second": per_second, "duration": 0.0, "peaks": []}
    frames = np.abs(mono[: n * hop]).reshape(n, hop).max(axis=1)
    top = float(np.percentile(frames, 99.5)) or 1.0
    norm = np.clip(frames / top, 0, 1)
    return {"per_second": per_second, "duration": len(mono) / sr,
            "peaks": [round(float(v), 3) for v in norm]}


def rms_envelope(path: str | Path, hop_s: float = 0.02) -> tuple[np.ndarray, float]:
    """10ms/20ms 人声包络（带磁盘缓存：源文件没变就直接读 .rms.npz）。

    每次时间微调质检都要它；392 秒的 wav 现算约 0.7 秒，缓存后降到毫秒级。
    """
    path = Path(path)
    cache = path.with_name(path.name + f".rms{int(round(hop_s * 1000))}.npz")
    key = None
    try:
        st = path.stat()
        key = f"{st.st_mtime_ns}:{st.st_size}:{hop_s}"
    except OSError:
        pass
    if key and cache.is_file():
        try:
            z = np.load(cache, allow_pickle=False)
            if str(z["key"]) == key:
                return z["rms"], float(z["hop"])
        except Exception:
            pass
    sr = 16000
    mono = load_mono(path, sr)
    hop = int(sr * hop_s)
    n = len(mono) // hop
    frames = mono[: n * hop].reshape(n, hop)
    rms = np.sqrt((frames.astype(np.float64) ** 2).mean(axis=1) + 1e-12)
    if key:
        try:
            np.savez_compressed(cache, key=key, rms=rms, hop=hop_s)
        except OSError:
            pass
    return rms, hop_s


def activity_regions(path: str | Path, *, min_gap: float = 0.6, min_len: float = 0.25) -> list[list[float]]:
    """Coarse vocal activity (works best on a separated vocal stem)."""
    rms, hop = rms_envelope(path)
    if len(rms) == 0:
        return []
    db = 20 * np.log10(rms + 1e-9)
    floor = np.percentile(db, 20)
    peak = np.percentile(db, 98)
    thr = floor + (peak - floor) * 0.35
    active = db > thr
    regions: list[list[float]] = []
    start = None
    for i, a in enumerate(active):
        if a and start is None:
            start = i
        elif not a and start is not None:
            regions.append([start * hop, i * hop])
            start = None
    if start is not None:
        regions.append([start * hop, len(active) * hop])
    merged: list[list[float]] = []
    for r in regions:
        if merged and r[0] - merged[-1][1] < min_gap:
            merged[-1][1] = r[1]
        else:
            merged.append(r)
    return [[round(a, 2), round(b, 2)] for a, b in merged if b - a >= min_len]
