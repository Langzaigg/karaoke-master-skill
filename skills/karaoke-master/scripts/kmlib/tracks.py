"""原唱 / 伴奏 audio tracks for the deliverables (on/off vocal MP4, Hi-Res MKV).

Defaults when the user supplies nothing:

* 原唱 (on vocal) — audio-only job: the original source file (keeps its native
  quality); video job: the audio extracted from the source video;
* 伴奏 (off vocal) — the instrumental produced by vocal separation in stage 1,
  or, when a lossless 原唱 is supplied, separated from that file instead.

User supplied files usually come from a different release than the video's
audio, so they are aligned first: envelopes from the workbench's own
``audio_alignment.extract_waveform`` are cross-correlated with FFT (any offset),
checked window by window for consistency (an edited MAD has no single offset),
then shifted with ``audio_alignment.export_aligned_audio``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .jobstore import JobStore
from .paths import ensure_repo_on_path


# ------------------------------------------------------------------ alignment
def _envelope(peaks: list[float]) -> np.ndarray:
    x = np.asarray(peaks, dtype=np.float64)
    if len(x) > 5:
        x = np.convolve(x, np.ones(5) / 5.0, mode="same")
    x = x - x.mean()
    norm = np.linalg.norm(x)
    return x / norm if norm > 0 else x


def _best_lag(ref: np.ndarray, tgt: np.ndarray, *, min_overlap: int) -> tuple[int, float, float]:
    """lag L maximising sum ref[i + L] * tgt[i] (ref = audio, tgt = video)."""
    from scipy.signal import correlate

    corr = correlate(ref, tgt, mode="full", method="fft")
    lags = np.arange(-(len(tgt) - 1), len(ref))
    overlap = np.minimum(len(ref), lags + len(tgt)) - np.maximum(0, lags)
    corr = np.where(overlap >= min_overlap, corr, -np.inf)
    k = int(np.argmax(corr))
    best = float(corr[k])
    excl = corr.copy()
    lo, hi = max(0, k - 40), min(len(excl), k + 41)
    excl[lo:hi] = -np.inf
    second = float(np.max(excl)) if np.isfinite(excl).any() else 0.0
    return int(lags[k]), best, second


def estimate_shift(video: Path, audio: Path, log=print) -> dict:
    """Seconds to shift ``audio`` (positive = pad silence, negative = trim)
    so it lines up with the video's own audio track."""
    ensure_repo_on_path()
    from krok_helper.audio_alignment import extract_waveform

    vw = extract_waveform(Path(video), None, log, label="视频")
    aw = extract_waveform(Path(audio), None, log, label=Path(audio).name)
    pps = vw.peaks_per_second
    v, a = _envelope(vw.peaks), _envelope(aw.peaks)
    lag, best, second = _best_lag(a, v, min_overlap=int(min(len(v), len(a)) * 0.5))
    shift = -lag / pps  # audio time = video time + lag/pps
    confidence = 0.0 if best <= 0 else max(0.0, min(1.0, (best - max(second, 0.0)) / best * 2.5))
    win = int(30 * pps)
    local = []
    for start in range(0, max(1, len(v) - win), win):
        seg = _envelope(vw.peaks[start:start + win])
        if np.abs(seg).sum() == 0:
            continue
        lo = max(0, start + lag - int(3 * pps))
        hi = min(len(a), start + lag + win + int(3 * pps))
        if hi - lo < win:
            continue
        l2, _b2, _ = _best_lag(_envelope(aw.peaks[lo:hi]), seg, min_overlap=int(win * 0.9))
        local.append(round(-(lo + l2 - start) / pps, 3))
    deviations = [abs(x - shift) for x in local]
    consistent = (not local) or (float(np.median(deviations)) < 0.08 and max(deviations) < 0.5)
    return {"shift": round(shift, 3), "confidence": round(confidence, 2), "consistent": bool(consistent),
            "local": local, "audio_duration": aw.duration, "video_duration": vw.duration}


# ------------------------------------------------------------------ sources
def master_mp4(store: JobStore, st: dict) -> Path | None:
    """The rendered karaoke video (its own audio is the on-vocal mix)."""
    for e in st.get("exports", []):
        if e.get("kind") == "mp4" and e.get("state") == "done":
            p = store.abs(e["path"])
            if p.is_file():
                return p
    return None


def default_sources(store: JobStore, st: dict) -> tuple[Path | None, list[Path]]:
    """原唱 / 伴奏 when the caller names none: the Hi-Res source registered in
    stage 1 (already on the video timeline) wins, then the job's own audio."""
    media = st.get("media", {})
    hires = media.get("hires") or {}
    on = None
    if hires.get("on") and store.abs(hires["on"]).is_file():
        on = store.abs(hires["on"])
    elif media.get("timing_audio") and store.abs(media["timing_audio"]).is_file():
        on = store.abs(media["timing_audio"])
    elif media.get("audio"):
        on = store.abs(media["audio"])
    offs = [store.abs(p) for p in hires.get("offs", []) if store.abs(p).is_file()]
    if not offs and media.get("instrumental"):
        offs = [store.abs(media["instrumental"])]
    return on, offs


def register_hires(store: JobStore, on: str | None, offs: list[str] | None = None, *, align: bool = True) -> dict:
    """Stage 1: put the user's lossless 原唱 (and optional 伴奏) on the video timeline.

    The aligned 原唱 becomes the job's timing audio — vocal separation and forced
    alignment in stage 2 run on it — and the default source for on vocal / Hi-Res.
    """
    ensure_repo_on_path()
    from krok_helper.audio_alignment import export_aligned_audio

    st = store.load()
    ref = store.abs(st["media"]["audio"])
    notes: list[str] = []
    result: dict = {"on": None, "offs": [], "notes": notes}

    def place(src: str, dest_name: str, label: str, shared: float | None = None) -> tuple[str | None, float | None]:
        p = Path(src)
        if not p.is_absolute():
            p = store.dir / p
        if not p.is_file():
            notes.append(f"⚠ {label}文件不存在：{p}")
            return None, None
        shift = 0.0
        if align:
            info = estimate_shift(ref, p, log=store.log)
            shift = info["shift"]
            if shared is not None and (info["confidence"] < 0.35 or abs(shift - shared) < 0.05):
                shift = shared
            elif info["confidence"] < 0.2:
                notes.append(f"⚠ {label}「{p.name}」与素材音频无法可靠对齐（置信 {info['confidence']:.0%}），未采用")
                return None, None
            if not info["consistent"]:
                notes.append(f"⚠ {label}「{p.name}」与素材无法用单一偏移对齐（素材可能经过剪辑），"
                             f"已按整体偏移 {shift:+.3f} 秒处理")
            notes.append(f"{label}：{p.name} 对齐 {shift:+.3f} 秒（置信 {info['confidence']:.0%}）")
        out = store.path("media", dest_name)
        export_aligned_audio(p, out, shift, None, store.log)  # WAV, keeps the source's rate / bit depth
        return store.rel(out), shift

    on_shift = None
    if on:
        result["on"], on_shift = place(on, "hires_on.wav", "原唱")
    for k, off in enumerate([o for o in (offs or []) if o]):
        rel, _ = place(off, f"hires_off_{k + 1}.wav", "伴奏" if len(offs) == 1 else f"伴奏{k + 1}", shared=on_shift)
        if rel:
            result["offs"].append(rel)

    def apply(s: dict) -> None:
        s["media"]["hires"] = {"on": result["on"], "offs": result["offs"], "notes": notes,
                               "src_on": on, "src_offs": offs or []}
        if result["on"]:
            s["media"]["timing_audio"] = result["on"]
        s.setdefault("options", {})["hires"] = {"on": on, "off": offs or []}

    store.update(apply)
    for n in notes:
        store.log(n)
    return result


@dataclass
class Tracks:
    on: Path | None
    on_is_default: bool
    offs: list[Path] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def resolve_tracks(store: JobStore, video: Path, *, on: str | None, offs: list[str] | None, align: bool = True,
                   separate_from_on: bool = True, progress=None) -> Tracks:
    """Pick and align the 原唱 / 伴奏 files for ``video``."""
    ensure_repo_on_path()
    from krok_helper.audio_alignment import export_aligned_audio

    st = store.load()
    work = store.path("render", "tracks", "x").parent
    def_on, def_offs = default_sources(store, st)
    notes: list[str] = []
    say = progress or (lambda msg: None)
    # Every render (master, preview clip, MV) shares the source audio's timeline, so
    # user files are aligned against the full source audio, not the (maybe short) video.
    ref = store.abs(st["media"]["audio"]) if st.get("media", {}).get("audio") else Path(video)
    if not Path(ref).is_file():
        ref = Path(video)

    def resolve(path: str | None, default: Path | None, label: str, shared_shift: float | None = None):
        if not path:
            if default is not None:
                notes.append(f"{label}：使用{'素材原音频' if label == '原唱' else '人声分离得到的伴奏'}（{default.name}）")
            return default, None
        p = Path(path)
        if not p.is_absolute():
            p = store.dir / p
        if not p.is_file():
            raise SystemExit(f"{label}文件不存在：{p}")
        if not align:
            notes.append(f"{label}：{p.name}（未对齐，按原样使用）")
            return p, None
        say(f"对齐{label}：{p.name}")
        info = estimate_shift(ref, p, log=store.log)
        shift = info["shift"]
        if shared_shift is not None and (info["confidence"] < 0.35 or abs(shift - shared_shift) < 0.05):
            shift = shared_shift
        elif info["confidence"] < 0.2:
            notes.append(f"⚠ {label}「{p.name}」与素材音频无法可靠对齐（置信 {info['confidence']:.0%}），"
                         f"已改用默认音轨；请确认文件是否为同一首歌的同一版本")
            return default, None
        if not info["consistent"]:
            notes.append(f"⚠ {label}「{p.name}」与视频音轨无法用单一偏移对齐（素材可能经过剪辑），"
                         f"各段偏移 {info['local'][:6]}；已按整体偏移 {shift:+.3f} 秒处理，请试听")
        if abs(shift) < 0.002:
            notes.append(f"{label}：{p.name}（无需偏移，置信 {info['confidence']:.0%}）")
            return p, 0.0
        out = work / f"{p.stem}_aligned_{shift:+.3f}.wav"  # shift in the name: a cache can never be reused wrongly
        if not out.is_file() or out.stat().st_mtime < p.stat().st_mtime:
            export_aligned_audio(p, out, shift, None, store.log)
        notes.append(f"{label}：{p.name} 已对齐 {shift:+.3f} 秒（置信 {info['confidence']:.0%}）")
        return out, shift

    hires = st.get("media", {}).get("hires") or {}
    if on and hires.get("on") and on == hires.get("src_on"):
        on = None  # same file as registered in stage 1: use the aligned copy
    if offs and hires.get("offs") and list(offs) == list(hires.get("src_offs") or []):
        offs = None
    registered_on = bool(not on and hires.get("on") and store.abs(hires["on"]).is_file())
    if registered_on:
        on_path, on_shift = store.abs(hires["on"]), 0.0
        notes.append("原唱：阶段一登记的 Hi-Res 音源（已对齐）")
    else:
        on_path, on_shift = resolve(on, def_on, "原唱")
    offs = [o for o in (offs or []) if o]
    aligned_offs: list[Path] = []
    if not offs and hires.get("offs"):
        aligned_offs = [store.abs(p) for p in hires["offs"] if store.abs(p).is_file()]
        if aligned_offs:
            notes.append("伴奏：阶段一登记的 Hi-Res 伴奏（已对齐）")
    if on and not offs and separate_from_on:
        # better 伴奏: separate the (aligned) lossless 原唱 instead of the video's lossy audio
        from . import analysis, media

        src = Path(on_path)
        if work not in src.resolve().parents:
            src = media.extract_audio(on_path, work / "on_source.wav",
                                      sample_rate=int(media.probe(on_path).get("sample_rate") or 48000))
        say("从原唱分离伴奏")
        _vocals, inst = analysis.separate_vocals(src)
        if inst is not None:
            aligned_offs.append(inst)
            notes.append(f"伴奏：由提供的原唱分离生成（{inst.name}）")
    if not aligned_offs:
        for i, path in enumerate(offs or [None]):
            label = "伴奏" if len(offs) <= 1 else f"伴奏{i + 1}"
            p, _ = resolve(path, def_offs[0] if (not path and def_offs) else None, label, shared_shift=on_shift)
            if p is not None:
                aligned_offs.append(Path(p))
    # Renders over a non-video background already carry the timing audio (= registered
    # Hi-Res source / original file); only source-video masters carry the video's own audio.
    bg_type = ((st.get("options") or {}).get("background") or {}).get("type")
    has_video = bool((st.get("media") or {}).get("source", {}).get("has_video"))
    master_has_timing_audio = not (has_video and bg_type in (None, "source"))
    on_is_default = not on and (master_has_timing_audio or not registered_on)
    return Tracks(on=Path(on_path) if on_path else None, on_is_default=on_is_default, offs=aligned_offs, notes=notes)
