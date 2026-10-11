"""Audio analysis helpers: vocal separation, ASR, lyric-line matching, QA.

Heavy models run in the AI runtime venv (``KM_HOME/ai_venv``) as subprocesses:

* separation → SUG ``StandaloneVocalSeparator`` (audio-separator,
  ``UVR-MDX-NET-Inst_HQ_3.onnx``), the same engine the timing module uses;
* ASR        → ``scripts/ai/asr.py`` (faster-whisper, word timestamps);
* alignment  → SUG forced-alignment worker (see ``sugbridge.align``).
"""

from __future__ import annotations

import os
import re
import subprocess
import unicodedata
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from .paths import SCRIPTS_DIR, hidden_subprocess_kwargs, km_home, models_dir

ProgressCb = Callable[[float, str], None]


def ai_python() -> Path:
    """AI runtime interpreter: ``accel`` setting / ``KM_AI_PYTHON``, else ``<home>/ai_venv``."""
    from . import accel

    chosen = accel.profile().get("ai_python")
    if chosen:
        return Path(chosen)
    return km_home() / "ai_venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def timing_audio(store, st: dict) -> Path:
    """Audio that vocal separation / forced alignment / non-video renders use:
    the Hi-Res source registered in stage 1, else the job's lossless copy of an
    audio-only source, else the audio extracted from the video."""
    media = st.get("media", {})
    for key in ("timing_audio", "audio"):
        rel = media.get(key)
        if rel and store.abs(rel).is_file():
            return store.abs(rel)
    raise SystemExit("任务没有音频素材")


# ------------------------------------------------------------------ separation
def separate_vocals(audio: Path, *, progress: Optional[ProgressCb] = None) -> tuple[Path, Path | None]:
    """Return (vocals.wav, instrumental.wav|None) next to ``audio``.

    Model files are fetched with SUG's mirror relay (same files audio-separator
    expects); inference runs in ``scripts/ai/separate.py`` so the ONNX
    execution provider follows the acceleration profile (CUDA / DirectML / CPU).
    """
    from . import accel, sugbridge

    sugbridge.setup()
    from strange_uta_game.backend.application.ai_timing import separation as sug_sep

    audio = Path(audio)
    target = audio.with_name(audio.stem + "_人声.wav")
    if target.is_file() and target.stat().st_mtime >= audio.stat().st_mtime:
        return target, _find_instrumental(audio)
    cb = progress or (lambda p, m: None)
    model_root = models_dir() / "separation"
    model_root.mkdir(parents=True, exist_ok=True)
    cb(0.02, "检查分离模型")
    try:
        sug_sep.ensure_separation_model(model_root)
        sug_sep._download_missing_model_files(model_root, proxy=os.environ.get("HTTPS_PROXY", ""))
    except Exception:
        pass  # audio-separator downloads by itself as a fallback
    provider = accel.profile().get("onnx", "cpu")
    try:
        return _run_separator(audio, model_root, provider, cb)
    except RuntimeError:
        if provider == "cpu":
            raise
        cb(0.05, f"{provider} 分离失败，改用 CPU 重试")
        return _run_separator(audio, model_root, "cpu", cb)


def _run_separator(audio: Path, model_root: Path, provider: str, cb) -> tuple[Path, Path | None]:
    from . import accel

    cmd = [str(ai_python()), str(SCRIPTS_DIR / "ai" / "separate.py"), str(audio), str(audio.parent),
           str(model_root), "--provider", provider]
    dml_device = accel.profile().get("dml_device")
    if provider == "dml" and dml_device:
        cmd += ["--dml-device", str(dml_device)]
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                            errors="replace", env=env, **hidden_subprocess_kwargs())
    tail: list[str] = []
    result = None
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip()
        if line.startswith("progress:"):
            _, pct, msg = line.split(":", 2)
            cb(float(pct) / 100.0, msg)
        elif line.startswith("provider:"):
            cb(0.05, "推理设备：" + line.split(":", 1)[1])
        elif line.startswith("done:"):
            result = line[5:]
        elif line:
            tail = (tail + [line])[-30:]
    if proc.wait() != 0 or not result:
        raise RuntimeError("人声分离失败：\n" + "\n".join(tail))
    vocals, _, inst = result.partition("|")
    return Path(vocals), (Path(inst) if inst else _find_instrumental(audio))


def _find_instrumental(audio: Path) -> Path | None:
    for cand in sorted(audio.parent.glob(audio.stem + "*Instrumental*.wav")):
        return cand
    return None


# ------------------------------------------------------------------------ ASR
def run_asr(audio: Path, out_json: Path, *, model: str = "large-v3-turbo", language: str = "ja",
            progress: Optional[ProgressCb] = None, device: str | None = None) -> Path:
    from . import accel

    prof = accel.profile()
    cmd = [str(ai_python()), str(SCRIPTS_DIR / "ai" / "asr.py"), str(audio), str(out_json),
           "--model", model, "--language", language, "--models-dir", str(models_dir() / "whisper"),
           "--device", device or prof["asr_device"], "--compute-type", prof["asr_compute"]]
    env = dict(os.environ, PYTHONIOENCODING="utf-8", HF_HUB_DISABLE_SYMLINKS_WARNING="1")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace", env=env, **hidden_subprocess_kwargs())
    tail: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip()
        if line.startswith("progress:"):
            _, pct, msg = line.split(":", 2)
            if progress:
                progress(float(pct) / 100.0, msg)
        elif line:
            tail = (tail + [line])[-30:]
    if proc.wait() != 0:
        raise RuntimeError("语音识别失败：\n" + "\n".join(tail))
    return out_json


# ------------------------------------------------------------- kana utilities
_KKS = None


def _kakasi():
    global _KKS
    if _KKS is None:
        import pykakasi

        _KKS = pykakasi.kakasi()
    return _KKS


def to_hira(text: str) -> str:
    out = []
    for ch in text:
        code = ord(ch)
        if 0x30A1 <= code <= 0x30F6:
            out.append(chr(code - 0x60))
        else:
            out.append(ch)
    return "".join(out)


_KEEP = re.compile(r"[぀-ゟ゠-ヿa-z0-9ー]")


def normalize_reading(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower()
    return "".join(ch for ch in to_hira(text) if _KEEP.match(ch))


def reading_of(text: str) -> str:
    """Kana reading of arbitrary Japanese text via pykakasi."""
    parts = _kakasi().convert(text)
    return normalize_reading("".join(p.get("hira") or p.get("orig", "") for p in parts))


# ------------------------------------------------------------------ matching
def asr_char_stream(asr: dict) -> tuple[str, list[float]]:
    """Concatenate ASR words into one kana stream with a time per character."""
    chars: list[str] = []
    times: list[float] = []
    for seg in asr.get("segments", []):
        words = seg.get("words") or [{"s": seg["start"], "e": seg["end"], "w": seg["text"]}]
        for w in words:
            kana = reading_of(w["w"])
            if not kana:
                continue
            n = len(kana)
            for k, ch in enumerate(kana):
                chars.append(ch)
                times.append(w["s"] + (w["e"] - w["s"]) * (k / max(1, n)))
    return "".join(chars), times


def _local_align(query: str, stream: str, top: int = 3) -> list[tuple[float, int, int]]:
    """Smith-Waterman (numpy, row-wise) → up to ``top`` non-overlapping hits
    as (normalised score, start, end) in stream coordinates."""
    n, m = len(query), len(stream)
    if n == 0 or m == 0:
        return []
    match, mismatch, gap = 2.0, -1.0, -1.0
    s_arr = np.frombuffer(stream.encode("utf-32-le"), dtype=np.uint32)
    H = np.zeros((n + 1, m + 1), dtype=np.float32)
    for i in range(1, n + 1):
        q = ord(query[i - 1])
        diag = H[i - 1, :-1] + np.where(s_arr == q, match, mismatch)
        up = H[i - 1, 1:] + gap
        row = np.maximum(0, np.maximum(diag, up))
        # left (gap in query) needs a running scan
        out = np.empty(m + 1, dtype=np.float32)
        out[0] = 0
        prev = 0.0
        for j in range(m):
            v = row[j]
            left = prev + gap
            if left > v:
                v = left
            out[j + 1] = v
            prev = v
        H[i] = out
    hits = []
    last = H[n].copy()
    best_possible = match * n
    for _ in range(top):
        j = int(np.argmax(last))
        score = float(last[j])
        if score <= 0:
            break
        # traceback start position
        i, jj = n, j
        while i > 0 and jj > 0 and H[i, jj] > 0:
            if H[i, jj] == H[i - 1, jj - 1] + (match if query[i - 1] == stream[jj - 1] else mismatch):
                i, jj = i - 1, jj - 1
            elif H[i, jj] == H[i - 1, jj] + gap:
                i -= 1
            else:
                jj -= 1
        start = jj
        hits.append((score / best_possible, start, j))
        lo, hi = max(0, start - n // 2), min(m + 1, j + n // 2 + 1)
        last[lo:hi] = 0
    return hits


def match_lines(line_readings: list[str], asr: dict, *, min_score: float = 0.35) -> list[dict]:
    """Locate each lyric line in the ASR transcript, enforcing time order.

    Returns per line ``{"score", "start", "end", "heard"}`` (None when not found).
    """
    stream, times = asr_char_stream(asr)
    cands: list[list[tuple[float, int, int]]] = [
        _local_align(r, stream, top=4) if len(r) >= 2 else [] for r in line_readings]
    # DP: choose for each line one hit or skip, positions strictly increasing.
    n = len(line_readings)
    best: list[dict] = [{} for _ in range(n + 1)]
    # state: last end position -> (total score, choices)
    states: dict[int, tuple[float, list]] = {-1: (0.0, [])}
    for i in range(n):
        new_states: dict[int, tuple[float, list]] = {}

        def push(pos, score, choice):
            cur = new_states.get(pos)
            if cur is None or score > cur[0]:
                new_states[pos] = (score, choice)

        for pos, (score, choice) in states.items():
            push(pos, score, choice + [None])
            for sc, s, e in cands[i]:
                if sc >= min_score and s > pos - 2:
                    push(e, score + sc, choice + [(sc, s, e)])
        # prune
        states = dict(sorted(new_states.items(), key=lambda kv: -kv[1][0])[:60])
    _, choices = max(states.values(), key=lambda v: v[0])
    out = []
    for i, ch in enumerate(choices):
        if ch is None:
            # report best unconstrained hit for the agent's information
            alt = cands[i][0] if cands[i] else None
            out.append({"score": round(alt[0], 2) if alt else 0.0, "start": None, "end": None,
                        "heard": stream[alt[1]:alt[2]] if alt else "", "ordered": False})
            continue
        sc, s, e = ch
        out.append({"score": round(sc, 2), "start": round(times[s], 2) if s < len(times) else None,
                    "end": round(times[min(e, len(times)) - 1], 2) if e > 0 else None,
                    "heard": stream[s:e], "ordered": True})
    return out


def map_to_reference(readings: list[str], ref_readings: list[str]) -> list[tuple[int | None, float]]:
    """Monotonic best mapping of lyric lines onto reference (synced LRC) lines.
    Returns per line (reference index or None, similarity)."""
    import difflib

    n, m = len(readings), len(ref_readings)
    sim = [[difflib.SequenceMatcher(None, a, b, autojunk=False).ratio() if a and b else 0.0
            for b in ref_readings] for a in readings]
    # DP over (i, j): best total similarity with j non-decreasing (a reference
    # line may be reused for repeated lyric lines only if adjacent).
    NEG = -1e9
    best = [[NEG] * (m + 1) for _ in range(n + 1)]
    back: list[list[tuple[int, int, int | None] | None]] = [[None] * (m + 1) for _ in range(n + 1)]
    for j in range(m + 1):
        best[0][j] = 0.0
    for i in range(1, n + 1):
        run_best, run_j = NEG, 0
        for j in range(m + 1):
            if best[i - 1][j] > run_best:
                run_best, run_j = best[i - 1][j], j
            # skip line i (unmapped)
            if run_best > best[i][j]:
                best[i][j] = run_best
                back[i][j] = (i - 1, run_j, None)
            if j >= 1:
                s = sim[i - 1][j - 1]
                if s >= 0.55:
                    prev = max(range(j), key=lambda k: best[i - 1][k])
                    cand = best[i - 1][prev] + s
                    if cand > best[i][j]:
                        best[i][j] = cand
                        back[i][j] = (i - 1, prev, j - 1)
    j = max(range(m + 1), key=lambda k: best[n][k])
    out: list[tuple[int | None, float]] = [(None, 0.0)] * n
    i = n
    while i > 0 and back[i][j] is not None:
        pi, pj, chosen = back[i][j]
        out[i - 1] = (chosen, sim[i - 1][chosen] if chosen is not None else 0.0)
        i, j = pi, pj
    return out


def _in_activity(t: float, activity: list[list[float]], pad: float = 1.0) -> bool:
    return any(a - pad <= t <= b + pad for a, b in activity)


def estimate_line_times(matches: list[dict], mapping: list[tuple[int | None, float]], ref_times: list[float | None],
                        activity: list[list[float]], duration: float) -> list[dict]:
    """Fuse ASR anchors with reference LRC timing.

    Anchors (good ASR hit + confident reference mapping) give ``video - ref``
    offsets; offsets are grouped into pieces wherever they jump (MAD cuts) and
    every mapped line gets a predicted video time from the nearest piece.
    """
    anchors = []
    for i, (m, (j, s)) in enumerate(zip(matches, mapping)):
        if m.get("start") is not None and m["score"] >= 0.5 and j is not None and s >= 0.8 and ref_times[j] is not None:
            anchors.append((i, ref_times[j], m["start"] - ref_times[j]))
    pieces: list[list[tuple[int, float, float]]] = []
    for a in anchors:
        if pieces and abs(a[2] - float(np.median([x[2] for x in pieces[-1]]))) <= 3.0:
            pieces[-1].append(a)
        else:
            pieces.append([a])
    # drop single-anchor pieces that sit between two agreeing pieces (ASR outliers)
    pieces = [p for k, p in enumerate(pieces) if len(p) > 1 or len(pieces) == 1
              or not (0 < k < len(pieces) - 1 and abs(np.median([x[2] for x in pieces[k - 1]])
                                                       - np.median([x[2] for x in pieces[k + 1]])) <= 3.0)]
    piece_info = [(min(x[1] for x in p), max(x[1] for x in p), float(np.median([x[2] for x in p]))) for p in pieces]
    out = []
    for i, (m, (j, s)) in enumerate(zip(matches, mapping)):
        est = None
        source = None
        if m.get("start") is not None and m["score"] >= 0.5:
            est, source = m["start"], "asr"
        if j is not None and ref_times[j] is not None and piece_info:
            rt = ref_times[j]
            inside = [p for p in piece_info if p[0] - 0.5 <= rt <= p[1] + 0.5]
            if inside:
                off = inside[0][2]
            else:
                off = min(piece_info, key=lambda p: min(abs(rt - p[0]), abs(rt - p[1])))[2]
            pred = rt + off
            if est is None or abs(est - pred) > 4.0:
                if 0 <= pred <= duration and (_in_activity(pred, activity) or not activity):
                    est, source = round(pred, 2), "lrc"
        out.append({"est": None if est is None else round(est, 2), "source": source,
                    "ref": j, "ref_sim": round(s, 2)})
    return out


def suggest_segment(matches: list[dict], duration: float, activity: list[list[float]]) -> dict:
    hits = [m for m in matches if m.get("start") is not None and m["score"] >= 0.5]
    if not hits:
        return {"start": 0.0, "end": round(duration, 2), "confidence": 0.0}
    first, last = hits[0]["start"], max(h["end"] for h in hits if h.get("end") is not None)
    start = max(0.0, first - 2.0)
    end = min(duration, last + 2.5)
    # extend to the end of the vocal activity region that contains the last hit
    for a, b in activity:
        if a <= last <= b:
            end = min(duration, max(end, b + 1.5))
    conf = len(hits) / max(1, len(matches))
    return {"start": round(start, 2), "end": round(end, 2), "confidence": round(conf, 2)}


# ------------------------------------------------------------------------- QA
def timing_qa(view: dict, *, vocal_rms: tuple[np.ndarray, float] | None = None,
              asr_matches: dict[int, dict] | None = None, line_scores: dict | None = None) -> dict[int, dict]:
    """Heuristic per-line checks used by the agent and the review page."""
    qa: dict[int, dict] = {}
    lines = view["lines"]
    rms, hop = vocal_rms if vocal_rms is not None else (None, 0.02)
    thr = None
    if rms is not None and len(rms):
        db = 20 * np.log10(rms + 1e-9)
        thr = np.percentile(db, 25) + (np.percentile(db, 97) - np.percentile(db, 25)) * 0.3
    for line in lines:
        i = line["i"]
        notes: list[str] = []
        level = 0
        timed = [c for c in line["chars"] if c["cp"]]
        if not timed:
            qa[i] = {"flag": "bad", "notes": ["没有时间戳"], "score": 0.0}
            continue
        start, end = line["start"], line["end"]
        # a long held final note is normal: rate uses at most 1.2 s of the last syllable
        last = timed[-1]
        eff_end = min(end, last["s"] + 1.2) if last["s"] is not None else end
        dur = max(0.01, eff_end - start)
        moras = sum(max(1, len(c["cp"])) for c in timed)
        rate = moras / dur
        if rate > 11:
            notes.append(f"语速过快（{rate:.1f} 拍/秒），可能被压缩")
            level = max(level, 2)
        elif rate < 1.0 and dur > 4:
            notes.append(f"行持续 {dur:.1f} 秒偏长（{rate:.1f} 拍/秒）")
            level = max(level, 1)
        short = sum(1 for c in timed if (c["e"] - c["s"]) < 0.045)
        if short >= max(2, len(timed) // 3):
            notes.append(f"{short} 个字时长不足 45ms")
            level = max(level, 1 if short < len(timed) // 2 else 2)
        if i + 1 < len(lines) and lines[i + 1]["start"] is not None and end > lines[i + 1]["start"] + 0.05:
            notes.append("与下一行重叠")
            level = max(level, 1)
        if thr is not None:
            a, b = int(start / hop), max(int(start / hop) + 1, int(end / hop))
            seg = 20 * np.log10(rms[a:b] + 1e-9)
            voiced = float((seg > thr).mean()) if len(seg) else 0.0
            if voiced < 0.25:
                notes.append(f"该区间人声能量很低（{voiced:.0%}），疑似错位")
                level = max(level, 2)
            elif voiced < 0.45:
                notes.append(f"人声覆盖偏低（{voiced:.0%}）")
                level = max(level, 1)
        if thr is not None:
            for ci, c in enumerate(line["chars"]):
                if not c["cp"] or c["e"] - c["s"] < 1.6:
                    continue
                a, b = int(c["s"] / hop), int(c["e"] / hop)
                seg = 20 * np.log10(rms[a:b] + 1e-9)
                frac = float((seg > thr).mean()) if len(seg) else 1.0
                if frac < 0.4:
                    notes.append(f"第 {ci + 1} 字「{c['c']}」持续 {c['e'] - c['s']:.1f} 秒，其中 {1 - frac:.0%} 无人声")
                    level = max(level, 2 if c["e"] - c["s"] > 3 else 1)
        if asr_matches and asr_matches.get(i) and asr_matches[i].get("start") is not None:
            m = asr_matches[i]
            if m["score"] >= 0.6 and abs(m["start"] - start) > 2.5:
                notes.append(f"与语音识别位置相差 {m['start'] - start:+.1f} 秒")
                level = max(level, 2 if abs(m["start"] - start) > 5 else 1)
        score = (line_scores or {}).get(i)
        flag = ("ok", "warn", "bad")[level]
        qa[i] = {"flag": flag, "notes": notes, "score": None if score is None else round(float(score), 3)}
    return qa


# ------------------------------------------------------------------ ruby QA
def _norm_kana(s: str) -> str:
    """ひらがな归一化比较用：カタカナ→ひらがな，づ→ず，ぢ→じ。"""
    out = []
    for ch in s:
        o = ord(ch)
        if 0x30A1 <= o <= 0x30F6:
            ch = chr(o - 0x60)
        out.append(ch)
    return "".join(out).replace("づ", "ず").replace("ぢ", "じ")


def _is_hira(ch: str) -> bool:
    return bool(ch) and all(0x3040 <= ord(c) <= 0x309F or c in "ー" for c in ch)


def ruby_qa(st: dict, view: dict, asr_path: Path) -> dict[int, list[str]]:
    """读音审查：行号 → warn notes。

    对每个注音词（阶段一歌词的 ruby span + 送り仮名），按其时间窗收集 ASR 听到的词，
    转成假名后与注音比较；相似度 < 0.5 时给出「读音待确认」warn。当て字 / 原唱与翻唱
    读法不同（違う→たがう、瞬間→とき、理由→わけ）都靠这个信号浮出来。不用分析器的
    默认读音做基准（脱离上下文误读多：描く→かく、霞ませる→かすみませる）。
    """
    import difflib
    import json as _json

    from .paths import ensure_repo_on_path

    ensure_repo_on_path()
    from strange_uta_game.backend.infrastructure.parsers.ruby_analyzer import create_analyzer

    analyzer = create_analyzer()
    words: list[tuple[float, float, str]] = []
    try:
        data = _json.loads(Path(asr_path).read_text(encoding="utf-8"))
        for seg in data.get("segments", []):
            for w in seg.get("words") or []:
                t = str(w["w"])
                latin = sum(1 for c in t if ord(c) < 0x2E80)  # 间奏幻觉的英文词不参与
                if float(w.get("p") or 1.0) >= 0.3 and latin * 2 <= len(t):
                    words.append((float(w["s"]), float(w["e"]), t))
    except Exception:
        pass
    if not words:
        return {}

    def best_sim(a: str, b: str) -> float:
        """a（注音）对 b（ASR，可能带前后文）的最佳局部相似度。"""
        if not a or not b:
            return 0.0
        if len(b) <= len(a):
            return difflib.SequenceMatcher(None, a, b).ratio()
        return max(difflib.SequenceMatcher(None, a, b[k:k + len(a)]).ratio()
                   for k in range(len(b) - len(a) + 1))

    stage1 = [l for l in st["lyrics"]["lines"] if l.get("include", True) and l["text"].strip()]
    heard: dict[int, list[str]] = {}
    for line, src_line in zip(view["lines"], stage1):
        if line["text"] != src_line["text"] or line.get("start") is None:
            continue
        i, text, chars = line["i"], line["text"], line["chars"]
        spans = [list(r[:3]) for r in src_line.get("ruby") or []]
        merged: list[list] = []  # 相邻逐字 span 合并回词级再比
        for sp in spans:
            if merged and sp[0] == merged[-1][1] + 1:
                merged[-1][1] = sp[1]
                merged[-1][2] += sp[2]
            else:
                merged.append(sp)
        for s, e, reading in merged:
            reading = str(reading).replace("^pause^", "").replace("^", "")  # ^ = 停顿检查点标记
            if not reading:
                continue
            ee = e + 1  # 送り仮名一起比（違う→たがう 的 う）
            while ee < len(text) and _is_hira(text[ee]):
                ee += 1
            surface, full = text[s:ee], reading + text[e + 1:ee]
            full_n = _norm_kana(full)
            if len(full_n) < 2:
                continue
            timed = [c for c in chars[s:ee] if c.get("s") is not None and c.get("e") is not None]
            if not timed:
                continue
            # Whisper 词起点在唱段上常早 1 秒以上（SKILL.md 已知偏差），窗口向前放宽
            t0 = timed[0]["s"] - 1.5
            t1 = min(timed[-1]["e"] + 0.5, timed[0]["s"] + 4.0)  # 长音尾巴会把间奏幻觉卷进来
            got = "".join(w for a, b, w in words if a < t1 and b > t0)
            if not got:
                continue
            if surface in got:
                continue  # ASR 写出了同一个词：读音无分歧
            try:
                heard_n = _norm_kana(analyzer.get_reading(got))
            except Exception:
                continue
            if not heard_n:
                continue
            if best_sim(full_n, heard_n) < 0.5:
                heard.setdefault(i, []).append(f"读音待确认：「{surface}」注音 {full}，ASR 听到「{got}」")
    return heard
