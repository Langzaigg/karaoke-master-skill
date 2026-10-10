"""Bridge to StrangeUtaGame (the Lin-K Lyrics timing submodule).

* build a SUG ``Project`` from lyric lines (+ UtaTen style ruby + singers);
* fill missing readings with SUG's own AutoCheckService;
* run SUG's forced-alignment worker (``NextFire/mms-300m-ForcedAligner-karaoke-ja-Latn``)
  on the separated vocal, optionally per line range / time window;
* export a compact "view" JSON (per character wipe windows) for the web page;
* apply timing edits and save ``.sug`` / LRC exports.

Import this module only from the skill venv (it needs PyQt6.QtCore for the
SUG lyric loader). ``setup()`` must run before any SUG import because SUG
reads its config/cache directories from environment variables at import time.
"""

from __future__ import annotations

import copy
import os
import tempfile
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from .paths import ensure_repo_on_path, km_home, models_dir

ALIGN_MODEL_ID = "NextFire/mms-300m-ForcedAligner-karaoke-ja-Latn"
ALIGN_MODEL_LICENSE = "CC-BY-NC-SA-4.0"

_READY = False
_PLAN_LOCK = __import__("threading").Lock()  # analyzers / resolver are not thread safe


def setup() -> None:
    global _READY
    if _READY:
        return
    home = km_home()
    for key, sub in (("SUG_CONFIG_DIR", "sug_config"), ("SUG_CACHE_DIR", "sug_cache"), ("SUG_LOGS_DIR", "sug_logs")):
        path = home / sub
        path.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault(key, str(path))
    ensure_repo_on_path()
    try:  # same kana-preservation patches the workbench applies to embedded SUG
        from krok_helper.sug_compat import apply_sug_compat_patches

        apply_sug_compat_patches()
    except Exception:
        pass
    _READY = True


# --------------------------------------------------------------------------- build
def _escape_block_text(text: str) -> str:
    return text.replace("{", "｛").replace("}", "｝")


def line_to_utaten(text: str, ruby: Iterable[Iterable[Any]] | None) -> str:
    """``text`` + ``[[start, end, reading], ...]`` (inclusive char ranges) →
    UtaTen ruby markup ``{漢字||かんじ}``."""
    spans = sorted(((int(a), int(b), str(r)) for a, b, r in (ruby or []) if str(r).strip()), key=lambda x: x[0])
    out: list[str] = []
    pos = 0
    for start, end, reading in spans:
        if start < pos or end >= len(text) or start > end:
            continue
        out.append(_escape_block_text(text[pos:start]))
        out.append("{" + _escape_block_text(text[start:end + 1]) + "||" + _escape_block_text(reading) + "}")
        pos = end + 1
    out.append(_escape_block_text(text[pos:]))
    return "".join(out)


def auto_check_flags() -> dict:
    setup()
    from strange_uta_game.frontend.settings.app_settings import AppSettings

    return dict(AppSettings.DEFAULT_SETTINGS["auto_check"])


def _auto_check_service(all_text: str):
    from strange_uta_game.backend.application.auto_check_service import (
        AutoCheckService,
        is_chinese_lyrics,
    )

    flags = auto_check_flags()
    if is_chinese_lyrics(all_text):
        from strange_uta_game.backend.infrastructure.parsers.ruby_analyzer import create_pinyin_analyzer

        return AutoCheckService(auto_check_flags=flags, chinese_mode=True, pinyin_analyzer=create_pinyin_analyzer())
    return AutoCheckService(auto_check_flags=flags)


def build_project(lines: list[dict], *, singers: list[dict] | None = None, meta: dict | None = None):
    """Build a SUG Project.

    ``lines``: ``[{"text", "ruby": [[s, e, reading]], "singer": id}]`` (only the
    lines that should be sung, in order). ``singers``: ``[{"id", "name", "color"}]``.
    """
    setup()
    from strange_uta_game.backend.domain import Project
    from strange_uta_game.backend.domain.entities import Singer
    from strange_uta_game.backend.domain.project import ProjectMetadata
    from strange_uta_game.frontend.editor.timing.lyric_loader import parse_lyric_content

    meta = meta or {}
    project = Project(metadata=ProjectMetadata(
        title=meta.get("title") or "", artist=meta.get("artist") or "", album=meta.get("work") or "",
        language=meta.get("language") or "ja"))
    default = project.get_default_singer()
    singer_ids: dict[str, str] = {}
    for idx, sg in enumerate(singers or []):
        if idx == 0:
            default.name = sg.get("name") or default.name
            default.color = sg.get("color") or default.color
            default.is_placeholder = False
            singer_ids[sg["id"]] = default.id
            continue
        s = Singer(name=sg.get("name") or f"歌手{idx + 1}", color=sg.get("color") or "#FF6B6B", is_default=False)
        project.add_singer(s)
        singer_ids[sg["id"]] = s.id

    content = "[tool:utaten-ruby]\n" + "\n".join(line_to_utaten(l["text"], l.get("ruby")) for l in lines)
    sentences, _nico, _new, _meta = parse_lyric_content(
        content, default.id, list(project.singers),
        auto_check_flags=auto_check_flags(), user_dict=[], skip_settings_sync=True,
    )
    sentences = [s for s in sentences if s.characters]
    if len(sentences) != len(lines):
        raise RuntimeError(f"歌词解析行数不一致：输入 {len(lines)} 行，解析得到 {len(sentences)} 行")
    for line, sentence in zip(lines, sentences):
        sid = singer_ids.get(str(line.get("singer") or ""), default.id)
        sentence.singer_id = sid
        for ch in sentence.characters:
            ch.singer_id = sid
        project.add_sentence(sentence)
    # karaoke convention: no furigana on kana (checkpoints are kept)
    _auto_check_service("\n".join(l["text"] for l in lines)).analyze_and_apply_pipeline(
        project, only_noruby=True, delete_types=["hiragana", "katakana_hiragana_ruby"])
    return project


def load_project(path: str | Path):
    setup()
    from strange_uta_game.backend.infrastructure.persistence.sug_io import SugProjectParser

    return SugProjectParser.load(str(path))


def drop_stale_pauses(project) -> int:
    """Clear mid-line pause releases that fall after the next character's start (left behind when
    words move, e.g. English lines): the previous word's wipe would run into the next one."""
    n = 0
    for s in project.sentences:
        chs = s.characters
        for ci, ch in enumerate(chs):
            if ch.is_sentence_end and ch.sentence_end_ts is not None:
                nxt = next((t for c in chs[ci + 1:] for t in c.timestamps), None)
                if nxt is not None and ch.sentence_end_ts > nxt:
                    ch.is_sentence_end = False
                    ch.sentence_end_ts = None
                    n += 1
    return n


def save_project(project, path: str | Path, *, media_path: str | None = None) -> Path:
    setup()
    from strange_uta_game.backend.infrastructure.persistence.sug_io import SugProjectParser

    drop_stale_pauses(project)

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tags = {"title": project.metadata.title, "artist": project.metadata.artist}
    SugProjectParser.save(project, str(path), nicokara_tags=tags, media_path=media_path)
    return path


# --------------------------------------------------------------------------- view
def _ruby_parts(ch) -> list[str]:
    if ch.ruby is None:
        return []
    return [p.text for p in ch.ruby.parts]


def char_windows(sentence) -> list[tuple[float | None, float | None, list[float]]]:
    """Per character (start, end, checkpoint times) in seconds, following the
    SUG preview rules: a timed char wipes until the next timed char starts, or
    until its own sentence-end checkpoint; untimed chars collapse onto the
    surrounding boundary."""
    chars = sentence.characters
    starts = [(ch.timestamps[0] if ch.timestamps else None) for ch in chars]
    line_end = None
    for ch in chars:
        if ch.is_sentence_end and ch.sentence_end_ts is not None:
            line_end = max(line_end or 0, ch.sentence_end_ts)
        if ch.timestamps:
            line_end = max(line_end or 0, ch.timestamps[-1])
    out = []
    for i, ch in enumerate(chars):
        if not ch.timestamps:
            out.append((None, None, []))
            continue
        start = ch.timestamps[0]
        if ch.is_sentence_end and ch.sentence_end_ts is not None:
            end = ch.sentence_end_ts
        else:
            nxt = next((s for s in starts[i + 1:] if s is not None), None)
            end = nxt if nxt is not None else (line_end if line_end and line_end > start else start + 400)
        end = max(end, ch.timestamps[-1])
        out.append((start / 1000.0, end / 1000.0, [t / 1000.0 for t in ch.timestamps]))
    # untimed chars: snap to previous end (or next start)
    for i, (s, e, cps) in enumerate(out):
        if s is None:
            prev = next((out[j][1] for j in range(i - 1, -1, -1) if out[j][1] is not None), None)
            nxt = next((out[j][0] for j in range(i + 1, len(out)) if out[j][0] is not None), None)
            t = prev if prev is not None else nxt
            out[i] = (t, t, [])
    return out


def project_view(project, *, qa: dict | None = None) -> dict:
    singers = [{"id": s.id, "name": s.name, "color": s.color} for s in project.singers]
    lines = []
    for li, sentence in enumerate(project.sentences):
        wins = char_windows(sentence)
        chars = []
        for ch, (s, e, cps) in zip(sentence.characters, wins):
            chars.append({
                "c": ch.char,
                "r": _ruby_parts(ch),
                "s": None if s is None else round(s, 3),
                "e": None if e is None else round(e, 3),
                "cp": [round(t, 3) for t in cps],
                "link": bool(ch.linked_to_next),
                "sg": ch.singer_id or sentence.singer_id,
            })
        timed = [c for c in chars if c["s"] is not None]
        lines.append({
            "i": li,
            "singer": sentence.singer_id,
            "text": sentence.text if hasattr(sentence, "text") else "".join(c["c"] for c in chars),
            "start": timed[0]["s"] if timed else None,
            "end": max((c["e"] for c in timed), default=None),
            "chars": chars,
            "qa": (qa or {}).get(li),
        })
    return {"version": 1, "title": project.metadata.title, "artist": project.metadata.artist,
            "singers": singers, "lines": lines}


def line_readings(project) -> list[dict]:
    """Ruby segments per line (word grouped) for the stage-1 lyric list."""
    out = []
    for sentence in project.sentences:
        segs: list[dict] = []
        group_text, group_ruby = "", ""
        for ch in sentence.characters:
            r = "".join(_ruby_parts(ch)).replace("^pause^", "")
            group_text += ch.char
            group_ruby += r
            if not ch.linked_to_next:
                segs.append({"t": group_text, "r": group_ruby if group_ruby and group_ruby != group_text else ""})
                group_text, group_ruby = "", ""
        if group_text:
            segs.append({"t": group_text, "r": group_ruby})
        out.append({"text": sentence.text, "segs": segs})
    return out


def _is_kana(text: str) -> bool:
    return bool(text) and all(0x3040 <= ord(c) <= 0x30FF or c in "ー・" for c in text)


def ruby_spans_from_project(project) -> list[list[list]]:
    """Inverse of :func:`line_to_utaten`: per line ``[[s, e, reading]]``.

    A span starts at a non-kana character with a reading and extends over the
    following *linked* non-kana characters (e.g. 記憶 → きおく, Fly → フライ).
    Kana never carries furigana.
    """
    result = []
    for sentence in project.sentences:
        chars = sentence.characters
        spans = []
        i = 0
        while i < len(chars):
            ch = chars[i]
            reading = "".join(_ruby_parts(ch)).replace("^pause^", "")
            if not reading or reading == ch.char or _is_kana(ch.char):
                i += 1
                continue
            end = i
            while chars[end].linked_to_next and end + 1 < len(chars):
                nxt = chars[end + 1]
                if _is_kana(nxt.char) or nxt.char.isspace():
                    break
                reading += "".join(_ruby_parts(nxt)).replace("^pause^", "")
                end += 1
            spans.append([i, end, reading])
            i = end + 1
        result.append(spans)
    return result


# --------------------------------------------------------------------------- align
def ensure_align_model(progress: Optional[Callable[[int, str], None]] = None) -> Path:
    setup()
    from strange_uta_game.backend.application.ai_timing import HfHubTransport, ModelDownloadService, ModelRegistry

    registry = ModelRegistry(models_dir() / "align")
    existing = registry.resolve_model_path(ALIGN_MODEL_ID)
    if existing is not None:
        return Path(existing)
    endpoint = os.environ.get("HF_ENDPOINT", "")
    service = ModelDownloadService(registry, HfHubTransport(endpoint=endpoint))
    return Path(service.download(ALIGN_MODEL_ID, "wav2vec2", license_text=ALIGN_MODEL_LICENSE, progress=progress))


def _shift_sentence(sentence, delta_ms: int) -> None:
    for ch in sentence.characters:
        if ch.timestamps:
            ch.timestamps = [max(0, int(t + delta_ms)) for t in ch.timestamps]
        if ch.sentence_end_ts is not None:
            ch.sentence_end_ts = max(0, int(ch.sentence_end_ts + delta_ms))


def align(project, vocal_wav: str | Path, *, ai_python: str, model_dir: str | Path, device: str = "cpu",
          window: tuple[float, float] | None = None, line_indices: list[int] | None = None,
          on_progress: Optional[Callable[[str, int, str], None]] = None, ffmpeg: str = "ffmpeg") -> dict:
    """Forced-align ``project`` (or a subset of its lines) against ``vocal_wav``.

    ``window`` (seconds) crops the audio first; resulting timestamps are written
    back in absolute media time. Returns a small stats dict.
    """
    setup()
    import subprocess

    from strange_uta_game.backend.application.ai_timing import (
        ApplyAiTimingCommand,
        PronunciationResolver,
        build_alignment_request,
        validate_result,
    )
    from strange_uta_game.backend.application.ai_timing.worker.client import AlignmentWorkerClient
    from strange_uta_game.backend.domain import Project

    progress = on_progress or (lambda *_: None)
    if line_indices is not None:
        sub = Project(metadata=copy.deepcopy(project.metadata))
        sub.singers = copy.deepcopy(project.singers)
        for idx in line_indices:
            sub.sentences.append(copy.deepcopy(project.sentences[idx]))
    else:
        sub = project

    offset_ms = 0
    audio = Path(vocal_wav)
    tmpdir = Path(tempfile.mkdtemp(prefix="km_align_"))
    if window is not None:
        start, end = window
        offset_ms = int(round(start * 1000))
        audio = tmpdir / "window.wav"
        cmd = [ffmpeg, "-y", "-v", "error", "-ss", f"{start:.3f}", "-i", str(vocal_wav), "-t", f"{max(0.1, end - start):.3f}",
               "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(audio)]
        subprocess.run(cmd, check=True, capture_output=True)

    progress("pronounce", 0, "解析注音")
    with _PLAN_LOCK:
        plan = PronunciationResolver().resolve_project(sub, fill_missing=True)
        if plan.generation_errors:
            raise RuntimeError("；".join(plan.generation_errors))
        if plan.pending_units:
            u = plan.pending_units[0]
            raise RuntimeError(f"第 {u.line_idx + 1} 行「{u.char_text}」缺少读音，无法对齐")
        options = {"tail_snap": False, "tail_correct": 0, "tail_silence": 5, "latin_word_split": 1}
        request = build_alignment_request(plan, options=options)
    progress("pronounce", 100, f"{len(request.tokens)} 个对齐单元")

    client = AlignmentWorkerClient(python_exe=ai_python)
    model_spec = {"provider": "wav2vec2", "model_id": str(model_dir), "device": device}
    try:
        result = client.run(request, audio_path=str(audio), model_spec=model_spec,
                            on_progress=lambda st, pct, msg: progress("align", int(pct), msg))
    finally:
        try:
            client.close()
        except Exception:
            pass
    validate_result(result, request)
    with _PLAN_LOCK:
        ApplyAiTimingCommand(sub, plan, request, result).execute()
    if offset_ms:
        for sentence in sub.sentences:
            _shift_sentence(sentence, offset_ms)
    if line_indices is not None:
        for sub_sentence, idx in zip(sub.sentences, line_indices):
            target = project.sentences[idx]
            for dst, src in zip(target.characters, sub_sentence.characters):
                dst.timestamps = list(src.timestamps)
                dst.sentence_end_ts = src.sentence_end_ts
    scores = [s.score for s in result.spans if getattr(s, "score", None) is not None]
    return {"tokens": len(request.tokens), "mean_score": (sum(scores) / len(scores)) if scores else None,
            "token_scores": _line_scores(plan, result, line_indices)}


def _line_scores(plan, result, line_indices) -> dict[int, float]:
    by_token = {s.token_index: s.score for s in result.spans}
    per_line: dict[int, list[float]] = {}
    try:
        units = [u for u in plan.units if getattr(u, "reading", None)]
    except Exception:
        return {}
    for idx, unit in enumerate(units):
        score = by_token.get(idx)
        if score is None:
            continue
        line = unit.line_idx if line_indices is None else line_indices[unit.line_idx]
        per_line.setdefault(line, []).append(float(score))
    return {k: sum(v) / len(v) for k, v in per_line.items()}


# --------------------------------------------------------------------------- refine
def refine_with_energy(project, rms_hop, *, line_indices: Iterable[int] | None = None) -> dict:
    """Line-head / tail correction from the separated vocal's energy.

    * head: if the first checkpoint of a line sits in a quiet stretch, move it
      to the vocal onset inside the first character's span;
    * tail: if a sentence-end checkpoint lingers in silence, pull it back to
      just after the last voiced frame.
    """
    import numpy as np

    rms, hop = rms_hop
    if rms is None or len(rms) == 0:
        return {"heads": 0, "tails": 0, "pauses": 0}
    db = 20 * np.log10(rms + 1e-9)
    floor, peak = np.percentile(db, 20), np.percentile(db, 98)
    thr = floor + (peak - floor) * 0.32
    voiced = db > thr
    n = len(voiced)

    def idx(ms: int) -> int:
        return max(0, min(n - 1, int(ms / 1000.0 / hop)))

    qa_voiced = db > (np.percentile(db, 25) + (np.percentile(db, 97) - np.percentile(db, 25)) * 0.3)  # == QA
    heads = tails = pauses = 0
    targets = set(line_indices) if line_indices is not None else None
    for li, s in enumerate(project.sentences):
        if targets is not None and li not in targets:
            continue
        timed = [c for c in s.characters if c.timestamps]
        if not timed:
            continue
        first = timed[0]
        s0 = first.timestamps[0]
        nxt = first.timestamps[1] if len(first.timestamps) > 1 else (
            timed[1].timestamps[0] if len(timed) > 1 else (first.sentence_end_ts or s0 + 600))
        limit = min(nxt - 60, s0 + 1500)
        a, b = idx(s0), idx(limit)
        if b > a + 3 and not voiced[a:a + 3].all():
            run = 0
            for k in range(a, b):
                run = run + 1 if voiced[k] else 0
                if run >= 3:
                    onset = int((k - 2) * hop * 1000) - 30
                    if onset - s0 >= 80:
                        first.timestamps[0] = onset
                        heads += 1
                    break
        # mid-line breath: a syllable that trails into >= 0.7 s of silence
        # before the next one gets a release checkpoint (SUG "sentence end")
        for ci, ch in enumerate(s.characters):
            if not ch.timestamps or ch.is_sentence_end:
                continue
            nxt = next((c for c in s.characters[ci + 1:] if c.timestamps), None)
            if nxt is None:
                continue
            start_ms, end_ms = ch.timestamps[-1], nxt.timestamps[0]
            if end_ms - start_ms < 1200:
                continue
            a, b = idx(start_ms), idx(end_ms)
            gap = _longest_gap(qa_voiced[a:b + 1])
            if gap is None or (gap[1] - gap[0]) * hop < 0.9:
                continue
            release = int((a + gap[0]) * hop * 1000) + 120
            if release > start_ms + 150:
                ch.is_sentence_end = True
                ch.sentence_end_ts = release
                pauses += 1
        for ch in s.characters:
            if not (ch.is_sentence_end and ch.sentence_end_ts is not None and ch.timestamps):
                continue
            last_cp = ch.timestamps[-1]
            end = ch.sentence_end_ts
            a, b = idx(last_cp + 120), idx(end)
            if b <= a:
                continue
            seg = voiced[a:b + 1]
            if seg.any():
                last_voiced = a + int(np.nonzero(seg)[0][-1])
            else:
                last_voiced = a
            new_end = int(last_voiced * hop * 1000) + 120
            if end - new_end >= 300 and new_end > last_cp + 100:
                ch.sentence_end_ts = new_end
                tails += 1
    return {"heads": heads, "tails": tails, "pauses": pauses}


def apply_word_anchors(project, lines: list[dict], *, line_indices: Iterable[int] | None = None,
                       tol_ms: int = 200) -> dict:
    """Correct the forced alignment with human word-level times (逐字歌词:
    NetEase YRC / Kugou KRC, stage-1 ``line["words"]`` = ``[[first, last, start_s]]``
    character ranges of ``line["text"]``; ``lines[i]`` belongs to sentence ``i``).

    The source's constant offset against this audio is the median difference
    between its word starts and the aligned ones. Words whose aligned start is
    off by more than ``tol_ms`` after removing that offset are moved to the
    source time; every checkpoint of the line is then re-mapped piecewise-linearly
    through the word starts, so syllables inside a moved word keep their shape.
    Nothing is changed when too few words agree (another version of the song).
    """
    import difflib
    import statistics

    targets = set(line_indices) if line_indices is not None else None
    per_line = []
    for li, (sentence, line) in enumerate(zip(project.sentences, lines)):
        words = line.get("words") or []
        if not words or (targets is not None and li not in targets):
            continue
        chars = sentence.characters
        sm = difflib.SequenceMatcher(a=line["text"], b="".join(ch.char for ch in chars), autojunk=False)
        pos = {}
        for a, b, n in sm.get_matching_blocks():
            for k in range(n):
                pos[a + k] = b + k
        knots = []
        for first, last, t, *_dur in words:
            idx = [pos[c] for c in range(int(first), int(last) + 1) if c in pos]
            timed = [k for k in idx if chars[k].timestamps]
            if timed:
                knots.append([chars[timed[0]].timestamps[0], int(round(float(t) * 1000))])
        if knots:
            per_line.append((li, knots))
    deltas = [v - a for _li, knots in per_line for a, v in knots]
    if len(deltas) < 8:
        return {"words": len(deltas), "moved": 0, "lines": [], "skipped": "逐字时间太少"}
    offset = int(statistics.median(deltas))
    agree = sum(1 for d in deltas if abs(d - offset) <= tol_ms) / len(deltas)
    stats = {"words": len(deltas), "offset_ms": offset, "agree": round(agree, 2), "moved": 0, "lines": []}
    if agree < 0.4:
        stats["skipped"] = "逐字歌词与音频大多对不上（可能是另一个版本），未使用"
        return stats
    for li, knots in per_line:
        src, dst = [], []
        moved = 0
        for a, v in knots:
            s = v - offset if abs(v - offset - a) > tol_ms else a
            moved += s != a
            if src and (a <= src[-1] or s <= dst[-1] + 30):
                continue  # keep the map strictly increasing
            src.append(a)
            dst.append(s)
        if not moved or not src:
            continue

        def f(t: int, src=src, dst=dst) -> int:
            if t <= src[0]:
                return t + dst[0] - src[0]
            for k in range(len(src) - 1):
                if src[k] <= t < src[k + 1]:
                    return int(dst[k] + (t - src[k]) * (dst[k + 1] - dst[k]) / (src[k + 1] - src[k]))
            return t + dst[-1] - src[-1]

        for ch in project.sentences[li].characters:
            if ch.timestamps:
                ch.timestamps = [max(0, f(t)) for t in ch.timestamps]
            if ch.sentence_end_ts is not None:
                ch.sentence_end_ts = max(0, f(ch.sentence_end_ts))
        stats["moved"] += moved
        stats["lines"].append(li)
    _enforce_monotonic(project)
    return stats


def _longest_gap(voiced) -> tuple[int, int] | None:
    """(start, end) frame indices of the longest unvoiced run."""
    best = None
    run_start = None
    for k, v in enumerate(list(voiced) + [True]):
        if not v and run_start is None:
            run_start = k
        elif v and run_start is not None:
            if best is None or k - run_start > best[1] - best[0]:
                best = (run_start, k)
            run_start = None
    return best


def auto_release_ms(project, line: int, char: int, rms_hop) -> int | None:
    """Release time for a syllable that trails into silence: last voiced frame
    (QA threshold) between its last checkpoint and the next syllable, + 120 ms."""
    import numpy as np

    rms, hop = rms_hop
    db = 20 * np.log10(rms + 1e-9)
    thr = np.percentile(db, 25) + (np.percentile(db, 97) - np.percentile(db, 25)) * 0.3
    chars = project.sentences[line].characters
    ch = chars[char]
    if not ch.timestamps:
        return None
    start = ch.timestamps[-1]
    nxt = next((c.timestamps[0] for c in chars[char + 1:] if c.timestamps), None)
    end = nxt if nxt is not None else (ch.sentence_end_ts or start + 1000)
    a, b = int(start / 1000 / hop), int(end / 1000 / hop)
    gap = _longest_gap(db[a:b + 1] > thr)
    if gap is None:
        return None
    return max(start + 150, int((a + gap[0]) * hop * 1000) + 120)


# --------------------------------------------------------------------------- edits
def _copy_char_timing(dst, src) -> None:
    """Timing of one character onto its rebuilt counterpart (same text position)."""
    ts = list(src.timestamps or [])
    if len(ts) > dst.check_count:
        dst.set_check_count(len(ts))  # keep every checkpoint of the old timing
    while 0 < len(ts) < dst.check_count:
        ts.append(ts[-1])
    dst.timestamps = ts
    dst.is_sentence_end = src.is_sentence_end
    dst.sentence_end_ts = src.sentence_end_ts
    dst.linked_to_next = src.linked_to_next
    if dst.has_ruby and dst.ruby is not None:
        dst.ruby.timestamps = list(ts)


def _port_line_timing(old, new, prefix: int = 0) -> None:
    """Port a rebuilt line's timing from the old one: characters before the edit
    point keep their times; the rest are spread linearly (by check count, in
    order) over the old tail's [first timestamp, line end]."""
    oc, nc = old.characters, new.characters
    if len(oc) == len(nc):  # equal length: every character keeps its time
        for d, s in zip(nc, oc):
            _copy_char_timing(d, s)
        return
    for k in range(min(prefix, len(oc), len(nc))):
        _copy_char_timing(nc[k], oc[k])
    tail_old = oc[prefix:]
    starts = [t for ch in tail_old for t in (ch.timestamps or [])]
    ends = [ch.sentence_end_ts for ch in oc if ch.is_sentence_end and ch.sentence_end_ts is not None]
    if not starts and not ends:  # untimed line: nothing to port
        return
    if starts:
        t0 = min(starts)
    else:  # pure append at the line end: spread the added characters after the old end
        t0 = max([t for ch in oc for t in (ch.timestamps or [])] + ends)
    t1 = max(starts + ends)
    if t1 <= t0:
        t1 = t0 + 500
    slots = [ch for ch in nc[prefix:] if ch.char.strip()]
    total_w = sum(max(1, ch.check_count) for ch in slots)
    acc = 0
    for ch in slots:
        w = max(1, ch.check_count)
        a = t0 + (t1 - t0) * acc / total_w
        acc += w
        b = t0 + (t1 - t0) * acc / total_w
        n = max(1, ch.check_count)
        ch.timestamps = [int(a + (b - a) * (j + 1) / n) for j in range(n)]
        ch.is_sentence_end = False
        ch.sentence_end_ts = None
    for ch in reversed(nc):
        if ch.char.strip():
            ch.is_sentence_end = True
            ch.sentence_end_ts = t1
            break


def _rebuild_line(project, li: int, text: str, ruby: list) -> None:
    """Replace sentence ``li`` with a fresh build of ``text`` + ``ruby`` spans,
    keeping its id and singer."""
    old = project.sentences[li]
    rebuilt = build_project([{"text": text, "ruby": ruby}]).sentences[0]
    rebuilt.singer_id = old.singer_id
    for ch in rebuilt.characters:
        ch.singer_id = old.singer_id
    rebuilt.id = old.id
    project.sentences[li] = rebuilt
    return old, rebuilt


def apply_edits(project, edits: list[dict]) -> list[str]:
    """Deterministic timing edits used by the review page and by the agent.

    ops: shift_lines {lines, ms} · shift_all {ms} · split_line {line, char} (char = first character of
    the new line; timing kept) · smooth {lines?, strength?=0.5, max_ms?=80}
    (even wipe speed inside phrases) · set_line_span {line, start, end}
    (seconds; rescales the line linearly) · set_char {line, char, cp?, t} ·
    set_line_end {line, t} · set_pause {line, char, t|None} ·
    set_singer {lines, singer (id or name), chars?: [first, last]} (chars = 0-based, inclusive:
    only that part of the line, e.g. a trio entering mid-line) · add_singer {name, color} ·
    set_text {line, text} (equal length → same times; length change → chars before the edit keep
    theirs, the rest spread linearly over the line's old span; ruby spans shift / truncate) ·
    set_ruby {line, ruby: [[start, end, reading]] | inline: "漢字(かな)"} (rebuilds the line;
    timing is kept when the text is unchanged, otherwise realign it afterwards)
    """
    log = []
    sentences = project.sentences
    for ed in edits:
        op = ed.get("op")
        if op == "shift_all":
            for s in sentences:
                _shift_sentence(s, int(ed["ms"]))
            log.append(f"整体平移 {ed['ms']} ms")
        elif op == "shift_lines":
            for li in _line_list(ed, len(sentences)):
                _shift_sentence(sentences[li], int(ed["ms"]))
            log.append(f"第 {_fmt_lines(ed)} 行平移 {ed['ms']} ms")
        elif op == "set_line_span":
            s = sentences[int(ed["line"])]
            times = [t for ch in s.characters for t in ch.timestamps]
            ends = [ch.sentence_end_ts for ch in s.characters if ch.sentence_end_ts is not None]
            if not times:
                # untimed line (e.g. an English line the recognizer missed): distribute the
                # characters evenly over the span — an LRC anchor, not a real alignment
                new_a, new_b = int(float(ed["start"]) * 1000), int(float(ed["end"]) * 1000)
                chars = [ch for ch in s.characters if ch.char.strip()]
                if not chars:
                    continue
                step = max(1, (new_b - new_a) / len(chars))
                for k, ch in enumerate(chars):
                    ch.timestamps = [int(new_a + k * step)]
                    ch.is_sentence_end = False
                    ch.sentence_end_ts = None
                chars[-1].is_sentence_end = True
                chars[-1].sentence_end_ts = new_b
                log.append(f"第 {int(ed['line']) + 1} 行无时间戳，在 {ed['start']}–{ed['end']} 秒内均分（参考歌词锚点）")
                continue
            old_a, old_b = min(times), max(times + ends)
            new_a, new_b = int(float(ed["start"]) * 1000), int(float(ed["end"]) * 1000)
            scale = (new_b - new_a) / max(1, old_b - old_a)
            for ch in s.characters:
                ch.timestamps = [int(new_a + (t - old_a) * scale) for t in ch.timestamps]
                if ch.sentence_end_ts is not None:
                    ch.sentence_end_ts = int(new_a + (ch.sentence_end_ts - old_a) * scale)
            log.append(f"第 {int(ed['line']) + 1} 行时间范围设为 {ed['start']}–{ed['end']} 秒")
        elif op == "set_char":
            ch = sentences[int(ed["line"])].characters[int(ed["char"])]
            cp = int(ed.get("cp", 0))
            ms = int(float(ed["t"]) * 1000)
            if cp < len(ch.timestamps):
                ch.timestamps[cp] = ms
            else:
                ch.add_timestamp(ms, cp)
            log.append(f"第 {int(ed['line']) + 1} 行第 {int(ed['char']) + 1} 字检查点 {cp} 设为 {ed['t']} 秒")
        elif op == "set_line_end":
            s = sentences[int(ed["line"])]
            ms = int(float(ed["t"]) * 1000)
            for ch in reversed(s.characters):
                if ch.is_sentence_end:
                    ch.sentence_end_ts = ms
                    break
            log.append(f"第 {int(ed['line']) + 1} 行结束时间设为 {ed['t']} 秒")
        elif op == "split_line":
            # a line too wide for the screen → two lines, timing kept (spaces at the cut dropped)
            from strange_uta_game.backend.domain.entities import Sentence

            li, at = int(ed["line"]), int(ed["char"])
            s = sentences[li]
            head, tail = list(s.characters[:at]), list(s.characters[at:])
            while head and not head[-1].char.strip():
                head.pop()
            while tail and not tail[0].char.strip():
                tail.pop(0)
            if not head or not tail:
                raise ValueError(f"第 {li + 1} 行无法在第 {at + 1} 字处拆分")
            first = next((t for c in tail for t in c.timestamps), None)
            last = head[-1]
            last.linked_to_next = False
            if not last.is_sentence_end or last.sentence_end_ts is None or (first and last.sentence_end_ts > first):
                last.is_sentence_end = True
                last.sentence_end_ts = first
            s.characters = head
            project.add_sentence(Sentence(singer_id=s.singer_id, characters=tail), after_sentence_id=s.id)
            log.append(f"第 {li + 1} 行在第 {at + 1} 字处拆成两行（时间不变）")
        elif op == "smooth":
            idx = _line_list(ed, len(sentences)) if ("lines" in ed or "line" in ed or "from" in ed) \
                else list(range(len(sentences)))
            k = smooth_wipe(sentences, idx, float(ed.get("strength", 0.5)), int(ed.get("max_ms", 80)))
            log.append(f"平滑走字（强度 {ed.get('strength', 0.5)}，最多 {ed.get('max_ms', 80)} ms）："
                       f"{len(idx)} 行中调整了 {k} 个检查点")
        elif op == "set_pause":
            # sung pause / release after a character (SUG is_sentence_end)
            ch = sentences[int(ed["line"])].characters[int(ed["char"])]
            if ed.get("t") is None:
                ch.is_sentence_end = False
                ch.sentence_end_ts = None
                log.append(f"第 {int(ed['line']) + 1} 行第 {int(ed['char']) + 1} 字取消换气点")
            else:
                ch.is_sentence_end = True
                ch.sentence_end_ts = int(float(ed["t"]) * 1000)
                log.append(f"第 {int(ed['line']) + 1} 行第 {int(ed['char']) + 1} 字在 {ed['t']} 秒收音（换气）")
        elif op == "set_text":
            # replace a line's text (review page ✎). Equal length → same times;
            # otherwise chars before the edit keep theirs, the rest are spread
            # linearly over the old tail; ruby spans shift / truncate with the edit
            li = int(ed["line"])
            old = sentences[li]
            old_text = old.text if hasattr(old, "text") else "".join(c.char for c in old.characters)
            new_text = str(ed["text"])
            if not new_text.strip():
                raise ValueError(f"第 {li + 1} 行文本不能为空")
            if new_text == old_text:
                continue
            spans = ruby_spans_from_project(_OneLine(old))[0]
            p = 0
            while p < min(len(old_text), len(new_text)) and old_text[p] == new_text[p]:
                p += 1
            s = 0
            while s < min(len(old_text) - p, len(new_text) - p) \
                    and old_text[len(old_text) - 1 - s] == new_text[len(new_text) - 1 - s]:
                s += 1
            old_end = len(old_text) - s  # edited region: old [p, old_end) → new [p, len(new_text)-s)
            delta = len(new_text) - len(old_text)
            kept = [[a, b, r] for a, b, r in spans if b < p] + \
                   [[a + delta, b + delta, r] for a, b, r in spans if a >= old_end]
            kept.sort(key=lambda x: x[0])
            dropped = len(spans) - len(kept)
            old_s, rebuilt = _rebuild_line(project, li, new_text, kept)
            _port_line_timing(old_s, rebuilt, prefix=p)
            note = f"第 {li + 1} 行文本已更新（时间轴保留）" if delta == 0 else \
                f"第 {li + 1} 行文本已更新：第 {p + 1} 字起的时间按行内原范围重排（建议复查该行）"
            if dropped:
                note += f"；{dropped} 处注音与改动区域重叠被移除"
            log.append(note)
        elif op == "set_ruby":
            # replace readings of one line; timing is kept when the text is unchanged
            li = int(ed["line"])
            old = sentences[li]
            old_text = old.text if hasattr(old, "text") else "".join(c.char for c in old.characters)
            if "inline" in ed:  # UtaTen-style 漢字(かな) from the review page
                raw = str(ed.get("inline") or "").strip()
                if not raw:
                    new = []
                else:
                    from . import lyrics as _lyrics

                    parsed = _lyrics.parse_user_lyrics(raw)
                    if len(parsed) != 1 or parsed[0]["text"] != old_text:
                        raise ValueError(f"第 {li + 1} 行注音与歌词文本不一致："
                                         f"解析得到「{parsed[0]['text'] if parsed else raw}」")
                    new = [[int(a), int(b), str(r)] for a, b, r in parsed[0]["ruby"]]
            else:
                new = [[int(a), int(b), str(r)] for a, b, r in ed["ruby"]]
            spans = ruby_spans_from_project(_OneLine(old))[0]
            for a, b, _r in new:
                spans = [sp for sp in spans if sp[1] < a or sp[0] > b]
            spans = sorted(spans + [sp for sp in new if sp[2]], key=lambda x: x[0])
            old_s, rebuilt = _rebuild_line(project, li, old_text, spans)
            same_shape = len(rebuilt.characters) == len(old_s.characters) and \
                all(d.char == s.char for d, s in zip(rebuilt.characters, old_s.characters))
            if same_shape:
                for d, s2 in zip(rebuilt.characters, old_s.characters):
                    _copy_char_timing(d, s2)
                log.append(f"第 {li + 1} 行读音已更新（时间轴保留）")
            else:
                log.append(f"第 {li + 1} 行读音已更新（需运行 realign --lines {li + 1}）")
        elif op == "add_singer":
            if not any(sg.name == ed["name"] for sg in project.singers):
                from strange_uta_game.backend.domain.entities import Singer

                project.add_singer(Singer(name=ed["name"], color=ed.get("color") or "#FF6B6B", is_default=False))
                log.append(f"新增演唱者 {ed['name']}")
        elif op == "set_singer":
            target = str(ed["singer"])
            by_name = {sg.name: sg.id for sg in project.singers}
            target = by_name.get(target, target)
            if target not in {sg.id for sg in project.singers}:
                raise ValueError(f"没有这个演唱者：{ed['singer']}（先用 add_singer 添加）")
            rng = ed.get("chars")
            for li in _line_list(ed, len(sentences)):
                chars = sentences[li].characters
                if rng:  # part of the line
                    a, b = int(rng[0]), min(len(chars) - 1, int(rng[1]))
                    for ch in chars[a:b + 1]:
                        ch.singer_id = target
                else:
                    sentences[li].singer_id = target
                    for ch in chars:
                        ch.singer_id = target
            part = f"第 {rng[0] + 1}–{rng[1] + 1} 字" if rng else ""
            log.append(f"第 {_fmt_lines(ed)} 行{part}演唱者改为 {ed['singer']}")
        else:
            raise ValueError(f"未知编辑操作：{op}")
    _enforce_monotonic(project)
    return log


class _OneLine:
    """Minimal project-like wrapper so ruby_spans_from_project works on one sentence."""

    def __init__(self, sentence):
        self.sentences = [sentence]


def smooth_wipe(sentences, indices, strength: float = 0.5, max_ms: int = 80, passes: int = 2) -> int:
    """Even out the wipe speed inside continuous phrases (平滑走字).

    Each inner checkpoint moves toward the time that gives a constant wipe speed between its two
    neighbours (positions in character widths, half width for Latin), ``passes`` times by
    ``strength``. Phrase starts / ends, pauses, gaps over 1 s (rests, held notes) stay where they
    are, and no checkpoint moves more than ``max_ms``. Returns how many checkpoints moved."""
    moved = 0
    for li in indices:
        chars = sentences[li].characters
        pts = []  # [t_ms, x, char index, checkpoint index, phrase]
        x, phrase = 0.0, 0
        for ci, ch in enumerate(chars):
            ts = list(ch.timestamps or [])
            for j, t in enumerate(ts):
                pts.append([float(t), x + j / len(ts), ci, j, phrase])
            x += 0.55 if ord((ch.char or " ")[0]) < 0x2E80 else 1.0
            if ch.is_sentence_end and ch.sentence_end_ts is not None:
                phrase += 1
        groups: list[list[int]] = []
        for k, p in enumerate(pts):
            if not groups or p[4] != pts[k - 1][4] or p[0] - pts[k - 1][0] > 1000:
                groups.append([])
            groups[-1].append(k)
        for g in groups:
            if len(g) < 3:
                continue
            orig = [pts[k][0] for k in g]
            xs = [pts[k][1] for k in g]
            t = list(orig)
            for _ in range(passes):
                new = list(t)
                for i in range(1, len(g) - 1):
                    if xs[i + 1] <= xs[i - 1]:
                        continue
                    target = t[i - 1] + (t[i + 1] - t[i - 1]) * (xs[i] - xs[i - 1]) / (xs[i + 1] - xs[i - 1])
                    new[i] = t[i] + strength * (target - t[i])
                t = new
            for i in range(1, len(g) - 1):
                v = min(max(t[i], orig[i] - max_ms), orig[i] + max_ms)
                v = min(max(v, t[i - 1] + 20), orig[i + 1] - 20)
                t[i] = v
                if abs(v - orig[i]) >= 5:
                    _t, _x, ci, j, _p = pts[g[i]]
                    chars[ci].timestamps[j] = int(round(v))
                    moved += 1
    return moved


def _line_list(ed: dict, n: int) -> list[int]:
    if "lines" in ed:
        return [int(i) for i in ed["lines"] if 0 <= int(i) < n]
    if "from" in ed:
        return list(range(int(ed["from"]), min(n, int(ed.get("to", ed["from"])) + 1)))
    return [int(ed["line"])]


def _fmt_lines(ed: dict) -> str:
    if "lines" in ed:
        return ",".join(str(int(i) + 1) for i in ed["lines"])
    if "from" in ed:
        return f"{int(ed['from']) + 1}-{int(ed.get('to', ed['from'])) + 1}"
    return str(int(ed["line"]) + 1)


def _enforce_monotonic(project) -> None:
    """Keep checkpoints non-decreasing inside every line after edits."""
    for s in project.sentences:
        last = -1
        for ch in s.characters:
            fixed = []
            for t in ch.timestamps:
                t = max(t, last)
                fixed.append(t)
                last = t
            ch.timestamps = fixed
            if ch.sentence_end_ts is not None and ch.sentence_end_ts < last:
                ch.sentence_end_ts = last + 50


# --------------------------------------------------------------------------- export
def export_lyrics(project, path: str | Path, fmt: str = "LRC (逐字)") -> Path:
    setup()
    from strange_uta_game.backend.application.export_service import ExportService

    for s in project.sentences:
        for ch in s.characters:
            ch.set_offset(project.global_offset_ms or 0)
    ExportService().export(project, fmt, str(path))
    return Path(path)
