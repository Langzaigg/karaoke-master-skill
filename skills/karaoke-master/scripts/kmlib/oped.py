"""OP/ED 拼接背景（oped 模式）.

Several OP / ED videos of the same song (a short ver, a long ver, a user's own
recording …) are each waveform-aligned onto the song's timeline, then stitched
into one picture-only background ``media/oped_bg.mp4``: the segment added first
wins wherever they overlap, neighbouring parts cross-fade, and stretches no
source covers are filled with the previous part's last frame (black at the very
beginning). All audio still comes from the song file.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path

from .jobstore import JobStore
from .paths import ffmpeg_exe, hidden_subprocess_kwargs

DEFAULT_FADE = 0.7


# ------------------------------------------------------------------ state
def _state(st: dict) -> dict:
    return (st.get("media") or {}).get("oped") or {}


def _segments(st: dict) -> list[dict]:
    return list(_state(st).get("segments") or [])


def _fade(st: dict) -> float:
    return float(_state(st).get("fade") or DEFAULT_FADE)


def _song_duration(st: dict) -> float:
    return float(st["media"]["source"]["duration"])


def _parse_time(text: str) -> float:
    """Seconds or m:ss (h:mm:ss) → seconds."""
    parts = str(text).strip().split(":")
    return sum(float(p) * 60 ** i for i, p in enumerate(reversed(parts)))


def _parse_section(spec: str, duration: float) -> tuple[float, float]:
    """``--section A-B``: the part of the source video this segment uses (video time)."""
    a, sep, b = str(spec).partition("-")
    if not sep or not a.strip() or not b.strip():
        raise SystemExit(f"--section 格式应为 起-止（秒或 m:ss）：{spec}")
    start, end = _parse_time(a), _parse_time(b)
    if not (0.0 <= start < end <= duration + 0.05):
        raise SystemExit(f"--section 超出视频范围（时长 {duration:.1f}s）：{spec}")
    return round(start, 3), round(min(end, duration), 3)


def _src_range(seg: dict) -> tuple[float, float]:
    """The segment's used range in its source video (whole video when no section)."""
    return (float(seg.get("src_start") or 0.0),
            float(seg.get("src_end") if seg.get("src_end") is not None else seg["duration"]))


def _speed(seg: dict) -> float:
    """Playback speed of the segment (``--speed``; < 1 = slow motion, stretching it)."""
    return float(seg.get("speed") or 1.0)


def _coverage(seg: dict, duration: float) -> tuple[float, float] | None:
    """The song-timeline interval a segment's picture covers."""
    if seg.get("shift") is None:
        return None
    src_start, src_end = _src_range(seg)
    a = max(0.0, float(seg["shift"]) + src_start)
    b = min(duration, float(seg["shift"]) + src_start + (src_end - src_start) / _speed(seg))
    return (a, b) if b - a > 0.05 else None


def _seg_path(store: JobStore, seg: dict) -> Path:
    f = seg["file"]
    p = Path(f)
    return p if p.is_absolute() else store.abs(f)


# ------------------------------------------------------------------ download
def _download(store: JobStore, url: str) -> tuple[Path, dict]:
    """yt-dlp download of an OP/ED source into ``media/oped_src/`` (picture + audio kept
    for the alignment; only the picture reaches the background)."""
    from .mvvideo import _pick_format, _service

    svc, service = _service()
    from krok_helper.video_download.download_task import DownloadOptions, DownloadTask

    vi = service.extract_info(url)
    fmt = _pick_format(vi.formats, 1080)
    if fmt is None:
        raise RuntimeError("找不到可下载的视频格式")
    out_dir = store.dir / "media" / "oped_src"
    out_dir.mkdir(parents=True, exist_ok=True)
    store.set_status(f"正在下载 OP/ED 视频：{vi.title}", "working")
    task = DownloadTask(task_id=f"oped{int(time.time())}", url=vi.webpage_url or url, title=vi.title,
                        source=vi.source, selected_format=fmt, info=vi)
    options = DownloadOptions(save_dir=str(out_dir), merge_video_audio=True, retry_count=3, timeout=30)
    with contextlib.redirect_stdout(sys.stderr):  # yt-dlp's progress bar must not pollute stdout
        service.download(task, options, lambda _e: None)
    if not task.local_file or not Path(task.local_file).is_file():
        raise RuntimeError("下载完成但找不到视频文件")
    return Path(task.local_file), {"title": vi.title, "uploader": vi.uploader, "url": vi.webpage_url or url}


# ------------------------------------------------------------------ add
def add_segments(store: JobStore, srcs: list[str], labels: list[str], credits: list[str],
                 sections: list[str], speeds: list[str]) -> list[dict]:
    from . import media, tracks
    from .analysis import timing_audio

    st = store.load()
    duration = _song_duration(st)
    ref = timing_audio(store, st)
    segments = _segments(st)
    results = []
    for k, src in enumerate(srcs):
        label = labels[k] if k < len(labels) else None
        credit = credits[k] if k < len(credits) else None
        section = sections[k] if k < len(sections) else None
        speed = None
        if k < len(speeds):
            try:
                speed = float(speeds[k])
            except ValueError:
                raise SystemExit(f"--speed 应是小数：{speeds[k]}")
            if not (0.1 <= speed <= 4.0):
                raise SystemExit(f"--speed 应在 0.1–4.0 之间：{speeds[k]}")
            if speed == 1.0:
                speed = None
        src = str(src).strip().strip('"')
        meta: dict = {}
        if re.match(r"https?://", src):
            path, meta = _download(store, src)
            file_rel = store.rel(path)
        else:
            path = Path(src)
            if not path.is_file():
                raise SystemExit(f"找不到视频文件：{path}")
            file_rel = store.rel(path)  # outside the job it stays absolute
        info = media.probe(path)
        if not info.get("has_video"):
            raise SystemExit(f"不是视频文件：{path}")
        src_start = src_end = None
        if section:
            src_start, src_end = _parse_section(section, float(info.get("duration") or 0.0))
        used = [int(s["id"][1:]) for s in segments if re.fullmatch(r"s\d+", str(s.get("id") or ""))]
        seg_id = f"s{(max(used) if used else 0) + 1}"
        store.set_status(f"正在对齐 {label or path.name}", "working")
        al = tracks.estimate_shift(ref, path, log=store.log)
        seg = {"id": seg_id, "source": src, "file": file_rel,
               "label": label or meta.get("title") or path.stem,
               "credit": credit or meta.get("uploader"),
               "duration": round(float(info.get("duration") or 0.0), 3),
               "fps": float(info.get("fps") or 0.0),
               "src_start": src_start, "src_end": src_end, "speed": speed,
               "shift": al["shift"], "confidence": al["confidence"], "consistent": al["consistent"]}
        segments.append(seg)
        cov = _coverage(seg, duration)
        cov_txt = f"覆盖歌曲 {cov[0]:.1f}s–{cov[1]:.1f}s" if cov else "不覆盖歌曲任何区间"
        sec_txt = f"（源区间 {src_start:.1f}s–{src_end:.1f}s）" if section else ""
        spd_txt = f" ×{speed:g} 慢放" if speed and speed < 1 else (f" ×{speed:g} 快放" if speed else "")
        print(f"段 {seg_id}「{seg['label']}」{sec_txt}{spd_txt}：shift={al['shift']:+.2f}s 置信 {al['confidence']:.0%} {cov_txt}")
        if al["confidence"] < 0.2:
            print(f"⚠ 段 {seg_id} 的音频与歌曲对不上（置信 {al['confidence']:.0%}），可能不是同一首歌 / 同一版本；"
                  f"确认无误可用 --shift {seg_id}=秒 手动指定偏移")
            store.log(f"OPED 段 {seg_id} 对齐置信过低：{al['confidence']:.0%}")
        results.append(seg)
    store.update(lambda s: s["media"].setdefault("oped", {"fade": DEFAULT_FADE}).update(segments=segments))
    return results


# ------------------------------------------------------------------ edits
def set_shift(store: JobStore, specs: list[str]) -> list[str]:
    done = []
    for spec in specs:
        sid, _, val = spec.partition("=")
        sid, val = sid.strip(), val.strip()
        if not sid or not val:
            raise SystemExit(f"--shift 格式应为 sN=秒：{spec}")

        def apply(s: dict, sid=sid, sec=float(val)) -> None:
            segs = _segments(s)
            seg = next((x for x in segs if x.get("id") == sid), None)
            if seg is None:
                raise SystemExit(f"没有段 {sid}（现有：{', '.join(x['id'] for x in segs) or '无'}）")
            seg["shift"] = sec
            seg["confidence"] = None
            seg["consistent"] = None
            s["media"]["oped"]["segments"] = segs

        store.update(apply)
        done.append(f"段 {sid} 的 shift 已手动设为 {float(val):+.2f}s（需重新 --render）")
        store.log(done[-1])
    return done


def remove_segment(store: JobStore, sid: str) -> str:
    def apply(s: dict) -> None:
        segs = _segments(s)
        keep = [x for x in segs if x.get("id") != sid]
        if len(keep) == len(segs):
            raise SystemExit(f"没有段 {sid}")
        s["media"]["oped"]["segments"] = keep

    store.update(apply)
    store.log(f"OPED：移除段 {sid}")
    return f"已移除段 {sid}"


def clear(store: JobStore) -> str:
    store.update(lambda s: s["media"].pop("oped", None))
    store.log("OPED：清空全部拼接段")
    return "已清空 OPED 拼接设置"


# ------------------------------------------------------------------ plan
def _plan(segments: list[dict], duration: float) -> list[dict]:
    """Scan 0..duration: every moment takes the earliest-added segment that covers it.
    Returns runs: {"kind": "video"|"gap", "seg"?, "t0", "t1"}."""
    covs = [_coverage(s, duration) for s in segments]
    bounds = {0.0, duration}
    for c in covs:
        if c:
            bounds.update(c)
    pts = sorted(bounds)
    runs: list[dict] = []
    for a, b in zip(pts, pts[1:]):
        if b - a < 0.02:
            continue
        mid = (a + b) / 2
        winner = next((i for i, c in enumerate(covs) if c and c[0] - 1e-6 <= mid <= c[1] + 1e-6), None)
        kind = "gap" if winner is None else "video"
        seg = segments[winner] if winner is not None else None
        if runs and runs[-1]["kind"] == kind and runs[-1].get("seg") is seg:
            runs[-1]["t1"] = b
        else:
            runs.append({"kind": kind, "seg": seg, "t0": a, "t1": b})
    return runs


def _frame_yavg(path: Path, t: float) -> float | None:
    """Mean luma of the frame at ``t`` (black detection for freeze picks)."""
    res = subprocess.run([ffmpeg_exe(), "-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1",
                          "-vf", "signalstats,metadata=print:key=lavfi.signalstats.YAVG", "-f", "null", "-"],
                         capture_output=True, text=True, encoding="utf-8", errors="replace",
                         **hidden_subprocess_kwargs())
    m = re.findall(r"YAVG=([0-9.]+)", res.stderr)
    return float(m[-1]) if m else None


def _freeze_frame(store: JobStore, seg: dict, t_song: float, w: int, h: int) -> Path:
    """The frame of ``seg`` shown at song time ``t_song`` (for filling a later gap).
    Videos often fade to black in their last second — step back until the frame has content."""
    path = _seg_path(store, seg)
    src_start, src_end = _src_range(seg)
    t_vid = max(src_start, t_song - float(seg["shift"]))
    t_vid = min(t_vid, max(src_start, src_end - 0.2))  # never past the used range's last frame
    chosen = t_vid
    for back in (0.0, 0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0):
        t = t_vid - back
        if t < src_start:
            break
        y = _frame_yavg(path, t)
        if y is not None and y > 24:
            chosen = t
            break
    png = store.path("media", "oped_freeze", f"{seg['id']}_{chosen:.2f}.png")
    if not png.exists():
        res = subprocess.run([ffmpeg_exe(), "-y", "-v", "error", "-ss", f"{chosen:.3f}",
                              "-i", str(path), "-frames:v", "1", str(png)],
                             capture_output=True, text=True, encoding="utf-8", errors="replace",
                             **hidden_subprocess_kwargs())
        if res.returncode != 0 or not png.exists():
            raise RuntimeError(f"抽取定格帧失败：{res.stderr.strip()[-300:]}")
    return png


# bump when the stitching / gap-fill logic changes: the render cache keys on this
PLAN_VERSION = 6


def _sig(segments: list[dict], fade: float, w: int, h: int, fps: int, duration: float) -> str:
    payload = {"v": PLAN_VERSION,
               "segs": [{k: s.get(k) for k in ("file", "shift", "duration", "src_start", "src_end", "speed")}
                        for s in segments],
               "fade": fade, "w": w, "h": h, "fps": fps, "duration": round(duration, 3)}
    return hashlib.sha1(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


# ------------------------------------------------------------------ render
def render(store: JobStore, *, force: bool = False) -> dict:
    st = store.load()
    segments = _segments(st)
    if not segments:
        raise SystemExit("还没有 OP/ED 拼接段：先 KM oped <job> --add <视频>")
    for s in segments:
        if s.get("shift") is None:
            raise SystemExit(f"段 {s['id']} 尚未对齐：先 --add 让它自动对齐，或 --shift {s['id']}=秒 手动指定")
        if not _seg_path(store, s).is_file():
            raise SystemExit(f"段 {s['id']} 的视频文件不存在：{s['file']}")
    duration = _song_duration(st)
    fade = _fade(st)
    w, h = (int(x) for x in str((st.get("options") or {}).get("resolution") or "1920x1080").lower().split("x"))
    fps = int((st.get("options") or {}).get("fps") or 60)
    sig = _sig(segments, fade, w, h, fps, duration)
    out = store.path("media", "oped_bg.mp4")
    if not force and _state(st).get("sig") == sig and out.is_file():
        print("拼接结果未变化，复用 media/oped_bg.mp4（--force 强制重渲）")
        return {"video": store.rel(out), "reused": True, "notes": _state(st).get("notes") or []}

    runs = _plan(segments, duration)
    # sliver gaps shorter than a fade (rounding between anchored sections): absorb
    # into the previous video run's cloned tail instead of making a freeze segment
    absorbed: dict[int, float] = {}
    merged: list[dict] = []
    for r in runs:
        if r["kind"] == "gap" and merged and merged[-1]["kind"] == "video" and r["t1"] - r["t0"] <= fade:
            absorbed[id(merged[-1])] = r["t1"] - r["t0"]
            merged[-1]["t1"] = r["t1"]
            continue
        merged.append(r)
    runs = merged
    notes: list[str] = []
    # gap fills: black at the very start, otherwise the previous part's last frame
    prev_video = None
    for r in runs:
        if r["kind"] == "video":
            prev_video = r
            continue
        if r["t0"] <= 0.01 or prev_video is None:
            r["kind"] = "black"
            notes.append(f"歌曲 {r['t0']:.1f}s–{r['t1']:.1f}s 无源覆盖：黑场")
        else:
            r["kind"] = "freeze"
            r["png"] = _freeze_frame(store, prev_video["seg"], prev_video["t1"], w, h)
            notes.append(f"歌曲 {r['t0']:.1f}s–{r['t1']:.1f}s 无源覆盖：定格段 {prev_video['seg']['id']} 的最后一帧")
    for r in runs:
        if r["kind"] == "video":
            seg = r["seg"]
            extra = absorbed.get(id(r))
            tail = f"，末尾 {extra:.1f}s 空隙并入收尾淡化" if extra else ""
            notes.append(f"歌曲 {r['t0']:.1f}s–{r['t1']:.1f}s：段 {seg['id']}「{seg.get('label') or seg['file']}」"
                         f"（shift {seg['shift']:+.2f}s）{tail}")

    # ffmpeg: one input per source file (split when a file serves several runs),
    # one per freeze frame; a synthesized colour stream for black runs
    inputs: list[tuple] = []  # ("video", path) | ("freeze", png, duration)
    file_inputs: dict[str, int] = {}
    run_input: list[int] = []
    for r in runs:
        if r["kind"] == "video":
            key = str(_seg_path(store, r["seg"]))
            if key not in file_inputs:
                file_inputs[key] = len(inputs)
                inputs.append(("video", key))
            run_input.append(file_inputs[key])
        elif r["kind"] == "freeze":
            run_input.append(len(inputs))
            inputs.append(("freeze", r["png"], r["t1"] - r["t0"]))
        else:
            run_input.append(-1)

    filters: list[str] = []
    use_count: dict[int, int] = {}
    for i in run_input:
        if i >= 0:
            use_count[i] = use_count.get(i, 0) + 1
    used: dict[int, int] = {}
    for i, n in use_count.items():
        if n > 1:
            filters.append(f"[{i}:v]split={n}" + "".join(f"[v{i}_{k}]" for k in range(n)))
    durs = [r["t1"] - r["t0"] for r in runs]
    fades = [min(fade, durs[k - 1] / 2, durs[k] / 2) for k in range(1, len(runs))]
    # a run feeds the fade into the next one from a cloned tail (tpad), so plan
    # positions stay absolute — the overlap is NOT subtracted from the timeline;
    # an absorbed sliver gap extends the same tail
    pads = [((fades[k] if fades[k] >= 0.04 else 0.0) + absorbed.get(id(runs[k]), 0.0))
            for k in range(len(fades))] + [absorbed.get(id(runs[-1]), 0.0) if runs else 0.0]
    for k, r in enumerate(runs):
        d = r["t1"] - r["t0"]
        tag = f"r{k}"
        tail = f",tpad=stop_mode=clone:stop_duration={pads[k]:.3f}" if pads[k] else ""
        if r["kind"] == "black":
            filters.append(f"color=c=black:s={w}x{h}:r={fps}:d={d:.3f},format=yuv420p,setsar=1,settb=AVTB{tail}[{tag}]")
            continue
        i = run_input[k]
        if use_count[i] > 1:
            j = used.get(i, 0)
            used[i] = j + 1
            src_pad = f"[v{i}_{j}]"
        else:
            src_pad = f"[{i}:v]"
        norm = (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
                f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black,fps={fps},format=yuv420p,setsar=1,settb=AVTB")
        if r["kind"] == "freeze":
            filters.append(f"{src_pad}{norm}{tail}[{tag}]")
        else:
            seg = r["seg"]
            src_start, src_end = _src_range(seg)
            shift = float(seg["shift"])
            spd = _speed(seg)
            # plan time t -> source time src_start + (t - shift - src_start) * speed
            start = max(src_start, src_start + (r["t0"] - shift - src_start) * spd)
            end = min(src_end, src_start + (r["t1"] - shift - src_start) * spd)
            slow = f"setpts=PTS/{spd:g}," if spd != 1.0 else ""
            filters.append(f"{src_pad}trim=start={start:.3f}:duration={end - start:.3f},setpts=PTS-STARTPTS,"
                           f"{slow}{norm}{tail}[{tag}]")

    cur = "[r0]"
    for k in range(1, len(runs)):
        f = fades[k - 1]
        nxt = f"x{k}"
        if f < 0.04:  # too short to fade: hard cut
            filters.append(f"{cur}[r{k}]concat=n=2:v=1:a=0[{nxt}]")
        else:
            # the fade occupies the first f seconds of the new run: previous content
            # stays until the plan boundary, then cross-fades out of its frozen tail
            filters.append(f"{cur}[r{k}]xfade=transition=fade:duration={f:.3f}:offset={runs[k]['t0']:.3f}[{nxt}]")
        cur = f"[{nxt}]"
    filters.append(f"{cur}tpad=stop_mode=clone:stop_duration=0.5,format=yuv420p[outv]")

    cmd = [ffmpeg_exe(), "-y", "-v", "error"]
    for inp in inputs:
        if inp[0] == "freeze":
            cmd += ["-loop", "1", "-t", f"{inp[2]:.3f}"]
        cmd += ["-i", str(inp[1])]
    cmd += ["-filter_complex", ";".join(filters), "-map", "[outv]",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", "-an", "-t", f"{duration:.3f}", str(out)]
    store.set_status("正在拼接 OP/ED 背景视频", "working")
    store.log(f"OPED 拼接：{len(runs)} 条流（{len(segments)} 段视频，淡化 {fade:.2f}s）")
    res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                         **hidden_subprocess_kwargs())
    if res.returncode != 0:
        store.set_status("OPED 拼接失败", "error")
        raise RuntimeError("拼接 OP/ED 背景失败：" + res.stderr[-600:])

    still = store.path("media", "oped_still.jpg")
    subprocess.run([ffmpeg_exe(), "-y", "-v", "error", "-ss", f"{min(duration * 0.4, 60):.2f}", "-i", str(out),
                    "-frames:v", "1", "-vf", "scale=1280:-2", str(still)], capture_output=True,
                   **hidden_subprocess_kwargs())
    result = {"segments": segments, "fade": fade, "video": store.rel(out),
              "still": store.rel(still) if still.exists() else None, "sig": sig, "notes": notes}

    def apply(s: dict) -> None:
        s["media"]["oped"] = result
        s.setdefault("options", {})["background"] = {"type": "oped"}

    store.update(apply)
    store.log(f"OPED 拼接背景已生成：{out.name}（{duration:.1f}s，{len(runs)} 条流）")
    store.set_status("OPED 拼接背景已就绪", None)
    print(f"已生成 media/oped_bg.mp4（{duration:.1f}s）并设为背景")
    return {"video": store.rel(out), "still": result["still"], "reused": False, "notes": notes}


# ------------------------------------------------------------------ list / main
def listing(store: JobStore) -> dict:
    st = store.load()
    op = _state(st)
    segments = _segments(st)
    duration = _song_duration(st)
    rows = []
    for s in segments:
        cov = _coverage(s, duration)
        src_start, src_end = _src_range(s)
        rows.append({"id": s.get("id"), "label": s.get("label"), "file": s.get("file"),
                     "duration": s.get("duration"), "shift": s.get("shift"),
                     "confidence": s.get("confidence"),
                     "src_start": s.get("src_start"), "src_end": s.get("src_end"),
                     "src_range": [round(src_start, 2), round(src_end, 2)],
                     "speed": _speed(s),
                     "covers": [round(cov[0], 2), round(cov[1], 2)] if cov else None,
                     "credit": s.get("credit")})
    if not segments:
        print("尚无 OP/ED 拼接段：KM oped <job> --add <视频路径或URL> --label \"…\"")
    for s, row in zip(segments, rows):
        shift = f"{s['shift']:+.2f}s" if s.get("shift") is not None else "未对齐"
        conf = f"置信 {s['confidence']:.0%}" if s.get("confidence") is not None else "手动偏移"
        cov = f"覆盖歌曲 {row['covers'][0]:.1f}s–{row['covers'][1]:.1f}s" if row["covers"] else "不覆盖歌曲"
        spd = _speed(s)
        spd_txt = f"  ×{spd:g} 慢放" if spd < 1 else (f"  ×{spd:g} 快放" if spd > 1 else "")
        print(f"{row['id']}  {row['label'] or row['file']}  源区间 {row['src_range'][0]:.1f}s–{row['src_range'][1]:.1f}s"
              f"（全长 {row['duration']:.1f}s）{spd_txt}  {shift}  {conf}  {cov}")
    if segments:
        video = op.get("video")
        print(f"淡化 {op.get('fade', DEFAULT_FADE):.2f}s · 拼接结果："
              + (f"{video}（已生成）" if video and store.abs(video).is_file() else "未生成（KM oped <job> --render）"))
        for n in op.get("notes") or []:
            print("  " + n)
    return {"segments": rows, "fade": op.get("fade", DEFAULT_FADE),
            "video": op.get("video"), "sig": op.get("sig"), "notes": op.get("notes") or []}


def main(store: JobStore, args) -> dict:
    out: dict = {}
    if getattr(args, "clear", False):
        out["clear"] = clear(store)
    if getattr(args, "add", None):
        out["added"] = add_segments(store, args.add, args.label or [], args.credit or [],
                                    getattr(args, "section", None) or [], getattr(args, "speed", None) or [])
    if getattr(args, "remove", None):
        out["removed"] = [remove_segment(store, sid) for sid in args.remove]
    if getattr(args, "shift", None):
        out["shifted"] = set_shift(store, args.shift)
    if getattr(args, "fade", None) is not None:
        if args.fade < 0 or args.fade > 5:
            raise SystemExit("--fade 应在 0–5 秒之间")
        store.update(lambda s: s["media"].setdefault("oped", {}).update(fade=float(args.fade)))
        store.log(f"OPED 交叉淡化设为 {args.fade:.2f}s（需重新 --render）")
        out["fade"] = args.fade
    if getattr(args, "render", False):
        out["render"] = render(store, force=getattr(args, "force", False))
    if getattr(args, "list", False) or not out:
        out["oped"] = listing(store)
    return out
