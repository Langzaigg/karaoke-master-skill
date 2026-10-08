"""Implementation of the ``km.py`` sub commands."""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .jobstore import JobStore, read_json, write_json
from .paths import projects_dir

SINGER_PALETTE = ["#FF5FA2", "#4FC3F7", "#FFD54F", "#81C784", "#B39DDB", "#FF8A65", "#4DD0E1", "#F48FB1"]


def _p(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=1))


def _json_arg(value: str):
    """JSON from the command line, or ``@path`` to read it from a file
    (avoids shell quoting problems, e.g. PowerShell → native exe)."""
    if value.startswith("@"):
        return json.loads(Path(value[1:]).read_text(encoding="utf-8-sig"))
    return json.loads(value)


def _slug(text: str) -> str:
    text = re.sub(r"[\\/:*?\"<>|\s　\[\]［］【】()（）～~]+", "_", text).strip("_")
    return (text or "karaoke")[:48]


def _ranges(spec: str | None, n: int) -> list[int]:
    """'1-3,5' (1-based, inclusive) → [0,1,2,4]."""
    if not spec:
        return []
    out: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if part == "all":
            return list(range(n))
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a) - 1, min(n, int(b))))
        else:
            out.append(int(part) - 1)
    return [i for i in out if 0 <= i < n]


# ===================================================================== status
def status(job: str, full: bool = False) -> dict:
    store = JobStore(job)
    st = store.load()
    if full:
        return st
    lines = st.get("lyrics", {}).get("lines", [])
    summary = {
        "job_dir": str(store.dir),
        "stage": st.get("stage"),
        "status": st.get("status"),
        "status_text": st.get("status_text"),
        "inputs": st.get("inputs"),
        "media": {k: v for k, v in st.get("media", {}).items() if k not in ("activity",)},
        "song": st.get("song"),
        "segment": st.get("segment"),
        "lyrics": {
            "selected": st.get("lyrics", {}).get("selected"),
            "candidates": [{k: c.get(k) for k in ("id", "provider", "title", "artist", "has_ruby", "has_timing", "line_count")}
                           for c in st.get("lyrics", {}).get("candidates", [])],
            "lines": [f"{i + 1:>3} {'✓' if l.get('include', True) else '·'} [{l.get('singer') or '-'}] "
                      f"{'%.2f' % l['match']['score'] if l.get('match') else '    '} "
                      f"{'%7.2f' % l['match']['start'] if l.get('match') and l['match'].get('start') is not None else '       '}  "
                      f"{l['text']}" for i, l in enumerate(lines)],
        },
        "options": st.get("options"),
        "progress": [f"{s['id']}:{s['state']}:{int(s.get('progress', 0) * 100)}%" for s in st.get("progress", {}).get("steps", [])],
        "timing": {k: v for k, v in st.get("timing", {}).items() if k != "qa"},
        "exports": st.get("exports"),
        "pending_user_actions": sum(1 for a in store.pending_actions() if a.get("handled_by", "agent") != "server"),
        "server": read_json(store.dir / "live_preview" / "lock.json"),
    }
    qa = st.get("timing", {}).get("qa_summary")
    if qa:
        summary["qa_summary"] = qa
    return summary


# ======================================================================== new
def cmd_new(args) -> int:
    from . import media

    src = args.source
    path = Path(src)
    is_file = path.is_file()
    base = Path(args.dir) if args.dir else projects_dir()
    name = args.name or _slug(args.title or (path.stem if is_file else src))
    job_dir = base / f"{name}_{time.strftime('%Y%m%d')}"
    k = 2
    while job_dir.exists():  # one folder per project, never reuse another project's folder
        job_dir = base / f"{name}_{time.strftime('%Y%m%d')}_{k}"
        k += 1
    store = JobStore(job_dir)
    inputs = {"source": str(path.resolve()) if is_file else None, "query": None if is_file else src}
    if is_file:
        ext = path.suffix.lower()
        inputs["type"] = "video" if ext in media.VIDEO_EXTS else "audio"
    else:
        inputs["type"] = "title"
    state = store.create(job_dir.name, inputs)
    store.update(lambda st: st["song"].update({k: v for k, v in (("title", args.title), ("artist", args.artist)) if v}))
    if args.out:
        out = Path(args.out).expanduser().resolve()
        out.mkdir(parents=True, exist_ok=True)
        store.update(lambda st: st.setdefault("options", {}).update(output_dir=str(out)))
    store.set_status("已创建任务，正在读取素材", "working")
    if is_file:
        _ingest_media(store, path)
    if args.like:
        _adopt_project(store, Path(args.like))
    else:
        store.set_status("素材已就绪，等待 Agent 分析", "working")
    _p({"job_dir": str(job_dir), "project": job_dir.name, "type": inputs["type"]})
    return 0


_ADOPT_MEDIA = ("vocals", "vocals_from", "instrumental", "vocals_peaks", "activity", "timing_audio", "hires")


def _adopt_project(store: JobStore, other_dir: Path) -> None:
    """``new --like``: same song, another edit (e.g. a different background).
    Copies song info, lyrics, analysis and timing from a project made from the
    same source file, so nothing heavy has to run again."""
    other = JobStore(other_dir)
    ost = other.load()
    st = store.load()
    if (ost.get("inputs") or {}).get("source") != (st.get("inputs") or {}).get("source"):
        raise SystemExit("只能沿用同一素材文件的工程：" + str(other.dir))
    for sub in ("lyrics", "analysis", "timing"):
        src = other.dir / sub
        if src.is_dir():
            shutil.copytree(src, store.dir / sub, dirs_exist_ok=True)
    om = ost.get("media") or {}
    rels = [om.get(k) for k in ("vocals", "instrumental", "vocals_peaks", "timing_audio")]
    rels += [(om.get("hires") or {}).get("on")] + list((om.get("hires") or {}).get("offs") or [])
    for rel in rels:
        if isinstance(rel, str) and rel and not Path(rel).is_absolute() and (other.dir / rel).is_file():
            dst = store.dir / rel
            if not dst.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                try:
                    dst.hardlink_to(other.dir / rel)
                except OSError:
                    shutil.copy2(other.dir / rel, dst)

    def apply(s: dict) -> None:
        for key in ("song", "lyrics", "segment", "analysis", "timing", "progress"):
            if key in ost:
                s[key] = copy.deepcopy(ost[key])
        s["media"].update({k: copy.deepcopy(om[k]) for k in _ADOPT_MEDIA if k in om})
        keep = {k: v for k, v in (ost.get("options") or {}).items() if k not in ("background", "output_dir")}
        s["options"] = {**keep, **(s.get("options") or {})}
        s["stage"] = ost.get("stage", 1)
        s["adopted_from"] = other.dir.name
        s["status"], s["status_text"] = "working", f"已沿用工程「{other.dir.name}」的歌词与时间轴"

    store.update(apply)
    store.log(f"沿用工程 {other.dir.name}：歌曲信息、歌词、分析结果与时间轴")


def _ingest_media(store: JobStore, src: Path) -> None:
    from . import media

    info = media.probe(src)
    m: dict = {"source": {"path": str(src), **{k: info.get(k) for k in (
        "duration", "has_video", "has_audio", "width", "height", "fps", "video_codec", "audio_codec", "sample_rate", "channels")}}}
    m["tags"] = info.get("tags", {})
    audio = media.extract_audio(src, store.path("media", "audio.wav"))
    m["audio"] = store.rel(audio)
    write_json(store.path("media", "peaks.json"), media.peaks(audio))
    m["peaks"] = "media/peaks.json"
    if info.get("has_video"):
        m["thumb"] = store.rel(media.thumbnail(src, store.path("media", "thumb.jpg"), min(30.0, info["duration"] / 3)))
        proxy = media.browser_proxy(src, store.path("media", "preview.mp4"), info)
        m["player"] = store.rel(proxy) if store.dir in proxy.resolve().parents else None
        if m["player"] is None:
            # source already browser friendly: link it into the job (hard link or copy)
            dst = store.path("media", "source" + src.suffix.lower())
            if not dst.exists():
                try:
                    dst.hardlink_to(src)
                except OSError:
                    shutil.copy2(src, dst)
            m["player"] = store.rel(dst)
    else:
        cover = media.extract_cover(src, store.path("media", "cover.jpg"))
        if cover:
            m["cover"] = store.rel(cover)
        m["player"] = store.rel(audio)
        # lossless original inside the job: separation / alignment / renders use it
        # (and nothing is ever written next to the user's own file)
        orig = store.path("media", "source_audio" + src.suffix.lower())
        if not orig.exists():
            try:
                orig.hardlink_to(src)
            except OSError:
                shutil.copy2(src, orig)
        m["timing_audio"] = store.rel(orig)
    store.update(lambda st: st["media"].update(m))
    store.log(f"素材：{src.name}，时长 {info['duration']:.1f} 秒" + ("，含视频" if info.get("has_video") else "，纯音频"))


# ======================================================================== set
def cmd_set(args) -> int:
    store = JobStore(args.job)

    def apply(st: dict) -> None:
        if args.song:
            patch = _json_arg(args.song)
            singers = patch.pop("singers", None)
            st["song"].update(patch)
            if singers is not None:
                for i, sg in enumerate(singers):
                    sg.setdefault("id", f"s{i + 1}")
                    sg.setdefault("color", SINGER_PALETTE[i % len(SINGER_PALETTE)])
                st["song"]["singers"] = singers
        if args.segment:
            a, b = (float(x) for x in args.segment.split(","))
            seg = st.get("segment") or {}
            seg.update({"start": round(a, 2), "end": round(b, 2), "by": "agent"})
            st["segment"] = seg
        if args.options:
            st.setdefault("options", {}).update(_json_arg(args.options))
        if args.stage:
            st["stage"] = args.stage
        if args.status:
            st["status_text"] = args.status
        if args.await_user:
            st["status"] = "awaiting_user"
            st.setdefault("agent", {})["waiting_for"] = f"stage{st.get('stage', 1)}"

    store.update(apply)
    if args.status:
        store.log(args.status)
    return 0


# ==================================================================== analyze
def cmd_analyze(args) -> int:
    from . import analysis, media

    store = JobStore(args.job)
    st = store.load()
    if not st["media"].get("audio"):
        raise SystemExit("任务没有音频素材（纯歌名任务请先提供音频/视频）")
    audio = analysis.timing_audio(store, st)
    store.set_status("正在分离人声（首次运行需下载分离模型）", "working")
    store.step("separate", state="running", progress=0.0, detail="准备分离模型")
    t0 = time.time()

    def sep_cb(p: float, msg: str) -> None:
        store.step("separate", progress=p, detail=msg)

    try:
        vocals, inst = analysis.separate_vocals(audio, progress=sep_cb)
    except Exception as exc:
        store.step("separate", state="error", error=str(exc))
        store.set_status(f"人声分离失败：{exc}", "error")
        raise
    store.step("separate", state="done", detail=f"用时 {time.time() - t0:.0f} 秒")
    activity = media.activity_regions(vocals)
    write_json(store.path("media", "vocals_peaks.json"), media.peaks(vocals))

    def upd(st: dict) -> None:
        st["media"]["vocals"] = store.rel(vocals)
        st["media"]["vocals_from"] = store.rel(audio)
        if inst:
            st["media"]["instrumental"] = store.rel(inst)
        st["media"]["vocals_peaks"] = "media/vocals_peaks.json"
        st["media"]["activity"] = activity

    store.update(upd)
    store.log(f"人声分离完成，检测到 {len(activity)} 段人声活动")
    if args.skip_asr:
        return 0
    store.set_status("正在识别演唱内容（用于确认视频中唱了哪些歌词）", "working")
    out = store.path("analysis", "asr.json")
    t0 = time.time()

    def asr_cb(p: float, msg: str) -> None:
        store.update(lambda st: st.setdefault("analysis", {}).update(asr_progress=round(p, 3), asr_detail=msg))

    lang = (store.load().get("song") or {}).get("language") or "ja"
    analysis.run_asr(vocals, out, model=args.asr_model, language=lang, progress=asr_cb)
    store.update(lambda st: st.setdefault("analysis", {}).update(asr="analysis/asr.json", asr_model=args.asr_model,
                                                                 asr_seconds=round(time.time() - t0, 1)))
    store.set_status("素材分析完成", "working")
    data = read_json(out)
    _p({"vocals": str(vocals), "activity_regions": len(activity), "asr_segments": len(data["segments"]),
        "asr_seconds": round(time.time() - t0, 1)})
    return 0


# ===================================================================== lyrics
def cmd_lyrics_search(args) -> int:
    from . import lyrics

    store = JobStore(args.job)
    st = store.load()
    song = st.get("song", {})
    queries = args.query or [q for q in (
        " ".join(x for x in (song.get("title"), song.get("artist")) if x),
        song.get("title"), st["inputs"].get("query")) if q]
    if not queries:
        raise SystemExit("缺少检索词：先 km.py set --song '{\"title\":..}' 或传 --query")
    store.set_status(f"正在检索歌词：{queries[0]}", "working")
    cands = lyrics.search(queries, utaten_queries=args.utaten_query)
    write_json(store.path("lyrics", "candidates.json"), cands)
    meta = [{k: c[k] for k in c if k != "lines"} | {"preview": [l["text"] for l in c["lines"][:4]]} for c in cands]
    store.update(lambda st: st["lyrics"].update(candidates=meta))
    store.log(f"找到 {len(cands)} 份歌词候选")
    _p([{k: c[k] for k in ("id", "provider", "title", "artist", "duration", "has_ruby", "has_timing", "has_translation", "line_count")} for c in cands])
    return 0


def _annotate(lines: list[dict]) -> None:
    """Fill missing readings with SUG's analyzer (marked "auto")."""
    from . import sugbridge

    todo = [l for l in lines if l["text"].strip()]
    if not todo:
        return
    project = sugbridge.build_project([{"text": l["text"], "ruby": [r[:3] for r in l.get("ruby", [])]} for l in todo])
    spans = sugbridge.ruby_spans_from_project(project)
    for line, new in zip(todo, spans):
        own = {(r[0], r[1]): r for r in line.get("ruby", [])}
        merged = []
        covered = set()
        for r in line.get("ruby", []):
            merged.append(list(r[:3]))
            covered.update(range(r[0], r[1] + 1))
        for s, e, reading in new:
            if (s, e) in own or any(k in covered for k in range(s, e + 1)):
                continue
            merged.append([s, e, reading, "auto"])
        line["ruby"] = sorted(merged, key=lambda r: r[0])


def cmd_lyrics_use(args) -> int:
    from . import lyrics

    store = JobStore(args.job)
    cands = read_json(store.dir / "lyrics" / "candidates.json", [])
    by_id = {c["id"]: c for c in cands}
    if args.candidate in by_id:
        cand = by_id[args.candidate]
        lines = json.loads(json.dumps(cand["lines"]))
        selected = cand["id"]
    else:
        path = Path(args.candidate)
        if not path.is_absolute():
            path = store.dir / path
        lines = lyrics.parse_user_lyrics(path.read_text(encoding="utf-8"))
        selected = "custom"
    note = []
    if args.ruby_from:
        moved = lyrics.merge_ruby(lines, by_id[args.ruby_from]["lines"])
        note.append(f"从 {args.ruby_from} 迁移 {moved} 处注音")
    if args.split_long:
        n = lyrics.auto_split_long(lines, args.split_long)
        if n:
            note.append(f"拆分 {n} 个过长行")
    st = store.load()
    singers = st["song"].get("singers") or []
    name_to_id = {s["name"]: s["id"] for s in singers}
    default_singer = singers[0]["id"] if singers else None
    for line in lines:
        name = line.pop("singer_name", None)
        line["singer"] = name_to_id.get(name, default_singer) if name else (line.get("singer") or default_singer)
        line.setdefault("include", True)
    store.set_status("正在补全注音", "working")
    _annotate(lines)
    store.update(lambda st: st["lyrics"].update(selected=selected, lines=lines, ruby_from=args.ruby_from))
    store.log(f"选用歌词 {selected}，共 {len(lines)} 行" + ("；" + "；".join(note) if note else ""))
    _p({"selected": selected, "lines": len(lines), "notes": note})
    return 0


def cmd_lines(args) -> int:
    from . import lyrics

    store = JobStore(args.job)
    st = store.load()
    lines = st["lyrics"]["lines"]
    changed = []
    for spec in args.split or []:
        li, at = spec.split(":")
        lyrics.split_line(lines, int(li) - 1, int(at))
        changed.append(f"拆分第 {li} 行")
    for spec in sorted(args.merge or [], key=lambda x: -int(x)):
        lyrics.merge_lines(lines, int(spec) - 1)
        changed.append(f"合并第 {spec} 行")
    for spec in sorted(args.dup or [], key=lambda x: -int(x)):
        i = int(spec) - 1
        lines.insert(i + 1, json.loads(json.dumps(lines[i])))
        changed.append(f"复制第 {spec} 行")
    for spec in sorted(args.delete or [], key=lambda x: -int(x)):
        del lines[int(spec) - 1]
        changed.append(f"删除第 {spec} 行")
    n = len(lines)
    if args.include:
        keep = set(_ranges(args.include, n))
        for i, l in enumerate(lines):
            l["include"] = i in keep
        changed.append(f"入选 {len(keep)} 行")
    if args.exclude:
        for i in _ranges(args.exclude, n):
            lines[i]["include"] = False
        changed.append("排除 " + args.exclude)
    for spec in args.singer or []:
        rng, sid = spec.split("=", 1)
        for i in _ranges(rng, n):
            lines[i]["singer"] = sid
        changed.append(f"{rng} 行歌手→{sid}")
    for spec in args.ruby or []:
        head, reading = spec.split("=", 1)
        li, rng = head.split(":")
        a, b = (int(x) for x in rng.split("-")) if "-" in rng else (int(rng), int(rng))
        line = lines[int(li) - 1]
        line["ruby"] = [r for r in line.get("ruby", []) if r[1] < a or r[0] > b]
        if reading:
            line["ruby"].append([a, b, reading])
        line["ruby"].sort(key=lambda r: r[0])
        changed.append(f"第 {li} 行注音修改")
    if args.reannotate:
        for l in lines:
            l["ruby"] = [r for r in l.get("ruby", []) if len(r) < 4]
        _annotate(lines)
        changed.append("重新补全注音")
    if changed:
        store.update(lambda s: s["lyrics"].update(lines=lines))
        store.log("歌词行：" + "，".join(changed))
    if args.list or not changed:
        for i, l in enumerate(lines):
            print(f"{i + 1:>3} {'✓' if l.get('include', True) else '·'} [{l.get('singer') or '-'}] {l['text']}")
    return 0


# ====================================================================== match
def _line_reading(line: dict) -> str:
    from . import analysis

    text = line["text"]
    out = []
    pos = 0
    for s, e, r, *_ in sorted(line.get("ruby", []), key=lambda x: x[0]):
        out.append(text[pos:s])
        out.append(r)
        pos = e + 1
    out.append(text[pos:])
    return analysis.reading_of("".join(out))


def _pick_reference(cands: list[dict], duration: float, ref_id: str | None) -> dict | None:
    timed = [c for c in cands if c.get("has_timing")]
    if ref_id:
        return next((c for c in cands if c["id"] == ref_id), None)
    if not timed:
        return None
    # prefer a synced version whose length matches the material; ties → more lines
    return min(timed, key=lambda c: (abs((c.get("duration") or duration) - duration) > 20, -c["line_count"]))


def cmd_match(args) -> int:
    from . import analysis

    store = JobStore(args.job)
    st = store.load()
    asr = read_json(store.dir / "analysis" / "asr.json")
    if not asr:
        raise SystemExit("还没有语音识别结果：先运行 km.py analyze")
    lines = st["lyrics"]["lines"]
    readings = [_line_reading(l) for l in lines]
    matches = analysis.match_lines(readings, asr)
    duration = st["media"]["source"]["duration"]
    activity = st["media"].get("activity", [])
    cands = read_json(store.dir / "lyrics" / "candidates.json", [])
    ref = _pick_reference(cands, duration, getattr(args, "ref", None))
    if ref is not None:
        ref_readings = [analysis.reading_of(l["text"]) for l in ref["lines"]]
        mapping = analysis.map_to_reference(readings, ref_readings)
        est = analysis.estimate_line_times(matches, mapping, [l.get("t") for l in ref["lines"]], activity, duration)
    else:
        est = [{"est": m.get("start") if m["score"] >= 0.5 else None, "source": "asr" if m.get("start") is not None else None,
                "ref": None, "ref_sim": 0.0} for m in matches]
    for l, m, e in zip(lines, matches, est):
        l["match"] = {**m, **e}
        note = None
        if e["est"] is None:
            if ref is not None and e["ref"] is None:
                note = "参考歌词中没有这一行（可能是括号和声 / 歌词本独有）"
            else:
                note = "音频中未找到"
        l["match"]["note"] = note
        l["suggest"] = e["est"] is not None
    times = [e["est"] for e in est if e["est"] is not None]
    if times:
        first, last = min(times), max(times)
        end = last + 6.0
        for a, b in sorted(activity):  # follow the vocal activity that continues the last line
            if a <= end + 3.0 and b >= last - 1.0 and a <= last + 15.0:
                end = max(end, b)
        seg = {"start": round(max(0.0, first - 1.5), 2), "end": round(min(duration, end + 1.5), 2),
               "confidence": round(sum(1 for e in est if e["source"] == "asr") / max(1, len(est)), 2)}
    else:
        seg = {"start": 0.0, "end": round(duration, 2), "confidence": 0.0}
    if args.apply:
        for l in lines:
            l["include"] = l["suggest"]

    def upd(s: dict) -> None:
        s["lyrics"]["lines"] = lines
        s["lyrics"]["reference"] = ref["id"] if ref else None
        cur = s.get("segment") or {}
        if not cur or cur.get("by") != "user":
            s["segment"] = {**seg, "by": "match"}

    store.update(upd)
    n_asr = sum(1 for e in est if e["source"] == "asr")
    n_lrc = sum(1 for e in est if e["source"] == "lrc")
    store.log(f"歌词定位：{n_asr} 行由语音识别确认，{n_lrc} 行由参考时间轴推算，"
              f"{len(lines) - n_asr - n_lrc} 行未出现；建议区间 {seg['start']:.1f}–{seg['end']:.1f} 秒")
    for i, (l, e) in enumerate(zip(lines, est)):
        t = f"{e['est']:7.2f}" if e["est"] is not None else "      -"
        print(f"{i + 1:>3} {'✓' if l['suggest'] else '·'} {l['match']['score']:.2f} {t} {e['source'] or '-':>3} "
              f"ref={'-' if e['ref'] is None else e['ref'] + 1}  {l['match'].get('note') or ''}")
    _p({"segment": seg, "reference": ref["id"] if ref else None, "asr_confirmed": n_asr, "lrc_estimated": n_lrc,
        "not_found": len(lines) - n_asr - n_lrc})
    return 0


# ===================================================================== timing
def _timing_lines(st: dict) -> tuple[list[dict], list[dict]]:
    lines = [l for l in st["lyrics"]["lines"] if l.get("include", True) and l["text"].strip()]
    singers = st["song"].get("singers") or []
    return lines, singers


def _save_view(store: JobStore, project, qa: dict | None = None) -> dict:
    from . import sugbridge

    view = sugbridge.project_view(project, qa=qa)
    write_json(store.path("timing", "timed.json"), view)
    return view


def _run_qa(store: JobStore, project, line_scores: dict | None = None) -> dict:
    from . import analysis, media, sugbridge

    st = store.load()
    view = sugbridge.project_view(project)
    rms = None
    if st["media"].get("vocals"):
        rms = media.rms_envelope(store.abs(st["media"]["vocals"]))
    asr_m = {}
    inc = [l for l in st["lyrics"]["lines"] if l.get("include", True) and l["text"].strip()]
    for i, l in enumerate(inc):
        if l.get("match"):
            asr_m[i] = l["match"]
    old = st.get("timing", {}).get("line_scores") or {}
    scores = {int(k): v for k, v in old.items()}
    scores.update(line_scores or {})
    qa = analysis.timing_qa(view, vocal_rms=rms, asr_matches=asr_m, line_scores=scores)
    view = _save_view(store, project, qa)
    summary = {"ok": sum(1 for q in qa.values() if q["flag"] == "ok"),
               "warn": sum(1 for q in qa.values() if q["flag"] == "warn"),
               "bad": sum(1 for q in qa.values() if q["flag"] == "bad")}
    store.update(lambda s: s["timing"].update(qa_summary=summary, line_scores={str(k): v for k, v in scores.items()},
                                              view="timing/timed.json", revision=int(time.time())))
    from . import weblayout

    weblayout.refresh(store)  # engine-exact overlay for the review page
    return {"summary": summary, "qa": qa, "view": view}


def _align_blocks(lines: list[dict], window: tuple[float, float], activity: list[list[float]] | None = None,
                  *, max_span: float = 95.0) -> list[tuple[list[int], tuple[float, float]]]:
    """Split the included lines into alignment blocks at instrumental breaks.

    Uses the stage-1 estimates (``match.est``) and the vocal-activity map: a
    block boundary is placed just before the vocal onset that follows a real
    gap (>= 1 s without voice) between two consecutive lines. Windows do not
    overlap, so a block's first line cannot be pulled into the previous
    line's tail and an error cannot drift across a break.
    """
    est = [((l.get("match") or {}).get("est")) for l in lines]
    if not activity or sum(1 for e in est if e is not None) < max(3, len(lines) // 2):
        return [(list(range(len(lines))), window)]
    known = [(i, e) for i, e in enumerate(est) if e is not None]
    for i in range(len(est)):
        if est[i] is None:
            prev = max((k for k in known if k[0] < i), default=None, key=lambda k: k[0])
            nxt = min((k for k in known if k[0] > i), default=None, key=lambda k: k[0])
            if prev and nxt:
                est[i] = prev[1] + (nxt[1] - prev[1]) * (i - prev[0]) / (nxt[0] - prev[0])
            else:
                est[i] = (prev or nxt)[1]
    acts = sorted(activity)
    cuts: list[tuple[int, float]] = []  # (first line index of new block, boundary time)
    for i in range(1, len(lines)):
        best = None
        for (a0, b0), (a1, b1) in zip(acts, acts[1:]):
            if b0 >= est[i - 1] + 0.8 and a1 <= est[i] + 0.6 and a1 - b0 >= 1.0:
                best = a1
        if best is not None:
            cuts.append((i, max(est[i - 1] + 0.8, best - 0.35)))
    groups: list[tuple[int, int]] = []
    starts = [0] + [c[0] for c in cuts]
    ends = [c[0] for c in cuts] + [len(lines)]
    bounds = [window[0]] + [c[1] for c in cuts] + [window[1]]
    blocks = []
    for k, (a, b) in enumerate(zip(starts, ends)):
        blocks.append((list(range(a, b)), (round(bounds[k], 2), round(bounds[k + 1], 2))))
    # very long blocks: split once more at the largest estimate gap
    out = []
    for idx, (w0, w1) in blocks:
        if w1 - w0 > max_span and len(idx) > 4:
            j = max(range(1, len(idx)), key=lambda k: est[idx[k]] - est[idx[k - 1]])
            mid = est[idx[j]] - 0.5
            out.append((idx[:j], (w0, round(mid, 2))))
            out.append((idx[j:], (round(mid, 2), w1)))
        else:
            out.append((idx, (w0, w1)))
    return out


def cmd_timing(args) -> int:
    from . import analysis, media, sugbridge
    from .paths import ffmpeg_exe

    store = JobStore(args.job)
    st = store.load()
    store.reset_steps()
    store.set_status("开始自动打轴", "working", stage=2)
    try:
        # 1 prepare ------------------------------------------------------
        store.step("prepare", state="running", detail="检查素材与区间")
        seg = st.get("segment") or {}
        duration = st["media"]["source"]["duration"]
        window = (float(seg.get("start", 0.0)), float(seg.get("end", duration)))
        store.step("prepare", state="done", detail=f"歌曲区间 {window[0]:.1f}–{window[1]:.1f} 秒")
        # 2 separate -----------------------------------------------------
        t_audio = analysis.timing_audio(store, st)
        same_source = st["media"].get("vocals_from", st["media"].get("audio")) == store.rel(t_audio)
        if st["media"].get("vocals") and store.abs(st["media"]["vocals"]).is_file() and same_source:
            store.step("separate", state="done", detail="复用阶段一的分离结果")
            vocals = store.abs(st["media"]["vocals"])
        else:
            hires_note = "（Hi-Res 音源）" if (st["media"].get("hires") or {}).get("on") else ""
            store.step("separate", state="running", detail="分离人声" + hires_note)
            vocals, inst = analysis.separate_vocals(t_audio,
                                                    progress=lambda p, m: store.step("separate", progress=p, detail=m))

            def upd_vocals(s: dict) -> None:
                s["media"].update(vocals=store.rel(vocals), vocals_from=store.rel(t_audio))
                if inst:
                    s["media"]["instrumental"] = store.rel(inst)

            store.update(upd_vocals)
            store.step("separate", state="done", detail="已用" + (hires_note.strip("（）") or "素材音频") + "重新分离")
        # 3 pronounce ----------------------------------------------------
        store.step("pronounce", state="running", detail="构建 SUG 工程并补全注音")
        lines, singers = _timing_lines(st)
        if not lines:
            raise RuntimeError("没有入选的歌词行")
        meta = {"title": st["song"].get("title"), "artist": st["song"].get("artist"), "work": st["song"].get("work")}
        project = sugbridge.build_project([{"text": l["text"], "ruby": [r[:3] for r in l.get("ruby", [])],
                                            "singer": l.get("singer")} for l in lines], singers=singers, meta=meta)
        sug_path = store.path("timing", "project.sug")
        sugbridge.save_project(project, sug_path)
        _save_view(store, project)
        store.step("pronounce", state="done", detail=f"{len(lines)} 行")
        # 4 align ---------------------------------------------------------
        store.step("align", state="running", detail="准备对齐模型")
        model_dir = sugbridge.ensure_align_model(lambda pct, msg: store.step("align", progress=pct / 1000.0, detail=f"下载对齐模型 {msg}"))
        t0 = time.time()
        last = [0.0]

        def on_prog(stage: str, pct: int, msg: str) -> None:
            if time.time() - last[0] < 0.4 and pct < 100:
                return
            last[0] = time.time()
            elapsed = time.time() - t0
            eta = elapsed / max(0.01, pct / 100.0) - elapsed if pct > 3 else None
            store.step("align", progress=pct / 100.0, detail=msg, eta=eta)

        blocks = ([(list(range(len(lines))), window)] if args.no_chunk
                  else _align_blocks(lines, window, st["media"].get("activity")))
        from . import accel

        device = args.device or accel.profile()["align_device"]
        store.log(f"对齐设备：{device}")
        stats = {"tokens": 0, "token_scores": {}, "mean_score": None}
        means = []
        # SUG's worker loads the model once per process and exits after one request,
        # so blocks run in parallel workers to overlap model loading (disjoint lines).
        # parallel workers are opt-in (align_jobs): several model processes at once can overload a machine
        jobs = int(accel.profile().get("align_jobs") or 1)
        jobs = max(1, min(jobs, len(blocks)))
        block_pct = [0.0] * len(blocks)
        prog_lock = threading.Lock()

        def run_block(bi: int):
            idx, win = blocks[bi]

            def on_block(stage: str, pct: int, msg: str) -> None:
                with prog_lock:
                    block_pct[bi] = pct / 100.0
                    overall = sum(block_pct) / len(blocks) * 100
                on_prog(stage, int(overall), f"第 {bi + 1}/{len(blocks)} 段（{win[0]:.0f}–{win[1]:.0f} 秒）：{msg}")

            dev = device
            try:
                return sugbridge.align(project, vocals, ai_python=str(analysis.ai_python()), model_dir=model_dir,
                                       device=dev, window=win, on_progress=on_block, ffmpeg=ffmpeg_exe(),
                                       line_indices=None if len(blocks) == 1 else idx)
            except Exception as exc:
                if dev == "cpu":
                    raise
                store.log(f"{dev} 推理失败（{exc}），第 {bi + 1} 段改用 CPU 重试")
                return sugbridge.align(project, vocals, ai_python=str(analysis.ai_python()), model_dir=model_dir,
                                       device="cpu", window=win, on_progress=on_block, ffmpeg=ffmpeg_exe(),
                                       line_indices=None if len(blocks) == 1 else idx)

        store.log(f"对齐分 {len(blocks)} 段，并行 {jobs} 个进程")
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            futures = {pool.submit(run_block, bi): bi for bi in range(len(blocks))}
            for fut in as_completed(futures):
                bi = futures[fut]
                res = fut.result()
                idx = blocks[bi][0]
                stats["tokens"] += res["tokens"]
                stats["token_scores"].update(res.get("token_scores") or {})
                if res.get("mean_score") is not None:
                    means.append(res["mean_score"])
                _save_view(store, project)  # live timeline in the page
                store.log(f"对齐第 {bi + 1}/{len(blocks)} 段完成：第 {idx[0] + 1}-{idx[-1] + 1} 行")
        stats["mean_score"] = sum(means) / len(means) if means else None
        sugbridge.save_project(project, sug_path)
        _save_view(store, project)
        store.step("align", state="done", detail=f"{stats['tokens']} 个单元，用时 {time.time() - t0:.0f} 秒")
        # 5 refine ---------------------------------------------------------
        store.step("refine", state="running", detail="按人声能量修正行首起唱点与尾音")
        rms = media.rms_envelope(vocals)
        fixed = sugbridge.refine_with_energy(project, rms)
        sugbridge.apply_edits(project, [])  # enforces monotonic checkpoints
        sugbridge.save_project(project, sug_path)
        store.step("refine", state="done",
                   detail=f"修正行首 {fixed['heads']} 处，尾音 {fixed['tails']} 处，句中换气 {fixed['pauses']} 处")
        # 6 qa -------------------------------------------------------------
        store.step("qa", state="running", detail="逐行质检")
        res = _run_qa(store, project, stats.get("token_scores"))
        s = res["summary"]
        store.step("qa", state="running", progress=0.5,
                   detail=f"正常 {s['ok']} · 需注意 {s['warn']} · 疑似错误 {s['bad']}（等待 Agent 复核）")
        store.update(lambda st2: st2["timing"].update(sug="timing/project.sug", aligned_at=time.time(),
                                                      mean_score=stats.get("mean_score")))
        store.set_status("对齐完成，Agent 正在复核", "working")
        _p({"tokens": stats["tokens"], "qa": s, "bad_lines": [i + 1 for i, q in res["qa"].items() if q["flag"] == "bad"],
            "warn_lines": [i + 1 for i, q in res["qa"].items() if q["flag"] == "warn"]})
        return 0
    except Exception as exc:
        for step in store.load()["progress"]["steps"]:
            if step["state"] == "running":
                store.step(step["id"], state="error", error=str(exc))
        store.set_status(f"打轴失败：{exc}", "error")
        traceback.print_exc()
        return 1


def cmd_realign(args) -> int:
    from . import analysis, sugbridge
    from .paths import ffmpeg_exe

    store = JobStore(args.job)
    st = store.load()
    project = sugbridge.load_project(store.dir / "timing" / "project.sug")
    n = len(project.sentences)
    idx = _ranges(args.lines, n)
    if not idx:
        raise SystemExit("行号无效")
    if args.window:
        a, b = (float(x) for x in args.window.split(","))
    else:
        view = sugbridge.project_view(project)
        prev_end = next((view["lines"][i]["end"] for i in range(idx[0] - 1, -1, -1) if view["lines"][i]["end"]), 0.0)
        nxt = next((view["lines"][i]["start"] for i in range(idx[-1] + 1, n) if view["lines"][i]["start"]), None)
        a = max(0.0, (prev_end or 0.0) - 0.3)
        seg_end = (st.get("segment") or {}).get("end") or st["media"]["source"]["duration"]
        b = (nxt + 0.3) if nxt else float(seg_end)
    store.set_status(f"重新对齐第 {idx[0] + 1}-{idx[-1] + 1} 行（{a:.1f}–{b:.1f} 秒）", "working")
    model_dir = sugbridge.ensure_align_model()
    from . import accel

    stats = sugbridge.align(project, store.abs(st["media"]["vocals"]), ai_python=str(analysis.ai_python()),
                            model_dir=model_dir, device=args.device or accel.profile()["align_device"],
                            window=(a, b), line_indices=idx,
                            ffmpeg=ffmpeg_exe())
    from . import media

    sugbridge.refine_with_energy(project, media.rms_envelope(store.abs(st["media"]["vocals"])), line_indices=idx)
    sugbridge.apply_edits(project, [])
    sugbridge.save_project(project, store.dir / "timing" / "project.sug")
    res = _run_qa(store, project, stats.get("token_scores"))
    store.log(f"重新对齐第 {idx[0] + 1}-{idx[-1] + 1} 行完成")
    _p({"lines": [i + 1 for i in idx], "window": [a, b],
        "qa": {i + 1: res["qa"][i] for i in idx}, "summary": res["summary"]})
    return 0


def cmd_edit(args) -> int:
    from . import sugbridge

    store = JobStore(args.job)
    project = sugbridge.load_project(store.dir / "timing" / "project.sug")
    ops = _json_arg(args.ops)
    if isinstance(ops, dict):
        ops = [ops]
    ops = _resolve_auto_ops(store, project, ops)
    log = sugbridge.apply_edits(project, ops)
    sugbridge.save_project(project, store.dir / "timing" / "project.sug")
    res = _run_qa(store, project)
    for line in log:
        store.log("编辑：" + line)
    _p({"applied": log, "summary": res["summary"]})
    return 0


def _resolve_auto_ops(store: JobStore, project, ops: list[dict]) -> list[dict]:
    """``set_pause`` with ``"t": "auto"`` → release time from the vocal energy."""
    if not any(o.get("op") == "set_pause" and o.get("t") == "auto" for o in ops):
        return ops
    from . import media, sugbridge

    st = store.load()
    rms = media.rms_envelope(store.abs(st["media"]["vocals"]))
    out = []
    for o in ops:
        if o.get("op") == "set_pause" and o.get("t") == "auto":
            ms = sugbridge.auto_release_ms(project, int(o["line"]), int(o["char"]), rms)
            o = dict(o, t=None if ms is None else ms / 1000.0)
        out.append(o)
    return out


def cmd_qa(args) -> int:
    store = JobStore(args.job)
    view = read_json(store.dir / "timing" / "timed.json")
    if not view:
        raise SystemExit("尚未打轴")
    for line in view["lines"]:
        q = line.get("qa") or {}
        flag = {"ok": " ", "warn": "!", "bad": "✗"}.get(q.get("flag"), "?")
        t = f"{line['start']:7.2f}-{line['end']:7.2f}" if line["start"] is not None else "   -   "
        print(f"{line['i'] + 1:>3} {flag} {t} {line['text'][:28]:<28} {'；'.join(q.get('notes', []))}")
    return 0


# ============================================================ render helpers
def cmd_previews(args) -> int:
    from . import render

    return render.cmd_previews(JobStore(args.job), args.only)


def cmd_style(args) -> int:
    from . import render

    store = JobStore(args.job)
    patch = _json_arg(args.patch)
    background = patch.pop("background", None)
    if background is not None:  # validates a video path, prepares AMV / montage stills
        set_background(store, background)

    def apply(st: dict) -> None:
        opts = st.setdefault("options", {})
        for key, value in patch.items():
            if key in ("overrides", "singer_styles") and isinstance(value, dict):
                opts.setdefault(key, {}).update(value)
            else:
                opts[key] = value

    store.update(apply)
    if patch:
        store.log("样式更新：" + json.dumps(patch, ensure_ascii=False))
    if not args.no_preview:
        render.write_web_style(store)
    return 0


def cmd_frame(args) -> int:
    from . import render

    store = JobStore(args.job)
    out = Path(args.out) if args.out else store.path("previews", f"frame_{int(args.t * 1000)}.png")
    render.render_job_frame(store, args.t, out)
    _p({"frame": str(out)})
    return 0


def cmd_export(args) -> int:
    from . import render

    store = JobStore(args.job)
    if args.cast_delay is not None:  # persisted: on / off vocal and Hi-Res redone later use it too
        store.update(lambda s: s.setdefault("options", {}).update(cast_delay_ms=int(args.cast_delay)))
        store.log(f"投屏延迟设为 {int(args.cast_delay):+d} ms（画面比声音提前）")
    return render.cmd_export(store, [k.strip() for k in args.kinds.split(",") if k.strip()], clip=args.clip)


def cmd_mv(args) -> int:
    """Generated backgrounds: AMV (``mv``) and image montage (``montage``) designs."""
    from . import mv

    store = JobStore(args.job)
    if args.list_presets:
        _p(mv.catalog(args.kind))
        return 0
    kind = args.kind
    if args.preset:
        spec = mv.preset_spec(args.preset)
        kind = mv.save_spec(store, spec, kind)
        store.log(f"{mv.KIND_LABEL[kind]}设计：采用预设「{spec['name']}」")
    if args.spec:
        spec = _json_arg(args.spec)
        if spec.get("preset") and not spec.get("layers"):
            spec = {**mv.preset_spec(spec["preset"]), **spec}
        kind = mv.save_spec(store, spec, kind)
        store.log(f"{mv.KIND_LABEL[kind]}设计：已写入自定义场景（" + str(spec.get("theme") or spec.get("preset") or "") + "）")
    kind = kind or mv.design_kind(store.load())
    if args.show:
        _p({"kind": kind, **mv.load_spec(store, kind)})
    if args.gallery:
        _p(mv.render_gallery(store, kind))
    if args.stills or args.preset or args.spec:
        stills = mv.render_stills(store, kind=kind)
        _p({"kind": kind, "stills": stills} if stills else
           {"kind": kind, "stills": [], "note": "素材池是空的：先导入图包（mv-assets --add）或搜集图片"})
    if args.video or args.seconds:
        out = mv.render_video(store, kind=kind, seconds=args.seconds)
        _p({"kind": kind, "video": str(out)})
    return 0


def set_background(store: JobStore, background: dict) -> dict:
    """Stage-1 background choice (also writable later with ``style``): source /
    video / mv / montage / subs. Prepares the design stills the page shows."""
    from . import mv

    bg = {k: v for k, v in (background or {}).items() if v is not None}
    kind = bg.get("type")
    if kind == "video":
        p = Path(str(bg.get("path") or "").strip().strip('"'))
        if not p.is_file():
            raise SystemExit(f"找不到视频文件：{p}")
        bg["path"] = str(p)
    store.update(lambda s: s.setdefault("options", {}).update(background=bg))
    from .render import BACKGROUND_TYPES

    store.log(f"背景改为：{BACKGROUND_TYPES.get(kind, kind)}")
    if kind in ("mv", "montage"):
        st = store.load()
        if not ((st.get("previews") or {}).get("designs") or {}).get(kind):
            mv.render_stills(store, kind=kind)
        if kind == "montage":
            mv.refresh_montage_plan(store)
    return bg


def set_hires_source(store: JobStore, on: str | None, offs: list[str] | None, *, align: bool = True) -> dict:
    """Register (or clear) the stage-1 Hi-Res source; stage 2 then separates / aligns on it."""
    from . import tracks

    on = (on or "").strip().strip('"') or None
    offs = [o.strip().strip('"') for o in (offs or []) if o and o.strip()]
    if not on and not offs:
        def clear(s: dict) -> None:
            s["media"].pop("hires", None)
            src = next((p for p in (store.dir / "media").glob("source_audio.*")), None)
            if src is not None:
                s["media"]["timing_audio"] = store.rel(src)
            else:
                s["media"].pop("timing_audio", None)
            s.setdefault("options", {})["hires"] = {"on": None, "off": []}

        store.update(clear)
        store.log("已取消 Hi-Res 音源，打轴改回使用素材音频")
        return {"on": None, "offs": [], "notes": ["已取消 Hi-Res 音源"]}
    store.set_status("正在对齐 Hi-Res 音源", "working")
    res = tracks.register_hires(store, on, offs, align=align)
    store.set_status("Hi-Res 音源已登记：打轴将使用它分离人声" if res.get("on") else "Hi-Res 音源未能登记", None)
    return res


def cmd_hires_source(args) -> int:
    store = JobStore(args.job)
    _p(set_hires_source(store, None if args.clear else args.on, None if args.clear else args.off,
                        align=not args.no_align))
    return 0


def cmd_mv_assets(args) -> int:
    """Image pool for the MV montage."""
    from . import mv_assets

    store = JobStore(args.job)
    out: dict = {}
    if args.add:
        tags = [t.strip() for t in (args.tags or "").split(",") if t.strip()]
        res = mv_assets.add_many(store, args.add, origin=args.origin, source=args.source, credit=args.credit,
                                 tags=tags or None)
        ok = sum(1 for r in res if r.get("ok"))
        out["added"] = {"ok": ok, "rejected": [r for r in res if not r.get("ok")]}
        store.log(f"混剪素材：新增 {ok} 张（共 {len(res)} 张图片）")
    if args.from_video:
        res = mv_assets.from_video(store, args.from_video)
        out["from_video"] = {"added": sum(1 for r in res if r.get("ok")), "skipped": sum(1 for r in res if not r.get("ok"))}
    if args.remove:
        out["removed"] = mv_assets.remove(store, args.remove)
    if args.credits:
        out["credits"] = mv_assets.credits(store)
    if args.plan:
        from . import mv

        if mv.montage_layer(mv.load_spec(store, "montage")) is None:
            out["plan"] = {"note": "图片混剪设计里没有 montage 图层（km.py mv <job> --preset anime_montage）"}
        else:
            plan = mv.refresh_montage_plan(store)
            out["plan"] = {k: plan[k] for k in ("bpm", "bars_per_shot", "pool", "unique_used", "ideal_unique",
                                                "repeated_sections", "fallback_repeats", "by_origin", "note")}
            out["plan"]["shots"] = len(plan["shots"])
            out["plan"]["needed_unique"] = sum(1 for s in plan["shots"] if s.get("why") != "repeat")
            out["plan"]["repeats"] = plan["repeats"]
    if args.list or not out:
        out["pool"] = [{"origin": mv_assets.origin_of(a), **{k: a.get(k) for k in ("id", "name", "w", "h", "credit",
                                                                                  "source", "tags")}}
                       for a in mv_assets.load_pool(store)]
    _p(out)
    return 0


def cmd_mv_video(args) -> int:
    """MV background downloaded from YouTube / Bilibili (Lin-K Lyrics' video downloader)."""
    from . import mvvideo

    store = JobStore(args.job)
    out: dict = {}
    if args.search:
        items = mvvideo.search(args.search, args.n)
        mvvideo.set_candidates(store, args.search, items)
        out["candidates"] = [{"k": k + 1, **{x: it[x] for x in ("title", "uploader", "duration", "views", "url")}}
                             for k, it in enumerate(items)]
    target = args.info or args.use
    if target and target.startswith("#"):  # "#2" = second search result
        items = ((store.load().get("previews") or {}).get("mv_video_candidates") or {}).get("items") or []
        target = items[int(target[1:]) - 1]["url"]
    if args.info:
        out["info"] = mvvideo.info(target, args.max_height)
    if args.use:
        out["use"] = mvvideo.use(store, target, max_height=args.max_height)
    if args.local:
        p = Path(args.local.strip().strip('"'))
        if not p.is_file():
            raise SystemExit(f"找不到视频文件：{p}")
        store.update(lambda s: s["media"].update(mv_video={"file": str(p), "title": p.stem, "source": "本地文件"}))
        out["use"] = mvvideo.align(store, p)
    _p(out)
    return 0


def cmd_versions(args) -> int:
    from . import versions

    store = JobStore(args.job)
    outs = versions.cmd_versions(store, video=args.video, on=args.on, offs=args.off, align=not args.no_align)
    entry = next((e for e in store.load().get("exports", []) if e["kind"] == "onoff"), {})
    _p({"outputs": [str(p) for p in outs], "notes": entry.get("notes", [])})
    return 0


def cmd_hires(args) -> int:
    from . import hires

    store = JobStore(args.job)
    try:
        outs = hires.cmd_hires(store, on=args.on, offs=args.off, video=args.video, align=not args.no_align)
    except BaseException as exc:
        from . import render

        render._record_export(store, "hires", state="error", label="Hi-Res 混流（MKV）", path="", error=str(exc))
        store.set_status(f"Hi-Res 混流失败：{exc}", "error")
        raise
    st = store.load()
    entry = next((e for e in st.get("exports", []) if e["kind"] == "hires"), {})
    _p({"outputs": [str(p) for p in outs], "notes": entry.get("notes", [])})
    return 0


def open_app(store: JobStore, which: str | None = None, exe: str | None = None) -> dict:
    """Open the job's result in the Lin-K Lyrics GUI (the main project).

    ``which``: ``yurika`` (step 5 subtitle project, default when exported),
    ``sug`` (step 4 timing project) or a file path. ``exe`` / ``KM_LINK_EXE``
    points to an installed ``Lin-K Lyrics.exe``; otherwise the engine checkout
    is started from source with the skill venv (same dependencies).
    """
    import os
    import subprocess
    import sys

    from .paths import REPO_ROOT

    st = store.load()
    files = {e["kind"]: store.abs(e["path"]) for e in st.get("exports", []) if e.get("state") == "done" and e.get("path")}
    if which and which not in ("yurika", "sug"):
        target = Path(which)
    elif which == "sug" or (not which and "yurika" not in files):
        target = files.get("sug") or (store.dir / "timing" / "project.sug")
    else:
        target = files.get("yurika")
    if target is None or not Path(target).is_file():
        raise SystemExit("没有可打开的工程：先导出 .sug / .yurika（km.py export <job> --kinds sug,yurika）")
    exe = exe or os.environ.get("KM_LINK_EXE")
    if exe:
        cmd = [exe, str(target)]
    else:
        if REPO_ROOT is None:
            raise SystemExit("找不到 Lin-K Lyrics 引擎，先运行 km.py setup")
        cmd = [sys.executable, str(REPO_ROOT / "app.py"), str(target)]
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    env = {k: v for k, v in os.environ.items() if k != "QT_QPA_PLATFORM"}
    subprocess.Popen(cmd, cwd=str(REPO_ROOT or Path(target).parent), env=env, creationflags=flags,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    store.log(f"已在 Lin-K Lyrics 中打开：{Path(target).name}")
    return {"opened": str(target), "command": cmd}


def cmd_open_app(args) -> int:
    _p(open_app(JobStore(args.job), args.file, args.exe))
    return 0


def apply_stage1(store: JobStore, payload: dict) -> None:
    """Write the user's stage-1 decisions (from the page) into job.json."""
    def apply(st: dict) -> None:
        song = dict(payload.get("song") or {})
        singers = song.pop("singers", None)
        st["song"].update({k: v for k, v in song.items() if k not in ("confidence", "notes", "sources")})
        if singers:
            st["song"]["singers"] = singers
        seg = payload.get("segment")
        if seg and seg.get("start") is not None:
            prev = st.get("segment") or {}
            moved = abs(prev.get("start", -1) - seg["start"]) > 0.05 or abs(prev.get("end", -1) - seg["end"]) > 0.05
            st["segment"] = {**prev, "start": round(seg["start"], 2), "end": round(seg["end"], 2),
                             "by": "user" if moved else prev.get("by", "user")}
        lines = st["lyrics"]["lines"]
        for line, d in zip(lines, payload.get("lines") or []):
            line["include"] = bool(d.get("include", True))
            if d.get("singer"):
                line["singer"] = d["singer"]
        st.setdefault("options", {}).update(payload.get("options") or {})
        st["status"] = "working"
        st["status_text"] = "已确认制作方案，等待 Agent 开始自动打轴"
        st.setdefault("agent", {})["waiting_for"] = None
        st["confirmed_stage1_at"] = time.time()

    store.update(apply)
    n = sum(1 for l in store.load()["lyrics"]["lines"] if l.get("include", True))
    store.log(f"用户确认阶段一：{n} 行歌词，模板 {payload.get('options', {}).get('template')}，特效 {payload.get('options', {}).get('effect')}")


# ================================================================== ui-action
def cmd_ui_action(args) -> int:
    store = JobStore(args.job)
    action = read_json(Path(args.action_file))
    kind = action.get("type")
    payload = action.get("payload") or {}
    try:
        if kind == "edit_timing":
            from . import sugbridge

            project = sugbridge.load_project(store.dir / "timing" / "project.sug")
            log = sugbridge.apply_edits(project, _resolve_auto_ops(store, project, payload.get("ops", [])))
            sugbridge.save_project(project, store.dir / "timing" / "project.sug")
            _run_qa(store, project)
            for line in log:
                store.log("手动微调：" + line)
        elif kind == "preview_styles":
            from . import render

            def upd(st: dict) -> None:
                if payload.get("options"):
                    st.setdefault("options", {}).update(payload["options"])
                if payload.get("singers"):
                    st["song"]["singers"] = payload["singers"]

            store.update(upd)
            render.cmd_previews(store, payload.get("only"))
        elif kind == "confirm_stage1":
            apply_stage1(store, payload)
            hr = (payload.get("options") or {}).get("hires") or {}
            reg = (store.load()["media"].get("hires") or {})
            if (hr.get("on") or None) != (reg.get("src_on") or None) or list(hr.get("off") or []) != list(reg.get("src_offs") or []):
                set_hires_source(store, hr.get("on"), hr.get("off"))
        elif kind == "set_hires_source":
            set_hires_source(store, payload.get("on"), payload.get("off"))
        elif kind == "mv_preset":
            from . import mv

            spec = mv.preset_spec(payload["preset"])
            dk = mv.save_spec(store, spec)
            store.log(f"{mv.KIND_LABEL[dk]}设计：用户选择预设「{spec['name']}」")
            mv.render_stills(store, kind=dk, spec=spec)
        elif kind == "mv_stills":
            from . import mv

            mv.render_stills(store, kind=payload.get("kind"))
        elif kind == "mv_gallery":
            from . import mv

            mv.render_gallery(store, payload.get("kind"))
        elif kind == "set_background":
            set_background(store, payload.get("background") or {})
        elif kind == "mv_video_search":
            from . import mvvideo

            q = str(payload.get("query") or "").strip()
            if q:
                store.set_status("正在搜索 MV", "working")
                mvvideo.set_candidates(store, q, mvvideo.search(q, 8))
                store.set_status(f"找到 MV 候选：{q}", None)
        elif kind == "mv_video_use":
            from . import mvvideo

            res = mvvideo.use(store, str(payload["url"]))
            from . import render

            render.cmd_previews(store, ["templates"])  # galleries now show the MV frame
            store.log("MV 背景：" + "；".join(res.get("notes") or []))
        elif kind == "mv_video_local":
            from . import mvvideo

            p = Path(str(payload.get("path") or "").strip().strip('"'))
            if not p.is_file():
                raise SystemExit(f"找不到视频文件：{p}")
            store.update(lambda s: s["media"].update(mv_video={"file": str(p), "title": p.stem, "source": "本地文件"}))
            mvvideo.align(store, p)
        elif kind == "montage_source":
            src = payload.get("source") if payload.get("source") in ("web", "user", "mixed") else "mixed"
            store.update(lambda st: st.setdefault("options", {}).update(montage_source=src))
            store.log("混剪素材来源：" + {"web": "Agent 上网搜集", "user": "用户上传图包", "mixed": "两者混合"}[src])
        elif kind == "mv_assets_import":
            from . import mv, mv_assets

            def progress(done: int, total: int) -> None:
                if done == total or done % 5 == 0:
                    store.set_status(f"正在导入图片 {done}/{total}", "working")

            res = mv_assets.add_many(store, payload.get("paths") or [], origin=payload.get("origin") or "user",
                                     credit=payload.get("credit"), progress=progress)
            ok = sum(1 for r in res if r.get("ok"))
            bad = len(res) - ok
            msg = f"已导入 {ok} 张图片" + (f"，{bad} 张未导入（重复 / 太小 / 无法解码）" if bad else "")
            store.log("混剪素材：" + msg)
            store.set_status(msg, None)
            if mv.design_kind(store.load()) == "montage":
                mv.render_stills(store, kind="montage")
            else:
                mv.refresh_montage_plan(store)
        elif kind == "mv_asset_remove":
            from . import mv, mv_assets

            gone = mv_assets.remove(store, [str(x) for x in payload.get("ids") or []])
            store.log(f"混剪素材：用户移除 {gone} 张")
            if mv.design_kind(store.load()) == "montage":
                mv.render_stills(store, kind="montage")
            else:
                mv.refresh_montage_plan(store)
        elif kind == "mv_plan":
            from . import mv

            mv.refresh_montage_plan(store)
        elif kind == "set_option":
            from . import render

            store.update(lambda st: st.setdefault("options", {}).update(payload))
            render.write_web_style(store)
        elif kind == "preview_frame":
            from . import render

            t = float(payload.get("t", 0))
            out = store.path("previews", "engine_frame.png")
            render.render_job_frame(store, t, out)
            store.update(lambda st: st["previews"].update(engine_frame={"path": "previews/engine_frame.png", "t": t,
                                                                        "rev": int(time.time() * 1000)}))
        elif kind == "export":
            from . import render

            kinds = payload.get("kinds") or ["sug", "yurika", "mp4"]
            if payload.get("hires"):
                store.update(lambda st: st.setdefault("options", {}).update(hires=payload["hires"]))
            render.cmd_export(store, kinds, clip=payload.get("clip"))
        else:
            store.log(f"未知操作 {kind}")
    except Exception as exc:
        store.log(f"操作 {kind} 失败：{exc}")
        store.chat("system", f"操作「{kind}」失败：{exc}")
        traceback.print_exc()
        return 1
    return 0


DISPATCH = {
    "new": cmd_new,
    "set": cmd_set,
    "analyze": cmd_analyze,
    "lyrics-search": cmd_lyrics_search,
    "lyrics-use": cmd_lyrics_use,
    "lines": cmd_lines,
    "match": cmd_match,
    "previews": cmd_previews,
    "mv": cmd_mv,
    "spectrum": cmd_mv,
    "versions": cmd_versions,
    "hires-source": cmd_hires_source,
    "mv-assets": cmd_mv_assets,
    "mv-video": cmd_mv_video,
    "timing": cmd_timing,
    "realign": cmd_realign,
    "edit": cmd_edit,
    "style": cmd_style,
    "qa": cmd_qa,
    "frame": cmd_frame,
    "export": cmd_export,
    "hires": cmd_hires,
    "open-app": cmd_open_app,
    "ui-action": cmd_ui_action,
}


def dispatch(args) -> int:
    return DISPATCH[args.cmd](args) or 0
