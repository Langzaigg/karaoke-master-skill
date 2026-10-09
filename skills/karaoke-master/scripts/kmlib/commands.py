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
    old = read_json(store.dir / "lyrics" / "candidates.json", []) or []
    # keep what the user gave (their LRC) next to the new search results
    cands += [c for c in old if c.get("provider") == "user"]
    # ids are positional: re-point the chosen lyrics at the same candidate (or keep it) after a re-search
    sel = st["lyrics"].get("selected")
    was = next((c for c in old if c["id"] == sel and c.get("provider") != "user"), None)
    if was is not None:
        def ident(c: dict) -> tuple:
            return c.get("provider"), c.get("title"), c.get("artist"), round(float(c.get("duration") or 0)), c.get("line_count")

        same = next((c for c in cands if ident(c) == ident(was)), None)
        if same is None:
            same = dict(was, id=f"kept-{was['id']}")
            cands.append(same)
        if same["id"] != sel:
            store.update(lambda s2: s2["lyrics"].update(selected=same["id"]))
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
    credits: list[str] = []
    if args.candidate in by_id:
        cand = by_id[args.candidate]
        lines = json.loads(json.dumps(cand["lines"]))
        selected = cand["id"]
        credits = list(cand.get("credits") or [])
    else:
        path = Path(args.candidate)
        if not path.is_absolute():
            path = store.dir / path
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        if lyrics.is_lrc_text(text):
            # a synced file the user gave: keep its line times as the timing
            # reference for `match` (registered as candidate "user-lrc")
            lines, credits = lyrics.parse_user_lrc(text)
            selected = "user-lrc"
            cands = [c for c in cands if c["id"] != selected] + [{
                "id": selected, "provider": "user", "provider_name": "用户提供的 LRC", "title": path.stem,
                "artist": None, "album": None, "duration": None, "url": None, "has_ruby": False, "has_timing": True,
                "has_translation": any(l.get("tr") for l in lines), "line_count": len(lines),
                "lines": json.loads(json.dumps(lines)),
                "credits": credits, "error": ""}]
            write_json(store.path("lyrics", "candidates.json"), cands)
            by_id = {c["id"]: c for c in cands}
            store.update(lambda st: st["lyrics"].update(candidates=[
                {k: c[k] for k in c if k != "lines"} | {"preview": [l["text"] for l in c["lines"][:4]]} for c in cands]))
        else:
            lines = lyrics.parse_user_lyrics(text)
            selected = "custom"
    note = []
    if selected == "user-lrc":
        note.append(f"LRC 时间作为定位参考（{sum(1 for l in lines if l.get('t') is not None)} 行带时间）")
        if credits:
            note.append("文件内的制作信息未当作歌词：" + "；".join(credits))
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
    for spec in getattr(args, "fix_char", None) or []:
        # one character by position (typos such as "," for an apostrophe) — no lyric text needed
        head, ch = spec.split("=", 1)
        li, pos = (int(x) for x in head.split(":"))
        line = lines[li - 1]
        text = line["text"]
        if len(ch) > 3 or not 0 <= pos < len(text):
            raise SystemExit(f"--fix-char {spec}：位置超出范围，或替换文字超过 3 个字符")
        line["text"] = text[:pos] + ch + text[pos + 1:]
        delta = len(ch) - 1
        if delta:  # spans after the position move with the text (deleted char: its 1-char spans go)
            def shift(spans: list) -> list:
                out = []
                for a, b, *rest in spans:
                    if delta < 0 and a == b == pos:
                        continue
                    out.append([a + delta if a > pos else a, b + delta if b >= pos and (b > pos or delta < 0) else b, *rest])
                return out
            line["ruby"] = shift(line.get("ruby") or [])
            if line.get("words"):
                line["words"] = shift(line["words"])
        changed.append(f"第 {li} 行第 {pos + 1} 个字符" + (f"改为「{ch}」" if ch else "删除"))
    if getattr(args, "split_long", None):
        k = lyrics.auto_split_long(lines, args.split_long)
        changed.append(f"拆分 {k} 个过长行")
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
def _ref_key(text: str, reading: str) -> str:
    """Key for matching a lyric line to the synced reference: English lines by their letters (our
    UtaTen lines carry katakana readings for English, the reference has the plain words)."""
    import unicodedata

    letters = re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFKC", text).lower())
    return letters if len(letters) > len(re.findall(r"[぀-ヿ一-鿿]", text)) else reading


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
    chosen = next((c for c in cands if c["id"] == st["lyrics"].get("selected") and c.get("has_timing")), None)
    # the chosen lyrics are their own best reference when they are synced (e.g. the user's LRC)
    ref = _pick_reference(cands, duration, getattr(args, "ref", None) or (chosen["id"] if chosen else None))
    if ref is not None:
        ref_readings = [_ref_key(l["text"], analysis.reading_of(l["text"])) for l in ref["lines"]]
        mapping = analysis.map_to_reference([_ref_key(l["text"], r) for l, r in zip(lines, readings)], ref_readings)
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


OVERFLOW_NOTE = "超出画面宽度：用 edit split_line 拆成两行"


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
    wide = (read_json(store.path("render", "web_layout.json")) or {}).get("overflow") or []
    if wide:  # the engine layout says these lines are wider than the frame
        for i in wide:
            if i in qa:
                qa[i] = {**qa[i], "flag": "bad", "notes": list(qa[i].get("notes", [])) + [OVERFLOW_NOTE]}
        view = _save_view(store, project, qa)
        summary = {k: sum(1 for q in qa.values() if q["flag"] == k) for k in ("ok", "warn", "bad")}
        store.update(lambda s: s["timing"].update(qa_summary=summary))
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
        if str(st["song"].get("language") or "").lower().startswith("en") and not getattr(args, "aligner", False):
            # English song: the forced aligner (Japanese model) smears English words; time every
            # word from the word-level lyrics / English recognition instead (= realign --english)
            from types import SimpleNamespace

            store.step("align", state="running", detail="英文歌：按逐字歌词 / 英文识别给每个单词定时")
            ns = SimpleNamespace(window=None, device=args.device, no_words=getattr(args, "no_words", False),
                                 fresh_asr=False, asr_first=False)
            res = _english_timing(store, store.load(), project, list(range(len(lines))), ns)
            store.step("align", state="done", detail=res["mode"] + (
                f"（逐字歌词偏移 {res['lyrics_offset']:+.2f} 秒，{res['lyrics_agree']:.0%} 单词与识别一致）"
                if res.get("lyrics_offset") is not None else ""))
            store.step("refine", state="done", detail="英文歌按单词时间定时，不做能量修正")
            s = res["summary"]
            store.step("qa", state="running", progress=0.5,
                       detail=f"正常 {s['ok']} · 需注意 {s['warn']} · 疑似错误 {s['bad']}（等待 Agent 复核）")
            store.update(lambda st2: st2["timing"].update(sug="timing/project.sug", aligned_at=time.time(),
                                                          method="english"))
            store.set_status("打轴完成，Agent 正在复核", "working")
            bad = [li for li, v in res["lines"].items() if isinstance(v, str)]
            _p({"method": "english", "mode": res["mode"], "lyrics_offset": res.get("lyrics_offset"),
                "lyrics_agree": res.get("lyrics_agree"), "words": res["words"], "qa": s, "untimed_lines": bad,
                "bad_lines": [i + 1 for i, q in res["qa"].items() if q["flag"] == "bad"],
                "warn_lines": [i + 1 for i, q in res["qa"].items() if q["flag"] == "warn"]})
            return 0
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
        if any(l.get("words") for l in lines) and not getattr(args, "no_words", False):
            anc = sugbridge.apply_word_anchors(project, lines)
            store.update(lambda s2: s2["timing"].update(word_anchors={k: v for k, v in anc.items() if k != "lines"}))
            store.log(anc.get("skipped") or (
                f"逐字歌词校正：{anc['words']} 个单词中 {anc['agree']:.0%} 与对齐一致（来源整体偏移 {anc['offset_ms']:+d} ms），"
                f"移动 {anc['moved']} 个单词（{len(anc['lines'])} 行）"))
            sugbridge.save_project(project, sug_path)
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
    if getattr(args, "english", False):
        return _realign_english(store, st, project, idx, args)
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
    lines, _singers = _timing_lines(st)
    if len(lines) == n and any(lines[i].get("words") for i in idx) and not getattr(args, "no_words", False):
        anc = sugbridge.apply_word_anchors(project, lines, line_indices=idx)
        store.log(anc.get("skipped") or f"逐字歌词校正：移动 {anc['moved']} 个单词")
    from . import media

    sugbridge.refine_with_energy(project, media.rms_envelope(store.abs(st["media"]["vocals"])), line_indices=idx)
    sugbridge.apply_edits(project, [])
    sugbridge.save_project(project, store.dir / "timing" / "project.sug")
    res = _run_qa(store, project, stats.get("token_scores"))
    store.log(f"重新对齐第 {idx[0] + 1}-{idx[-1] + 1} 行完成")
    _p({"lines": [i + 1 for i in idx], "window": [a, b],
        "qa": {i + 1: res["qa"][i] for i in idx}, "summary": res["summary"]})
    return 0


def _english_words(line: dict) -> list[tuple[list[int], str]]:
    """Words of a timed line: ([char indices], normalized lowercase word)."""
    words, cur = [], []
    chars = line["chars"]
    for ci, ch in enumerate(chars + [{"c": " "}]):
        if re.match(r"[A-Za-z0-9'’‘]", ch["c"]):
            cur.append(ci)
        elif cur:
            words.append((cur, re.sub(r"[^a-z0-9]", "", "".join(chars[k]["c"] for k in cur).lower())))
            cur = []
    return words


def _norm_word(w: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (w or "").lower())


def _verbatim_offset(verb: dict[int, list], st_lines: list[dict], asr_words: list[dict],
                     max_shift: float = 60.0) -> tuple[float, int, float]:
    """The word-level lyrics' constant offset against the recognizer: every word is
    paired with the same-spelled recognized words within ``max_shift`` seconds (a MAD
    or an MV intro can shift the song by many seconds), the 0.1 s bin most words fall
    into wins, and the offset is the median of the pairs near it. Returns (offset,
    words paired, share of them within 0.25 s of the offset)."""
    import collections
    import statistics

    pairs = []
    for li, words in verb.items():
        text = st_lines[li]["text"]
        for w in words:
            key = _norm_word(text[w[0]:w[1] + 1])
            cands = [x["s"] - w[2] for x in asr_words if x["n"] == key and abs(x["s"] - w[2]) < max_shift]
            if key and cands:
                pairs.append(cands)
    if len(pairs) < 5:
        return 0.0, len(pairs), 0.0
    hist = collections.Counter(b for c in pairs for b in {round(d, 1) for d in c})
    mode = max(hist, key=lambda b: (hist[b] + 0.5 * (hist.get(round(b - 0.1, 1), 0) + hist.get(round(b + 0.1, 1), 0)),
                                    -abs(b)))
    # refine: the offset (within ±0.3 s of the fullest bin) that most words agree with, then the
    # median of those words — the bin alone can sit beside the peak of a skewed distribution

    def agreeing(o: float) -> list[float]:
        return [d for d in (min(c, key=lambda d: abs(d - o)) for c in pairs) if abs(d - o) <= 0.25]

    cand = [mode + k * 0.01 for k in range(-30, 31)]
    peak = max(cand, key=lambda o: (len(agreeing(o)), -abs(o - mode)))
    off = statistics.median(agreeing(peak)) if agreeing(peak) else peak
    return off, len(pairs), len(agreeing(off)) / len(pairs)


def _realign_english(store: JobStore, st: dict, project, idx: list[int], args) -> int:
    _p(_english_timing(store, st, project, idx, args))
    return 0


def _english_timing(store: JobStore, st: dict, project, idx: list[int], args) -> dict:
    """English lines: word times from an English recognizer (the forced aligner is built for
    Japanese and smears English words), merged with the song's word-level lyrics (逐字歌词:
    NetEase YRC / Kugou KRC, ``words`` of the stage-1 lines) when the chosen lyrics have them —
    those place the words the recognizer missed, give karaoke word lengths (recognizer word
    ends are vague) and the search window of every line. An English song re-uses the stage-1
    English recognition of the whole vocal stem; otherwise the line windows are recognized
    again. Without word-level lyrics each line is searched between the previous line's start
    and the next line's end, so a repeated refrain matches its own occurrence."""
    import subprocess
    from difflib import SequenceMatcher

    from . import analysis, sugbridge
    from .paths import ffmpeg_exe

    view = sugbridge.project_view(project)
    lines = view["lines"]
    n = len(lines)
    st_lines, _singers = _timing_lines(st)
    idx = sorted(idx)  # in order: a line's search starts after the previous line's last word (refrains)
    verb_all = {}
    if len(st_lines) == n and not getattr(args, "no_words", False):
        verb_all = {li: st_lines[li]["words"] for li in range(n) if st_lines[li].get("words")
                    and st_lines[li]["text"] == lines[li]["text"]}
    verb = {li: verb_all[li] for li in idx if li in verb_all}
    # 1 recognizer words -------------------------------------------------
    asr_words = None
    lang = str((st.get("song") or {}).get("language") or "ja").lower()
    full = read_json(store.dir / "analysis" / "asr.json") if lang.startswith("en") and not args.window \
        and not getattr(args, "fresh_asr", False) else None
    if full and any(seg.get("words") for seg in full.get("segments", [])):
        asr_words = [{"s": w["s"], "e": w["e"], "n": _norm_word(w["w"])}
                     for seg in full["segments"] for w in seg.get("words") or []]
    stored = (st.get("timing") or {}).get("word_offset") or {}
    if verb and asr_words:  # the whole song's recognition: measure the offset song-wide
        off, n_pairs, agree = _verbatim_offset(verb_all, st_lines, asr_words)
        store.update(lambda s2: s2.setdefault("timing", {}).update(
            word_offset={"offset": round(off, 3), "pairs": n_pairs, "agree": round(agree, 3)}))
    elif verb and stored:  # re-recognizing a few lines: keep the song-wide value
        off, n_pairs, agree = float(stored["offset"]), int(stored["pairs"]), float(stored["agree"])
    else:
        off, n_pairs, agree = 0.0, 0, 0.0
    trusted = n_pairs >= 8 and agree >= 0.4
    if verb and (asr_words or stored) and not trusted:
        verb = {}  # the word-level lyrics do not line up with this audio (another version): don't use them
    wins = {}
    for li in idx:
        if args.window:
            a, b = (float(x) for x in args.window.split(","))
        elif li in verb and (asr_words or stored):  # the word-level lyrics know where the line is
            vw = verb[li]
            a = vw[0][2] + off - 2.0
            b = vw[-1][2] + off + (vw[-1][3] if len(vw[-1]) > 3 else 1.0) + 2.0
        elif lines[li]["start"] is None and len(st_lines) == n:  # not timed yet: the stage-1 estimates
            est = [((l.get("match") or {}).get("est")) for l in st_lines]
            prev = next((est[i] for i in range(li - 1, -1, -1) if est[i] is not None), None)
            nxt = next((est[i] for i in range(li + 1, n) if est[i] is not None), None)
            here = est[li] if est[li] is not None else (prev if prev is not None else 0.0)
            a = (prev if prev is not None else here - 4.0) - 1.0
            b = (nxt if nxt is not None else here + 8.0) + 3.0
        else:
            prev = lines[li - 1] if li > 0 else None
            nxt = lines[li + 1] if li + 1 < n else None
            a = (prev["start"] if prev and prev["start"] is not None else (lines[li]["start"] or 4.0) - 4.0) - 1.0
            b = (nxt["end"] if nxt and nxt["end"] else (lines[li]["end"] or a) + 6.0) + 1.0
        wins[li] = (max(0.0, a), b)
    if asr_words is None:
        merged: list[list[float]] = []
        for a, b in sorted(wins.values()):
            if merged and a <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        # one recognizer run: the windows joined with 1.5 s of silence; map times back afterwards
        tmp = store.path("analysis", "english")
        tmp.mkdir(parents=True, exist_ok=True)
        gap = 1.5
        parts, offsets, pos = [], [], 0.0
        for k, (a, b) in enumerate(merged):
            parts.append(f"[0:a]atrim={a:.3f}:{b:.3f},asetpts=PTS-STARTPTS,aresample=16000,aformat=sample_fmts=fltp:sample_rates=16000:channel_layouts=mono[w{k}]")
            offsets.append((pos, a, b - a))
            pos += (b - a) + gap
        concat = "".join(f"[w{k}][s{k}]" if k + 1 < len(merged) else f"[w{k}]" for k in range(len(merged)))
        sil = ";".join(f"anullsrc=r=16000:cl=mono,atrim=0:{gap},aformat=sample_fmts=fltp:sample_rates=16000:channel_layouts=mono[s{k}]" for k in range(len(merged) - 1))
        graph = ";".join(parts + ([sil] if sil else []))
        graph += f";{concat}concat=n={2 * len(merged) - 1}:v=0:a=1[out]"
        clip = tmp / "english_windows.wav"
        subprocess.run([ffmpeg_exe(), "-v", "error", "-y", "-i", str(store.abs(st["media"]["vocals"])),
                        "-filter_complex", graph, "-map", "[out]", "-ac", "1", "-ar", "16000", str(clip)], check=True)
        store.set_status(f"英文识别：第 {', '.join(str(i + 1) for i in idx)} 行", "working")
        out = analysis.run_asr(clip, tmp / "english_asr.json", language="en", device=args.device)
        asr_words = []
        for seg in read_json(out).get("segments", []):
            for w in seg.get("words") or []:
                for start, a, length in offsets:
                    if start - 0.1 <= w["s"] <= start + length + 0.1:
                        asr_words.append({"s": w["s"] - start + a, "e": w["e"] - start + a, "n": _norm_word(w["w"])})
                        break
        if verb and not stored:
            off, n_pairs, agree = _verbatim_offset(verb, st_lines, asr_words)
            if not (n_pairs >= 8 and agree >= 0.4):
                verb = {}
    # 2 per line: recognizer hits + word-level lyrics -----------------------
    ops, report, planned = [], {}, {}
    lyrics_first = bool(verb) and n_pairs >= 20 and agree >= 0.6 and not getattr(args, "asr_first", False)
    used = {"asr": 0, "lyrics": 0, "lyric_ends": 0, "between": 0}
    for li in idx:
        words = _english_words(lines[li])
        a, b = wins[li]
        cand = [w for w in asr_words if a <= w["s"] <= b]
        if li - 1 in planned:  # a repeated refrain must not match the previous line's occurrence
            floor = planned[li - 1][1][-1][1] - 0.05
            cand = [w for w in cand if w["s"] >= floor]
        sm = SequenceMatcher(None, [w for _, w in words], [w["n"] for w in cand], autojunk=False)
        hit = {}
        for x, y, size in sm.get_matching_blocks():
            for k in range(size):
                hit[x + k] = cand[y + k]

        def piece(c: int):
            return next((w for w in verb.get(li, []) if w[0] <= c <= w[1]), None)

        # a source word may be split into syllable pieces: start from the first, end with the last
        vmap = {k: (piece(chars[0]), piece(chars[-1])) for k, (chars, _w) in enumerate(words)}
        n_v = sum(1 for v0, _v1 in vmap.values() if v0 is not None)
        known = sum(1 for k in range(len(words)) if k in hit or vmap[k][0] is not None)
        enough = max(min(2, len(words)), (len(words) + 1) // 2)
        if not words or known < enough:
            report[li + 1] = f"识别到的单词不足（{len(hit)}/{len(words)}），未修改"
            continue

        def v_end(v0, v1, default: float) -> float:
            last = v1 if v1 is not None and len(v1) > 3 else v0 if len(v0) > 3 else None
            return last[2] + off + last[3] if last is not None else v0[2] + off + default

        times: list = []
        for k in range(len(words)):
            v0, v1 = vmap[k]
            if lyrics_first and v0 is not None and len(v0) > 3:
                # a reliable word-level source (it agrees with the recognizer on most words):
                # its starts and lengths are karaoke timing; the recognizer's word boundaries
                # are contiguous and pull a line's first word back into the previous held note
                s0 = v0[2] + off
                times.append((s0, max(s0 + 0.08, v_end(v0, v1, 0.35))))
                used["lyrics"] += 1
            elif k in hit:
                s0, e0 = hit[k]["s"], hit[k]["e"]
                used["asr"] += 1
                if v0 is not None and len(v0) > 3 and abs(v0[2] + off - s0) <= 0.3:
                    e0 = max(s0 + 0.08, v_end(v0, v1, 0.35))  # karaoke word length from the lyric source
                    used["lyric_ends"] += 1
                times.append((s0, e0))
            elif v0 is not None:
                s0 = v0[2] + off
                times.append((s0, max(s0 + 0.08, v_end(v0, v1, 0.35))))
                used["lyrics"] += 1
            else:
                times.append(None)
        k = 0  # one source piece covering several words (e.g. a missing space fixed later): split its time
        while k < len(words):
            j = k
            while j + 1 < len(words) and vmap[k][0] is not None and vmap[j + 1][0] is vmap[k][0]:
                j += 1
            group = range(k, j + 1)
            if j > k and all(times[x] for x in group) and (lyrics_first or not any(x in hit for x in group)):
                s0, e0 = times[k][0], max(times[x][1] for x in group)
                lens = [len(words[x][0]) for x in group]
                acc = 0
                for x, ln in zip(group, lens):
                    a0 = s0 + (e0 - s0) * acc / sum(lens)
                    acc += ln
                    times[x] = (a0, s0 + (e0 - s0) * acc / sum(lens))
            k = j + 1
        for j, t in enumerate(times):  # still unknown: between their neighbours
            if t:
                continue
            used["between"] += 1
            prev = next((times[i] for i in range(j - 1, -1, -1) if times[i]), None)
            nxt = next((times[i] for i in range(j + 1, len(times)) if times[i]), None)
            s0 = prev[1] if prev else nxt[0] - 0.4
            e0 = nxt[0] if nxt else prev[1] + 0.4
            times[j] = (s0, max(s0 + 0.1, e0))
        for j in range(1, len(times)):  # keep words in order (the two sources can disagree a little)
            s0, e0 = times[j]
            ps, pe = times[j - 1]
            s0 = max(s0, ps + 0.05)
            times[j - 1] = (ps, min(pe, s0))
            times[j] = (s0, max(e0, s0 + 0.08))
        times = [(max(0.0, s0), max(max(0.0, s0) + 0.08, e0)) for s0, e0 in times]  # SUG rejects t < 0
        planned[li] = (words, times)
        report[li + 1] = {"matched": f"{len(hit)}/{len(words)}", "lyrics_words": n_v}
    # a held last word that runs a little into the next line ends where that line starts
    # (bigger overlaps are kept: duet parts can really overlap)
    clamped = 0
    for li, (words, times) in planned.items():
        nxt = planned[li + 1][1][0][0] if li + 1 in planned else (
            lines[li + 1]["start"] if li + 1 < n and li + 1 not in idx else None)
        s0, e0 = times[-1]
        if nxt is not None and 0 < e0 + 0.1 - nxt <= 0.3:
            times[-1] = (s0, max(s0 + 0.08, nxt - 0.12))
            clamped += 1
    for li, (words, times) in planned.items():
        chars_all = lines[li]["chars"]
        for ci, ch in enumerate(project.sentences[li].characters[:-1]):  # drop the old timing's pauses
            if ch.is_sentence_end:
                ops.append({"op": "set_pause", "line": li, "char": ci, "t": None})
        for j, ((chars, _w), (s0, e0)) in enumerate(zip(words, times)):
            sent_chars = project.sentences[li].characters  # check_count: also right before the first timing
            cps = [(ci, c) for ci in chars for c in range(max(sent_chars[ci].check_count,
                                                                 len(chars_all[ci].get("cp") or [])))]
            for k, (ci, c) in enumerate(cps):
                ops.append({"op": "set_char", "line": li, "char": ci, "cp": c,
                            "t": round(s0 + (e0 - s0) * k / max(1, len(cps)), 3)})
            if j + 1 < len(times) and times[j + 1][0] - e0 > 0.35:  # a rest before the next word
                ops.append({"op": "set_pause", "line": li, "char": chars[-1], "t": round(e0 + 0.05, 3)})
        ops.append({"op": "set_line_end", "line": li, "t": round(times[-1][1] + 0.1, 3)})
        report[li + 1].update(start=round(times[0][0], 2), end=round(times[-1][1] + 0.1, 2))
    used["clamped_line_ends"] = clamped
    if ops:
        sugbridge.apply_edits(project, ops)
        sugbridge.save_project(project, store.dir / "timing" / "project.sug")
    res = _run_qa(store, project)
    src = (f"；逐字歌词偏移 {off:+.2f} 秒，{agree:.0%} 的单词与识别一致（{n_pairs} 个比对）"
           + ("，以逐字歌词为主" if lyrics_first else "")) if verb else ""
    store.log("英文行按英文识别重新对齐：" + "，".join(str(k) for k in report) + src)
    return {"lines": report, "summary": res["summary"], "words": used,
            "lyrics_offset": round(off, 3) if verb else None, "lyrics_agree": round(agree, 2) if verb else None,
            "mode": "逐字歌词为主、识别补缺" if lyrics_first else "语音识别为主" + ("、逐字歌词补缺" if verb else ""),
            "qa": res["qa"],
            "next": "再用 realign --lines <前后的日文行> 让相邻日文行重新对齐（纯英文歌不需要）"}


def _sync_split_stage1(store: JobStore, ops: list[dict]) -> None:
    """``split_line`` on the timing project → split the stage-1 lyric line the same way (text and
    furigana), so re-timing and the page keep matching the project. Ops apply in order."""
    splits = [(int(o["line"]), int(o["char"])) for o in ops if o.get("op") == "split_line"]
    if not splits:
        return

    def upd(st):
        lines = st["lyrics"]["lines"]
        for li, at in splits:
            inc = [k for k, l in enumerate(lines) if l.get("include", True) and l["text"].strip()]
            if li >= len(inc):
                continue
            k = inc[li]
            line = lines[k]
            text = line["text"]
            a = len(text[:at].rstrip())
            b = at + (len(text[at:]) - len(text[at:].lstrip()))
            ruby = line.get("ruby") or []
            words = line.get("words") or []  # word-level lyric times move with their characters
            head = dict(line, text=text[:a], ruby=[r for r in ruby if r[1] < a],
                        words=[[w[0], min(w[1], a - 1), *w[2:]] for w in words if w[0] < a])
            tail = dict(line, text=text[b:], ruby=[[r[0] - b, r[1] - b, *r[2:]] for r in ruby if r[0] >= b],
                        words=[[w[0] - b, w[1] - b, *w[2:]] for w in words if w[0] >= b])
            for d in (head, tail):
                d.pop("match", None)
                d.pop("t", None)
            lines[k:k + 1] = [head, tail]

    store.update(upd)


def cmd_edit(args) -> int:
    from . import sugbridge

    store = JobStore(args.job)
    project = sugbridge.load_project(store.dir / "timing" / "project.sug")
    ops = _json_arg(args.ops)
    if isinstance(ops, dict):
        ops = [ops]
    ops = _resolve_auto_ops(store, project, ops)
    if ops:
        _snapshot(store, "Agent 编辑：" + "、".join(sorted({str(o.get("op")) for o in ops})))
    log = sugbridge.apply_edits(project, ops)
    sugbridge.save_project(project, store.dir / "timing" / "project.sug")
    _sync_split_stage1(store, ops)
    res = _run_qa(store, project)
    for line in log:
        store.log("编辑：" + line)
    _p({"applied": log, "summary": res["summary"]})
    return 0


def cmd_singers(args) -> int:
    """歌割り from a subtitle file / part-distribution source → per-character singers."""
    import urllib.request

    from . import parts, render, sugbridge

    store = JobStore(args.job)
    st = store.load()
    view = read_json(store.dir / "timing" / "timed.json")
    if not view:
        raise SystemExit("尚未打轴：先完成打轴再导入歌割り")
    mapping = {}
    for item in args.map or []:
        k, _, v = item.partition("=")
        mapping[parts._hex(k) if k.strip().startswith(("#", "rgb")) else k.strip()] = v.strip()
    source = args.source
    if re.match(r"https?://", source):
        ext = Path(source.split("?")[0]).suffix.lower()
        ext = ext if ext in (".ass", ".ssa", ".lrc", ".txt", ".json") else ".html"
        req = urllib.request.Request(source, headers={"User-Agent": "Mozilla/5.0"})
        path = store.path("lyrics", "parts_source" + ext)
        path.write_bytes(urllib.request.urlopen(req, timeout=30).read())
    else:
        path = Path(source)
    song_singers = list(st["song"].get("singers") or [])
    names = {s["name"] for s in song_singers} | {s["name"] for s in view.get("singers") or []}
    runs = parts.read_runs(path, set(mapping) | names)
    res = parts.plan(view, runs, mapping, names)
    out = {k: res[k] for k in ("coverage", "singers", "uncovered", "weak", "keys")}
    if res["unmapped"] and not args.ignore_unmapped:
        _p({**out, "need_map": res["unmapped"],
            "hint": "用 --map 键=歌手 指定每个键（颜色 / Name / Style / 前缀）对应的歌手，不需要的写 键=-；"
                    "或加 --ignore-unmapped 忽略其余键"})
        return 2
    if args.dry_run or not res["singers"]:
        _p({**out, "dry_run": True})
        return 0
    # singers: job colours (by name) + SUG singers
    have = {s["name"] for s in song_singers}
    for k, name in enumerate(n for n in res["singers"] if n not in have):
        song_singers.append({"id": parts.singer_id(name), "name": name,
                             "color": parts.PALETTE[(len(song_singers) + k) % len(parts.PALETTE)]})
    color = {s["name"]: s.get("color") for s in song_singers}
    ops = [{"op": "add_singer", "name": n, "color": color.get(n)} for n in res["singers"]]
    ops += parts.edit_ops(res)
    project = sugbridge.load_project(store.dir / "timing" / "project.sug")
    sugbridge.apply_edits(project, ops)
    sugbridge.save_project(project, store.dir / "timing" / "project.sug")
    ids = {s["name"]: s["id"] for s in song_singers}
    majority = [max(set(v), key=v.count) if v else None for v in res["lines"]]

    def upd(s):
        s["song"]["singers"] = song_singers
        s["song"]["parts"] = {"source": args.source, "map": mapping, "coverage": res["coverage"],
                              "time": time.strftime("%Y-%m-%d %H:%M:%S")}
        inc = [l for l in s["lyrics"]["lines"] if l.get("include", True) and l["text"].strip()]
        for line, who in zip(inc, majority):
            if who:
                line["singer"] = ids.get(who, line.get("singer"))

    store.update(upd)
    render.write_web_style(store, refresh=False)
    qa = _run_qa(store, project)
    store.log(f"歌割り：来自 {args.source}，覆盖 {res['coverage']:.0%}")
    _p({**out, "applied": len(ops), "summary": qa["summary"]})
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
    wide = set((read_json(store.path("render", "web_layout.json")) or {}).get("overflow") or [])
    for line in view["lines"]:
        q = dict(line.get("qa") or {})
        if line["i"] in wide and OVERFLOW_NOTE not in (q.get("notes") or []):  # engine layout of the page
            q["flag"] = "bad"
            q["notes"] = list(q.get("notes", [])) + [OVERFLOW_NOTE]
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


def cmd_mv_clips(args) -> int:
    """Video-clip montage: footage (YouTube / Bilibili / local) → shots in the montage pool."""
    from . import mv, mv_assets, mv_clips

    store = JobStore(args.job)
    out: dict = {}
    if args.search:
        items = mv_clips.youtube_search(args.search, args.n)
        out["results"] = [{"k": k + 1, **{x: it.get(x) for x in ("title", "uploader", "duration", "views", "url")}}
                          for k, it in enumerate(items)]
    if args.info:
        out["info"] = mv_clips.info(args.info)
    crop = [float(x) for x in args.crop.split(",")] if args.crop else None
    tags = [t.strip() for t in (args.tags or "").split(",") if t.strip()] or None
    sections = mv_clips.parse_sections(args.sections)
    opts = {"library": args.library, "min_score": args.min_score, "max_clips": args.max_clips, "crop": crop,
            "tags": tags}
    for src in args.add or []:
        if re.match(r"https?://", src):
            rec = mv_clips.download(store, src, sections=sections or None, max_height=args.max_height,
                                    library=args.library, credit=args.credit)
            out.setdefault("added", []).append(mv_clips.import_source(store, rec, sections=sections or None, **opts))
        else:  # the user's own video: --sections (download parts) does not apply
            rec = mv_clips.add_local(store, src, library=args.library, credit=args.credit)
            out.setdefault("added", []).append(mv_clips.import_source(store, rec, **opts))
    if args.from_library:
        index = read_json(mv_clips.library_dir(store, args.library) / "sources.json", {}) or {}
        for rec in index.values():
            try:
                out.setdefault("added", []).append(mv_clips.import_source(store, rec, **opts))
            except Exception as exc:  # noqa: BLE001 - one bad source must not stop the rest
                out.setdefault("failed", []).append({"source": rec.get("id"), "error": str(exc)})
    if args.exclude:
        ids = [x.strip() for x in args.exclude.split(",") if x.strip()]
        if any(x.startswith(("#", "U")) for x in ids):  # "#12" / "U3" = tile of the last --sheet / --sheet-used
            sheet = {}
            for f in sorted(store.path("previews").glob("clip_sheet_*.json")):
                sheet.update({f"#{e['n']}": e["id"] for e in read_json(f, [])})
            for f in sorted(store.path("previews").glob("clip_used_*.json")):
                sheet.update({e["n"]: e["id"] for e in read_json(f, [])})
            ids = [sheet.get(x, x) for x in ids]
        out["removed"] = mv_assets.remove(store, ids)
    if args.tag:
        out["tagged"] = mv_clips.set_tags(store, args.tag)
    if args.avoid_from:
        if not (Path(args.avoid_from) / "render" / "montage_plan.json").is_file():
            raise SystemExit(f"--avoid-from：{args.avoid_from} 没有剪辑计划（render/montage_plan.json）")
        ids = mv_clips.used_in(args.avoid_from)
        spec = mv.load_spec(store, "montage") if mv.spec_path(store, "montage").is_file() \
            else mv.preset_spec("game_montage")
        if mv.montage_layer(spec) is None:
            spec = mv.preset_spec("game_montage")
        layer = mv.montage_layer(spec)
        layer["avoid"] = sorted(set(layer.get("avoid") or []) | set(ids))  # keep e.g. subtitled shots in it
        mv.save_spec(store, spec, "montage")
        out["avoid"] = f"另一个工程已用的 {len(ids)} 个镜头排到最后（avoid 共 {len(layer['avoid'])} 个）"
    if args.sheet:
        out["sheets"] = mv_clips.contact_sheets(store)
    if getattr(args, "mark_checked", False):  # after a used-sheet review (and its --exclude)
        out["checked"] = mv_clips.mark_checked(store)
    if getattr(args, "sheet_used", False):
        if mv.montage_layer(mv.load_spec(store, "montage")) is not None:
            mv.refresh_montage_plan(store)
        out["used_sheets"] = mv_clips.used_sheets(store)
    if args.credits:
        out["credits"] = mv_clips.credits_by_source(store)
    if getattr(args, "enhance", False):
        out["enhance"] = mv_clips.enhance(
            store, ids=[x.strip() for x in (getattr(args, "ids", None) or "").split(",") if x.strip()] or None,
            only_used=getattr(args, "only_used", False), no_sr=getattr(args, "no_sr", False),
            no_rife=getattr(args, "no_rife", False), sr_scale=args.sr_scale, rife_multi=args.rife_multi)
    if args.list:
        out["clips"] = [{k: a.get(k) for k in ("id", "name", "duration", "motion", "score", "tags",
                                               "t_in", "t_out", "library_id", "kind", "w", "h")}
                        for a in mv_assets.load_pool(store)]
    out["pool"] = {"clips": len(mv_clips.clips(store)), "total": len(mv_assets.load_pool(store))}
    media = store.load().get("media") or {}
    if (args.add or args.from_library or args.exclude or args.avoid_from) and \
            mv.montage_layer(mv.load_spec(store, "montage")) is not None and media.get("audio") \
            and (media.get("source") or {}).get("duration"):
        plan = mv.refresh_montage_plan(store)
        out["plan"] = {k: plan.get(k) for k in ("bpm", "bars_per_shot", "clips", "unique_used", "ideal_unique",
                                                "repeated_sections", "slow_motion", "fallback_repeats", "note")}
        out["plan"]["shots"] = len(plan["shots"])
    store.set_status(f"视频片段素材池：{out['pool']['clips']} 个镜头", None)
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
HISTORY_KEEP = 30


def _snapshot(store: JobStore, label: str) -> None:
    """Keep the timing project + singers before a manual change (stage-3 撤销)."""
    hist = store.path("timing", "history")
    hist.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}"
    shutil.copy2(store.dir / "timing" / "project.sug", hist / f"{stamp}.sug")
    st = store.load()
    write_json(hist / f"{stamp}.json", {"label": label, "singers": st["song"].get("singers") or [],
                                        "lyrics_lines": st["lyrics"]["lines"]})
    snaps = sorted(hist.glob("*.sug"))
    for old in snaps[:-HISTORY_KEEP]:
        old.unlink(missing_ok=True)
        old.with_suffix(".json").unlink(missing_ok=True)
    store.update(lambda s: s["timing"].update(undo=len(snaps[-HISTORY_KEEP:]), undo_label=label))


def _undo(store: JobStore) -> str | None:
    from . import render, sugbridge

    hist = store.path("timing", "history")
    snaps = sorted(hist.glob("*.sug")) if hist.exists() else []
    if not snaps:
        return None
    last = snaps[-1]
    meta = read_json(last.with_suffix(".json")) or {}
    shutil.copy2(last, store.dir / "timing" / "project.sug")
    last.unlink()
    last.with_suffix(".json").unlink(missing_ok=True)
    rest = sorted(hist.glob("*.sug"))
    prev = (read_json(rest[-1].with_suffix(".json")) or {}).get("label") if rest else None

    def upd(s):
        if meta.get("singers"):
            s["song"]["singers"] = meta["singers"]
        if meta.get("lyrics_lines"):
            s["lyrics"]["lines"] = meta["lyrics_lines"]
        s["timing"].update(undo=len(rest), undo_label=prev)

    store.update(upd)
    render.write_web_style(store, refresh=False)
    _run_qa(store, sugbridge.load_project(store.dir / "timing" / "project.sug"))
    return meta.get("label") or "上一次修改"


def _ui_singers(store: JobStore, payload: dict) -> list[str]:
    """Stage-3 singer edits: add / recolour singers and assign whole lines; choosing several
    singers makes (or reuses) the chorus singer 「A＆B」, drawn in 拼色 with their colours."""
    from . import parts, render, styles, sugbridge

    st = store.load()
    singers = [dict(s) for s in st["song"].get("singers") or []]
    by_name = {s["name"]: s for s in singers}
    log: list[str] = []
    for add in payload.get("add") or []:
        name = str(add.get("name") or "").strip()
        if name and name not in by_name:
            s = {"id": parts.singer_id(name), "name": name,
                 "color": add.get("color") or parts.PALETTE[len(singers) % len(parts.PALETTE)]}
            singers.append(s)
            by_name[name] = s
            log.append(f"新增歌手 {name}")
    for name, color in (payload.get("colors") or {}).items():
        if name in by_name and color:
            by_name[name]["color"] = color
            if by_name[name].get("colors") and len(by_name[name]["colors"]) > 1:
                by_name[name].pop("colors")  # an explicit recolour turns 拼色 off for that singer
            log.append(f"{name} 的颜色改为 {color}")
    ops: list[dict] = []
    assign = payload.get("assign")
    lines: list[int] = []
    target = None
    if assign:
        order = [s["name"] for s in singers]
        names = sorted({n for n in assign.get("singers") or [] if n in by_name}, key=order.index)
        if not names:
            raise ValueError("没有选择歌手")
        members: list[str] = []
        for n in names:  # 「A＆B」 + C → A＆B＆C
            sub = [p.strip() for p in re.split(r"[＆&]", n) if p.strip()]
            sub = sub if len(sub) > 1 and all(p in by_name for p in sub) else [n]
            members += [m for m in sub if m not in members]
        target = members[0] if len(members) == 1 else "＆".join(members)
        if target not in by_name:
            s = {"id": parts.singer_id(target), "name": target,
                 "color": styles.blend([by_name[m]["color"] for m in members])}
            singers.append(s)
            by_name[target] = s
            log.append(f"新增合唱 {target}（拼色）")
        lines = sorted({int(i) for i in assign.get("lines") or []})
        op = {"op": "set_singer", "lines": lines, "singer": target}
        if assign.get("chars") and len(lines) == 1:
            op["chars"] = [int(x) for x in assign["chars"]]
        ops = [{"op": "add_singer", "name": target, "color": by_name[target]["color"]}, op]
    # chorus singers named after others follow their members' colours
    for s in singers:
        sub = [p.strip() for p in re.split(r"[＆&]", s["name"]) if p.strip()]
        if len(sub) > 1 and all(p in by_name for p in sub) and not s.get("colors"):
            s["color"] = styles.blend([by_name[p]["color"] for p in sub])
    if ops:
        log.append(f"第 {', '.join(str(i + 1) for i in lines)} 行 → {target}")
    _snapshot(store, log[-1] if log else "歌手设置")
    project = sugbridge.load_project(store.dir / "timing" / "project.sug")
    if ops:
        sugbridge.apply_edits(project, ops)
        sugbridge.save_project(project, store.dir / "timing" / "project.sug")
    ids = {s["name"]: s["id"] for s in singers}

    def upd(s):
        s["song"]["singers"] = singers
        if target and not (assign or {}).get("chars"):
            inc = [l for l in s["lyrics"]["lines"] if l.get("include", True) and l["text"].strip()]
            for i in lines:
                if i < len(inc):
                    inc[i]["singer"] = ids[target]

    store.update(upd)
    render.write_web_style(store, refresh=False)  # colours by name; _run_qa rebuilds the overlay
    _run_qa(store, project)
    for line in log:
        store.log("歌手：" + line)
    return log


def cmd_ui_action(args) -> int:
    store = JobStore(args.job)
    action = read_json(Path(args.action_file))
    kind = action.get("type")
    payload = action.get("payload") or {}
    try:
        if kind == "edit_timing":
            from . import sugbridge

            ops = payload.get("ops", [])
            _snapshot(store, "平滑走字" if any(o.get("op") == "smooth" for o in ops) else "时间微调")
            project = sugbridge.load_project(store.dir / "timing" / "project.sug")
            log = sugbridge.apply_edits(project, _resolve_auto_ops(store, project, ops))
            sugbridge.save_project(project, store.dir / "timing" / "project.sug")
            _sync_split_stage1(store, ops)
            _run_qa(store, project)
            for line in log:
                store.log("手动微调：" + line)
        elif kind == "singers":
            _ui_singers(store, payload)
        elif kind == "undo":
            label = _undo(store)
            store.log(f"已撤销：{label}" if label else "没有可撤销的修改")
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
    "mv-clips": cmd_mv_clips,
    "timing": cmd_timing,
    "realign": cmd_realign,
    "edit": cmd_edit,
    "singers": cmd_singers,
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
