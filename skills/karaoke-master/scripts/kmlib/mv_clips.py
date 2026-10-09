"""Video-clip montage (视频片段混剪): beat-synced shots cut from game / anime footage.

Sources are videos found on YouTube / Bilibili (game trailers / PVs, opening
movies, gameplay recordings, cut-scene compilations) or local files. Each source
is downloaded once into a **footage library** shared by the projects next to
each other (``<projects>/_footage``): picture only, ≤ 1080p, and for long videos
only the sections the agent picked. A scan splits every file into shots (scene
cuts; long continuous shots in pieces) and measures brightness, motion, colour,
detail and black bars. Each usable shot becomes a ``clip`` asset in the job's
montage pool (``render/mv_assets/assets.json``, next to images)::

    {"id": "c…", "kind": "clip", "origin": "clip", "src": <library file>, "t_in", "t_out",
     "duration", "w", "h", "crop": [x, y, w, h] | None, "motion", "luma", "color", "score",
     "source": <video url with time>, "credit", "name", "tags", "ahash"}

The montage planner (``mv.plan_montage``) fills each beat-synced shot with an
unused clip whose length fits and whose motion matches the music's energy;
:class:`ClipReader` decodes the frames while the background renders. Clip ids
depend only on the library file and the in-point, so two projects using the
same library share ids (``--avoid-from`` keeps a second song from repeating the
first one's shots).
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import shutil
import struct
import subprocess
import tempfile
import time
import urllib.parse
from pathlib import Path

import numpy as np

from . import mv_assets
from .jobstore import JobStore, read_json, write_json
from .paths import ffmpeg_exe, hidden_subprocess_kwargs, km_home

SCAN_FPS = 6.0
SCAN_W, SCAN_H = 160, 90
VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi", ".flv", ".ts"}


# ------------------------------------------------------------------ library
def library_dir(store: JobStore, library: str | None = None) -> Path:
    """``<projects>/_footage`` (shared by sibling projects), a sub folder of it
    (``library`` = name) or any absolute folder."""
    base = store.dir.parent / "_footage"
    lib = Path(library) if library and Path(library).is_absolute() else base / library if library else base
    lib.mkdir(parents=True, exist_ok=True)
    return lib


def _proxy() -> str:
    return os.environ.get("KM_PROXY") or os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or ""


def _ydl_opts(**extra) -> dict:
    opts = {"quiet": True, "no_warnings": True, "noprogress": True}
    if _proxy():
        opts["proxy"] = _proxy()
    opts.update(extra)
    return opts


def _source_id(url: str, info: dict | None = None) -> str:
    vid = (info or {}).get("id") or hashlib.sha1(url.encode("utf-8")).hexdigest()[:11]
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(vid))


def info(url: str) -> dict:
    """Title / channel / duration / best picture-only format (≤1080p) — no download."""
    import yt_dlp

    vi, last = None, None
    for clients in CLIENTS:  # "Sign in to confirm you're not a bot" / 403 hit one client at a time
        opts = _ydl_opts(skip_download=True)
        if clients:
            opts["extractor_args"] = {"youtube": {"player_client": clients}}
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                vi = ydl.extract_info(url, download=False) or {}
            break
        except yt_dlp.utils.DownloadError as exc:
            last = exc
    if vi is None:
        raise RuntimeError(f"无法读取视频信息：{last}")
    fmts = [f for f in vi.get("formats") or [] if (f.get("vcodec") or "none") != "none" and (f.get("height") or 0)]
    best = max((f for f in fmts if f["height"] <= 1080), key=lambda f: (f["height"], f.get("tbr") or 0), default=None)
    return {"url": vi.get("webpage_url") or url, "id": vi.get("id"), "title": vi.get("title"),
            "channel": vi.get("channel") or vi.get("uploader"), "duration": vi.get("duration"),
            "max_height": max((f["height"] for f in fmts), default=None),
            "format": f"{best['height']}p {best.get('vcodec')}" if best else None,
            "chapters": [{"start": c.get("start_time"), "end": c.get("end_time"), "title": c.get("title")}
                         for c in vi.get("chapters") or []]}


def parse_sections(spec: str | None) -> list[tuple[float, float]]:
    """``"60-180, 7:30-9:00"`` → [(60, 180), (450, 540)] (seconds or m:ss)."""
    def sec(x: str) -> float:
        parts = [float(p) for p in x.strip().split(":")]
        return sum(v * 60 ** (len(parts) - 1 - k) for k, v in enumerate(parts))

    out = []
    for part in (spec or "").replace("，", ",").split(","):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"^([\d:.]+)\s*[-~–—～]\s*([\d:.]+)$", part)
        if not m:  # never fall back to downloading a whole multi-hour video
            raise SystemExit(f"--sections 无法解析：{part}（写法如 60-180 或 7:30-9:00）")
        a, b = sec(m.group(1)), sec(m.group(2))
        if b <= a:
            raise SystemExit(f"--sections 区间的结束早于开始：{part}")
        out.append((a, b))
    return out


def download(store: JobStore, url: str, *, sections: list[tuple[float, float]] | None = None, max_height: int = 1080,
             library: str | None = None, credit: str | None = None) -> dict:
    """Picture-only download into the footage library (re-uses files already there).
    Returns the library record of the source."""
    import yt_dlp

    lib = library_dir(store, library)
    index = read_json(lib / "sources.json", {}) or {}
    vi = info(url)
    sid = _source_id(url, vi)
    d = lib / sid
    d.mkdir(exist_ok=True)
    rec = index.get(sid) or {"id": sid, "url": vi["url"], "title": vi["title"], "channel": vi["channel"],
                             "duration": vi["duration"], "files": []}
    if credit:
        rec["credit"] = credit
    rec.setdefault("credit", f"{vi['title']}（{vi['channel']}）")
    h = int(max_height)
    fmt = (f"bv*[height<={h}][vcodec^=avc1]/bv*[height<={h}][vcodec^=vp09]/bv*[height<={h}][vcodec^=vp9]/"
           f"bv*[height<={h}]/b[height<={h}]")
    wanted = sections or [(0.0, float(vi["duration"] or 0))]
    have = {tuple(round(x) for x in f.get("section") or (f["start"], f["end"])) for f in rec["files"]
            if (d / f["file"]).is_file()}
    todo = [(a, b) for a, b in wanted if (round(a), round(b)) not in have]

    def save() -> None:  # after every file, so a later failure cannot lose earlier downloads
        rec["files"].sort(key=lambda f: f["start"])
        latest = read_json(lib / "sources.json", {}) or {}
        latest[sid] = rec
        write_json(lib / "sources.json", latest)

    if sections and todo:  # only the fragments that cover the sections (no full download)
        done = _fetch_sections(store, vi["url"], todo, d, max_height=h, title=vi["title"] or sid)
        rec["files"] += done
        save()
        todo = [(a, b) for a, b in todo if [a, b] not in [f["section"] for f in done]]
        if todo:
            store.log(f"分段下载失败，改用 yt-dlp 切段下载：{todo}")
    for a, b in todo:
        stem = f"{int(a):05d}-{int(b):05d}"
        opts = _ydl_opts(format=fmt, outtmpl=str(d / f"{stem}.%(ext)s"), retries=3, fragment_retries=3,
                         concurrent_fragment_downloads=4)
        if sections:
            from yt_dlp.utils import download_range_func

            opts["download_ranges"] = download_range_func(None, [(a, b)])
            opts["force_keyframes_at_cuts"] = False
            # ffmpeg reads the stream itself here: give up after 30 s without data instead of hanging
            opts["external_downloader_args"] = {"ffmpeg_i": ["-rw_timeout", "30000000"]}
        store.set_status(f"正在下载素材视频：{(vi['title'] or sid)[:30]}（{stem}）", "working")
        (d / f"{stem}.frag.mp4").unlink(missing_ok=True)
        last_err = None
        # YouTube often answers 403 to the default client: retry with the player clients
        # Lin-K Lyrics' downloader falls back to (krok_helper.video_download.ytdlp_service)
        for clients in (None, ["default", "web_embedded"], ["visionos", "android_vr", "web"], ["android_vr"]):
            try_opts = dict(opts)
            if clients:
                try_opts["extractor_args"] = {"youtube": {"player_client": clients}}
            try:
                with yt_dlp.YoutubeDL(try_opts) as ydl:
                    ydl.download([vi["url"]])
                last_err = None
                break
            except yt_dlp.utils.DownloadError as exc:
                last_err = exc
                for part in d.glob(f"{stem}.*"):
                    if part.suffix.lower() in (".part", ".ytdl") or part.name.endswith(".part"):
                        part.unlink(missing_ok=True)
        if last_err is not None:
            raise RuntimeError(f"下载失败：{last_err}")
        got = sorted(p for p in d.glob(f"{stem}.*") if p.suffix.lower() in VIDEO_EXTS and p.stem == stem)
        if not got:
            raise RuntimeError(f"下载完成但找不到文件：{stem}")
        rec["files"].append({"file": got[0].name, "start": a, "end": b, "section": [a, b]})
        save()
    save()
    return rec


CLIENTS = (None, ["default", "web_embedded"], ["visionos", "android_vr", "web"], ["android_vr"])


def _boxes(data: bytes, start: int = 0):
    """Top-level MP4 boxes: (type, offset, size, header length)."""
    pos = start
    while pos + 8 <= len(data):
        size, typ = struct.unpack(">I4s", data[pos:pos + 8])
        hdr = 8
        if size == 1:
            size, hdr = struct.unpack(">Q", data[pos + 8:pos + 16])[0], 16
        elif size == 0:
            size = len(data) - pos
        yield typ.decode("latin1"), pos, size, hdr
        if size < 8:
            return
        pos += size


def _parse_sidx(data: bytes, pos: int, hdr: int) -> tuple[int, int, int, list[tuple[int, int]]]:
    """Segment index: timescale, earliest presentation time, first offset, [(bytes, duration)]."""
    p = pos + hdr
    version = data[p]
    p += 8  # version/flags + reference_ID
    timescale = struct.unpack(">I", data[p:p + 4])[0]
    p += 4
    if version == 0:
        ept, first = struct.unpack(">II", data[p:p + 8])
        p += 8
    else:
        ept, first = struct.unpack(">QQ", data[p:p + 16])
        p += 16
    count = struct.unpack(">H", data[p + 2:p + 4])[0]
    p += 4
    refs = []
    for _ in range(count):
        size, dur, _sap = struct.unpack(">III", data[p:p + 12])
        refs.append((size & 0x7FFFFFFF, dur))
        p += 12
    return timescale, ept, first, refs


def _fetch_sections(store: JobStore, url: str, sections: list[tuple[float, float]], d: Path, *,
                    max_height: int, title: str) -> list[dict]:
    """Download only the parts of a long video that cover ``sections``.

    YouTube's picture-only MP4 streams carry a segment index (sidx): the init
    data and just the fragments overlapping a section are fetched with HTTP
    range requests through yt-dlp's own session, then remuxed to a plain MP4
    that starts at 0. (yt-dlp's ``download_ranges`` hands the stream URL to
    ffmpeg instead; behind a proxy whose exit address changes per connection
    the URL is bound to another IP and ffmpeg gets 403 or hangs.)
    Returns the library file records that were written."""
    import yt_dlp
    from yt_dlp.networking import Request

    done: list[dict] = []
    for clients in CLIENTS:
        opts = _ydl_opts(skip_download=True)
        if clients:
            opts["extractor_args"] = {"youtube": {"player_client": clients}}
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                vi = ydl.extract_info(url, download=False) or {}
                fmts = [f for f in vi.get("formats") or [] if (f.get("vcodec") or "none") != "none"
                        and f.get("acodec") == "none" and f.get("ext") == "mp4"
                        and str(f.get("protocol", "")).startswith("http") and 0 < (f.get("height") or 0) <= max_height]
                if not fmts:
                    continue
                fmt = dict(max(fmts, key=lambda f: (f["height"], "avc1" in (f.get("vcodec") or ""), f.get("fps") or 0)))
                headers = dict(fmt.get("http_headers") or {})
                total = fmt.get("filesize") or (1 << 62)

                def refresh() -> None:
                    """A new stream URL for the same format: the old one is bound to the
                    address that asked for it, and this proxy's exit address may have moved."""
                    fresh = ydl.extract_info(url, download=False) or {}
                    same = next((f for f in fresh.get("formats") or [] if f.get("format_id") == fmt["format_id"]), None)
                    if same:
                        fmt["url"] = same["url"]
                        headers.update(same.get("http_headers") or {})

                def get(lo: int, hi: int) -> bytes:
                    last = None
                    for attempt in range(6):
                        try:
                            data = ydl.urlopen(Request(fmt["url"], headers={**headers, "Range": f"bytes={lo}-{hi}"})).read()
                            if len(data) >= hi - lo + 1 or hi >= total - 1:
                                return data[:hi - lo + 1]
                            last = RuntimeError(f"短读 {len(data)}/{hi - lo + 1}")
                        except Exception as exc:  # noqa: BLE001 - retried
                            last = exc
                            if "403" in str(exc):
                                try:
                                    refresh()
                                except Exception as exc2:  # noqa: BLE001
                                    last = exc2
                        time.sleep(1.5 * (attempt + 1))
                    raise RuntimeError(f"分段请求失败：{last}")

                head = get(0, 262143)
                moov_end = sidx = None
                for typ, pos, size, hdr in _boxes(head):
                    if typ == "moov":
                        moov_end = pos + size
                    if typ == "sidx":
                        sidx = (pos, size, hdr)
                        break
                if sidx is None or moov_end is None:
                    return done  # no segment index: let yt-dlp cut it
                pos, size, hdr = sidx
                if pos + size > len(head):
                    head += get(len(head), pos + size - 1)
                timescale, ept, first, refs = _parse_sidx(head, pos, hdr)
                segs, t, off = [], ept / timescale, pos + size + first
                for nbytes, dur in refs:
                    segs.append((t, t + dur / timescale, off, off + nbytes - 1))
                    t += dur / timescale
                    off += nbytes
                for a, b in sections:
                    if any(f["section"] == [a, b] for f in done):
                        continue
                    use = [sg for sg in segs if sg[1] > a and sg[0] < b]
                    if not use:
                        continue
                    stem = f"{int(a):05d}-{int(b):05d}"
                    tmp, out = d / f"{stem}.frag.mp4", d / f"{stem}.mp4"
                    lo, hi = use[0][2], use[-1][3]
                    step = 8 * 1024 * 1024
                    try:
                        with open(tmp, "wb") as fh:
                            fh.write(head[:moov_end])
                            for x in range(lo, hi + 1, step):
                                store.set_status(f"正在下载素材片段：{title[:24]}（{stem}，"
                                                 f"{(x - lo) / max(1, hi - lo + 1):.0%}）", "working")
                                fh.write(get(x, min(hi, x + step - 1)))
                        res = subprocess.run([ffmpeg_exe(), "-y", "-v", "error", "-i", str(tmp), "-map", "0:v:0", "-c",
                                              "copy", "-movflags", "+faststart", "-avoid_negative_ts", "make_zero", str(out)],
                                             capture_output=True, text=True, encoding="utf-8", errors="replace",
                                             **hidden_subprocess_kwargs())
                    finally:
                        tmp.unlink(missing_ok=True)
                    if res.returncode != 0 or not out.is_file():
                        raise RuntimeError("分段重封装失败：" + res.stderr[-300:])
                    done.append({"file": out.name, "start": round(use[0][0], 3), "end": round(use[-1][1], 3),
                                 "section": [a, b], "format": f"{fmt['height']}p {fmt.get('vcodec')}"})
                return done
        except Exception as exc:  # noqa: BLE001 - next player client
            store.log(f"分段下载（{clients or 'default'}）失败：{exc}")
    return done


def add_local(store: JobStore, path: str, *, library: str | None = None, credit: str | None = None) -> dict:
    """Register a local video (or a folder of videos) in the library without copying it."""
    lib = library_dir(store, library)
    index = read_json(lib / "sources.json", {}) or {}
    p = Path(path)
    files = sorted(f for f in (p.rglob("*") if p.is_dir() else [p]) if f.is_file() and f.suffix.lower() in VIDEO_EXTS)
    if not files:
        raise SystemExit(f"找不到视频文件：{path}")
    sid = "local_" + hashlib.sha1(str(p.resolve()).encode("utf-8")).hexdigest()[:10]
    rec = {"id": sid, "url": None, "title": p.stem, "channel": None, "duration": None,
           "credit": credit or "用户提供的视频", "files": [{"file": str(f.resolve()), "start": 0.0, "end": None}
                                                     for f in files]}
    index[sid] = rec
    write_json(lib / "sources.json", index)
    return rec


def _file_path(lib: Path, rec: dict, f: dict) -> Path:
    p = Path(f["file"])
    return p if p.is_absolute() else lib / rec["id"] / p


# ------------------------------------------------------------------ scan
def _bars(lum: np.ndarray, axis: int, thr: float = 0.07) -> tuple[int, int]:
    """Black bars: leading / trailing rows (axis 0) or columns (axis 1) that stay
    dark in 97 % of the sampled frames (``lum``: uint8 luma frames)."""
    prof = np.percentile(lum.mean(axis=2 if axis == 0 else 1), 97, axis=0) / 255.0
    dark = prof < thr
    n = len(dark)
    lead = int(np.argmin(dark)) if not dark.all() else n
    trail = int(np.argmin(dark[::-1])) if not dark.all() else n
    return lead, trail


def scan_file(path: Path, cache: Path | None = None) -> dict:
    """Shots of one video file. Cached as ``<file>.scan.json`` next to library files,
    or at ``cache`` (the library's ``_scan`` folder for the user's own videos)."""
    cache = cache or path.with_name(path.name + ".scan.json")
    st_ = path.stat()
    if cache.is_file() and cache.stat().st_mtime >= st_.st_mtime:
        old = read_json(cache) or {}
        if old.get("src_size", st_.st_size) == st_.st_size and old.get("shots") is not None:
            return old
    cmd = [ffmpeg_exe(), "-v", "error", "-threads", "2", "-i", str(path), "-an", "-sn", "-vf",
           f"fps={SCAN_FPS:g},scale={SCAN_W}:{SCAN_H}:flags=area", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    fsz = SCAN_W * SCAN_H * 3
    lum_parts, diff_parts, color_parts = [], [], []
    prev = None
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, **hidden_subprocess_kwargs())
    try:  # streamed in blocks: only uint8 luma is kept (~0.3 GB per hour of footage)
        while True:
            raw = proc.stdout.read(fsz * 256)
            m = len(raw) // fsz
            if m == 0:
                break
            f = np.frombuffer(raw[:m * fsz], np.uint8).reshape(m, SCAN_H, SCAN_W, 3).astype(np.float32) / 255.0
            lum_parts.append(np.round((f[..., 0] * 0.299 + f[..., 1] * 0.587 + f[..., 2] * 0.114) * 255).astype(np.uint8))
            rg = f[..., 0] - f[..., 1]
            yb = 0.5 * (f[..., 0] + f[..., 1]) - f[..., 2]
            color_parts.append(np.sqrt(rg.std(axis=(1, 2)) ** 2 + yb.std(axis=(1, 2)) ** 2)
                               + 0.3 * np.sqrt(rg.mean(axis=(1, 2)) ** 2 + yb.mean(axis=(1, 2)) ** 2))
            diff_parts.append(np.abs(np.diff(np.concatenate([prev if prev is not None else f[:1], f]), axis=0))
                              .mean(axis=(1, 2, 3)))
            prev = f[-1:]
    finally:
        proc.stdout.close()
        proc.wait()
    n = sum(len(x) for x in lum_parts)
    if n < 6:
        raise RuntimeError(f"无法解码视频：{path.name}")
    lum = np.concatenate(lum_parts)
    diff = np.concatenate(diff_parts).astype(np.float32)
    color = np.concatenate(color_parts).astype(np.float32)
    diff[0] = 0.0
    top, bottom = _bars(lum, 0)
    left, right = _bars(lum, 1)
    crop = None
    if top + bottom > SCAN_H * 0.06 or left + right > SCAN_W * 0.06:
        if top + bottom < SCAN_H * 0.7 and left + right < SCAN_W * 0.7:
            crop = [round(left / SCAN_W, 4), round(top / SCAN_H, 4),
                    round((SCAN_W - left - right) / SCAN_W, 4), round((SCAN_H - top - bottom) / SCAN_H, 4)]
    # crop the measurement to the picture area
    y0, y1, x0, x1 = top, SCAN_H - bottom, left, SCAN_W - right
    if y1 - y0 < 10 or x1 - x0 < 10:
        y0, y1, x0, x1 = 0, SCAN_H, 0, SCAN_W
    inner = lum[:, y0:y1, x0:x1]
    luma = inner.mean(axis=(1, 2)) / 255.0
    detail = np.concatenate([(inner[a:a + 256].astype(np.float32) / 255.0).std(axis=(1, 2)) for a in range(0, n, 256)])
    # hard cuts: a jump well above the local level; flashes (one bright frame) cut on both sides
    cuts = []
    for i in range(1, n):
        lo, hi = max(1, i - 8), min(n, i + 9)
        local = np.median(np.concatenate([diff[lo:i], diff[i + 1:hi]])) if hi - lo > 2 else 0.0
        if diff[i] > 0.075 and diff[i] > 2.6 * local + 0.02:
            if not cuts or i - cuts[-1] >= 2:
                cuts.append(i)
    bounds = [0] + cuts + [n]
    shots = []
    for a, b in zip(bounds, bounds[1:]):
        a2, b2 = a + 2, b - 2  # keep away from the cut (scan resolution 1/6 s)
        if (b2 - a2) / SCAN_FPS < 1.0:
            continue
        dur = (b2 - a2) / SCAN_FPS
        pieces = max(1, math.ceil(dur / 7.0)) if dur > 9.0 else 1
        step = (b2 - a2) / pieces
        for k in range(pieces):
            s, e = int(a2 + k * step), int(a2 + (k + 1) * step)
            if (e - s) / SCAN_FPS < 1.0:
                continue
            mid = (s + e) // 2
            mo = float(np.median(diff[s + 1:e])) if e - s > 2 else 0.0
            small = lum[mid, y0:y1, x0:x1].astype(np.float32) / 255.0
            sm8 = small[: (small.shape[0] // 8) * 8, : (small.shape[1] // 8) * 8]
            sm8 = sm8.reshape(8, sm8.shape[0] // 8, 8, sm8.shape[1] // 8).mean(axis=(1, 3))
            bits = (sm8 >= sm8.mean()).flatten()
            ah = int("".join("1" if x else "0" for x in bits), 2)
            shots.append({"t_in": round(s / SCAN_FPS, 2), "t_out": round(e / SCAN_FPS, 2),
                          "duration": round((e - s) / SCAN_FPS, 2), "motion": round(mo, 4),
                          "luma": round(float(luma[s:e].mean()), 3), "color": round(float(color[s:e].mean()), 3),
                          "detail": round(float(detail[s:e].mean()), 3), "ahash": str(ah),
                          "fade": bool(luma[s:e].min() < 0.04), "piece": k if pieces > 1 else None})
    probe_cmd = [ffmpeg_exe("ffprobe"), "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                 "-of", "csv=p=0", str(path)]
    wh = subprocess.run(probe_cmd, capture_output=True, text=True, **hidden_subprocess_kwargs()).stdout.strip()
    w, h = (int(x) for x in wh.split(",")[:2]) if "," in wh else (0, 0)
    out = {"file": path.name, "frames": n, "duration": round(n / SCAN_FPS, 2), "w": w, "h": h, "crop": crop,
           "cuts": len(cuts), "shots": shots, "src_size": st_.st_size}
    try:
        write_json(cache, out)
    except OSError:  # a read-only folder only costs the cache
        pass
    return out


def shot_score(s: dict) -> float:
    """0–1: colourful, bright enough, detailed, moving (but not a blur of motion)."""
    if s["luma"] < 0.07 or s["detail"] < 0.03:
        return 0.0
    mo = s["motion"]
    move = 1.0 if 0.006 <= mo <= 0.09 else 0.55 if mo < 0.006 else 0.6
    q = (0.32 * min(1.0, s["color"] / 0.22) + 0.24 * min(1.0, s["luma"] / 0.32) + 0.2 * min(1.0, s["detail"] / 0.17)
         + 0.24 * move)
    if s.get("fade"):
        q *= 0.6
    return round(q, 3)


# ------------------------------------------------------------------ pool
def clip_id(src: Path, t_in: float, *, local: bool = False) -> str:
    """Library files: name + folder (stable across projects sharing the library).
    The user's own files (``local``): the whole path, so two ``…/movies/op.mp4`` differ."""
    key = str(src.resolve()) if local else f"{src.name}|{src.parent.name}"
    return "c" + hashlib.sha1(f"{key}|{t_in:.2f}".encode("utf-8")).hexdigest()[:10]


def _thumb(src: Path, t: float, out: Path, crop: list | None) -> bool:
    vf = []
    if crop:
        x, y, w, h = crop
        vf.append(f"crop=iw*{w}:ih*{h}:iw*{x}:ih*{y}")
    vf += ["scale=320:180:force_original_aspect_ratio=increase", "crop=320:180"]
    subprocess.run([ffmpeg_exe(), "-y", "-v", "error", "-ss", f"{t:.2f}", "-i", str(src), "-frames:v", "1",
                    "-vf", ",".join(vf), "-q:v", "3", str(out)], capture_output=True, **hidden_subprocess_kwargs())
    return out.is_file()


def import_source(store: JobStore, rec: dict, *, library: str | None = None, min_score: float = 0.35,
                  max_clips: int = 80, crop: list | None = None, tags: list[str] | None = None,
                  near_dup_bits: int = 3, sections: list[tuple[float, float]] | None = None) -> dict:
    """Scan a library source and add its usable shots to the job's pool
    (``sections``: only the files downloaded for those parts — another song may
    have downloaded other parts of the same video)."""
    lib = library_dir(store, library)
    pool_d = mv_assets.pool_dir(store)
    found, unreadable = [], []
    want = {(round(a), round(b)) for a, b in sections or []}
    excluded = set(mv_assets.excluded_ids(store))  # shots a review removed never come back
    pool0 = {a["id"]: a for a in mv_assets.load_pool(store)}
    moved = 0
    for f in rec["files"]:
        # --sections picks downloaded parts; the user's own files have none and are always taken
        if want and f.get("section") and tuple(round(x) for x in f["section"]) not in want:
            continue
        path = _file_path(lib, rec, f)
        if not path.is_file():
            continue
        local = Path(f["file"]).is_absolute()
        store.set_status(f"正在分析素材镜头：{(rec.get('title') or rec['id'])[:30]}", "working")
        cache = (lib / "_scan" / f"{hashlib.sha1(str(path.resolve()).encode('utf-8')).hexdigest()[:16]}.scan.json"
                 if local else None)
        if cache is not None:
            cache.parent.mkdir(exist_ok=True)
        try:
            sc = scan_file(path, cache)
        except RuntimeError as exc:  # one unreadable file must not stop the others
            store.log(f"跳过无法分析的视频 {path.name}：{exc}")
            unreadable.append(path.name)
            continue
        use_crop = crop if crop is not None else sc.get("crop")
        for s in sc["shots"]:
            q = shot_score(s)
            cid = clip_id(path, s["t_in"], local=local)
            old = pool0.get(cid)
            if old is not None and old.get("src") != str(path):  # footage moved: point the clip at it
                old["src"] = str(path)
                moved += 1
            if q >= min_score and cid not in excluded and old is None:
                found.append((q, path, f, sc, s, use_crop, cid))
    already = sum(1 for a in pool0.values() if a.get("library_id") == rec["id"])
    if len(found) > max_clips:  # best new shots, spread over the source
        found.sort(key=lambda x: -x[0])
        found = found[:max_clips]
    found.sort(key=lambda x: (str(x[1]), x[4]["t_in"]))
    # thumbnails first, outside the pool lock (they take a while; the page may import at the same time)
    ready = []
    for item in found:
        q, path, f, sc, s, use_crop, cid = item
        if _thumb(path, (s["t_in"] + s["t_out"]) / 2, pool_d / "thumbs" / f"{cid}.jpg", use_crop):
            ready.append(item)
    added, dups = [], 0
    with mv_assets._pool_lock(store):
        pool = mv_assets.load_pool(store)
        for a in pool:  # moved footage (see above)
            if a["id"] in pool0 and pool0[a["id"]].get("src") != a.get("src"):
                a["src"] = pool0[a["id"]]["src"]
        have = {a["id"] for a in pool}
        hashes = [(int(a["ahash"]), a.get("src"), a.get("t_in")) for a in pool if a.get("kind") == "clip"]
        for q, path, f, sc, s, use_crop, cid in ready:
            if cid in have:
                continue
            ah = int(s["ahash"])
            # trailers re-use shots: drop near-identical pictures from other files / scenes
            if any(bin(ah ^ h).count("1") <= near_dup_bits and not (src == str(path) and abs((t or 0) - s["t_in"]) < 15)
                   for h, src, t in hashes):
                dups += 1
                (pool_d / "thumbs" / f"{cid}.jpg").unlink(missing_ok=True)
                continue
            orig_t = (f.get("start") or 0.0) + s["t_in"]
            url = rec.get("url")
            if url and "youtube.com" in url:
                link = url + ("&" if "?" in url else "?") + f"t={int(orig_t)}s"
            elif url:
                link = url
            else:
                link = str(path)
            entry = {"id": cid, "kind": "clip", "origin": "clip", "file": None, "src": str(path),
                     "t_in": s["t_in"], "t_out": s["t_out"], "duration": s["duration"], "w": sc["w"], "h": sc["h"],
                     "crop": use_crop, "motion": s["motion"], "luma": s["luma"], "color": s["color"],
                     "detail": s["detail"], "score": q, "ahash": s["ahash"], "sha1": "", "library_id": rec["id"],
                     "source": link, "credit": rec.get("credit"),
                     "name": f"{(rec.get('title') or rec['id'])[:40]} @ {int(orig_t // 60)}:{int(orig_t % 60):02d}",
                     "tags": list(tags or []), "added": time.time()}
            pool.append(entry)
            hashes.append((ah, str(path), s["t_in"]))
            have.add(cid)
            added.append(cid)
        mv_assets.save_pool(store, pool)
    store.log(f"视频片段素材：{rec.get('title') or rec['id']} 新增 {len(added)} 个镜头"
              + (f"（去掉 {dups} 个与已有镜头几乎相同的）" if dups else "")
              + (f"；{len(unreadable)} 个文件无法分析" if unreadable else ""))
    return {"source": rec["id"], "title": rec.get("title"), "added": len(added), "near_duplicates": dups,
            "already": already, "candidates": len(found), "unreadable": unreadable, "relinked": moved}


def clips(store: JobStore) -> list[dict]:
    return [a for a in mv_assets.load_pool(store) if a.get("kind") == "clip"]


def contact_sheets(store: JobStore, *, ids: list[str] | None = None, cols: int = 6, per_sheet: int = 42) -> list[str]:
    """Numbered thumbnails of the clip pool for review (the agent looks at them
    and excludes shots with menus, captions, logos, facecams …)."""
    from .render import qt_app

    qt_app()
    from PyQt6.QtCore import QRectF, Qt
    from PyQt6.QtGui import QColor, QFont, QImage, QPainter

    items = [a for a in clips(store) if ids is None or a["id"] in ids]
    pool_d = mv_assets.pool_dir(store)
    for old in store.path("previews").glob("clip_sheet_*.*"):  # numbering restarts with every run
        old.unlink(missing_ok=True)
    out = []
    tw, th, lab = 320, 180, 22
    for si in range(0, len(items), per_sheet):
        chunk = items[si:si + per_sheet]
        rows = math.ceil(len(chunk) / cols)
        img = QImage(cols * tw, rows * (th + lab), QImage.Format.Format_RGB32)
        img.fill(QColor("#111111"))
        p = QPainter(img)
        f = QFont("Microsoft YaHei UI")
        f.setPixelSize(14)
        p.setFont(f)
        for k, a in enumerate(chunk):
            x, y = (k % cols) * tw, (k // cols) * (th + lab)
            t = QImage(str(pool_d / "thumbs" / f"{a['id']}.jpg"))
            if not t.isNull():
                p.drawImage(QRectF(x, y, tw, th), t)
            p.setPen(QColor("#FFE066"))
            p.drawText(QRectF(x + 4, y + th, 54, lab), Qt.AlignmentFlag.AlignVCenter, f"#{si + k + 1}")
            p.setPen(QColor("#FFFFFF"))
            p.drawText(QRectF(x + 58, y + th, tw - 62, lab), Qt.AlignmentFlag.AlignVCenter,
                       f"{a['id']}  {a['duration']:.1f}s  动{a['motion'] * 100:.1f}  {a.get('score', 0):.2f}")
        p.end()
        path = store.path("previews", f"clip_sheet_{si // per_sheet + 1}.jpg")
        img.save(str(path), quality=85)
        # number → id, so a reviewer can answer with the yellow tile numbers
        write_json(path.with_suffix(".json"), [{"n": si + k + 1, "id": a["id"], "name": a.get("name")}
                                               for k, a in enumerate(chunk)])
        out.append(str(path))
    return out


def used_sheets(store: JobStore, *, per_sheet: int = 12, frames: int = 6) -> list[str]:
    """Second review, after planning: for every clip the cut plan uses, ``frames``
    frames spread over exactly the part that will be on screen (one clip per row).
    Pool sheets show only a clip's middle frame, so a subtitle or a menu that appears
    later in the clip slips through. Rows are numbered ``U1`` … (``--exclude U3``).
    A clip whose on-screen part lies inside the span already checked (``mark_checked``
    after a review round) is skipped, so a re-plan only shows what is new — including
    a reviewed clip that a re-plan now plays from another in-point."""
    from .render import qt_app

    qt_app()
    from PyQt6.QtCore import QRectF, Qt
    from PyQt6.QtGui import QColor, QFont, QImage, QPainter

    plan = read_json(store.path("render", "montage_plan.json")) or {}
    pool = {a["id"]: a for a in clips(store)}
    spans: dict[str, list[float]] = {}
    for s in plan.get("shots", []):
        a = pool.get(s.get("asset"))
        if a is None:
            continue
        seen = (s["t1"] - s["t0"] + 0.4) * float(s.get("speed") or 1.0)
        lo, hi = float(s.get("in") or 0.0), min(a["duration"], float(s.get("in") or 0.0) + seen)
        cur = spans.setdefault(a["id"], [lo, hi])
        cur[0], cur[1] = min(cur[0], lo), max(cur[1], hi)
    for cid in list(spans):
        done = pool[cid].get("checked_span")
        if done and done[0] - 0.15 <= spans[cid][0] and spans[cid][1] <= done[1] + 0.15:
            del spans[cid]
    for old in store.path("previews").glob("clip_used_*.*"):
        old.unlink(missing_ok=True)
    tmp = store.path("previews", "_used_frames")
    tmp.mkdir(exist_ok=True)
    order = list(spans)
    tw, th, lab = 320, 180, 22
    out = []
    for si in range(0, len(order), per_sheet):
        chunk = order[si:si + per_sheet]
        rows = len(chunk)
        img = QImage(frames * tw, rows * (th + lab), QImage.Format.Format_RGB32)
        img.fill(QColor("#111111"))
        p = QPainter(img)
        f = QFont("Microsoft YaHei UI")
        f.setPixelSize(14)
        p.setFont(f)
        for k, cid in enumerate(chunk):
            a, (lo, hi) = pool[cid], spans[cid]
            x0, y = 0, k * (th + lab)
            for j, frac in enumerate((i + 0.5) / frames for i in range(frames)):
                frame = tmp / f"{cid}_{j}.jpg"
                _thumb(Path(a["src"]), a["t_in"] + lo + (hi - lo) * frac, frame, a.get("crop"))
                t = QImage(str(frame))
                if not t.isNull():
                    p.drawImage(QRectF(x0 + j * tw, y, tw, th), t)
            p.setPen(QColor("#FFE066"))
            p.drawText(QRectF(x0 + 4, y + th, 60, lab), Qt.AlignmentFlag.AlignVCenter, f"U{si + k + 1}")
            p.setPen(QColor("#FFFFFF"))
            p.drawText(QRectF(x0 + 64, y + th, frames * tw - 70, lab), Qt.AlignmentFlag.AlignVCenter,
                       f"{cid}  用到 {lo:.1f}–{hi:.1f}s / {a['duration']:.1f}s  {(a.get('name') or '')[:28]}")
        p.end()
        path = store.path("previews", f"clip_used_{si // per_sheet + 1}.jpg")
        img.save(str(path), quality=85)
        write_json(path.with_suffix(".json"), [{"n": f"U{si + k + 1}", "id": cid, "span": [round(x, 2) for x in spans[cid]]}
                                               for k, cid in enumerate(chunk)])
        out.append(str(path))
    shutil.rmtree(tmp, ignore_errors=True)
    return out


def mark_checked(store: JobStore) -> int:
    """Tag the clips shown on the last used-sheets (``clip_used_N.json``) as ``checked``
    — excluded ones are already gone from the pool."""
    used = {e["id"]: e.get("span") for f in store.path("previews").glob("clip_used_*.json") for e in read_json(f, [])}
    with mv_assets._pool_lock(store):
        pool = mv_assets.load_pool(store)
        n = 0
        for a in pool:
            if a["id"] in used and a.get("kind") == "clip":
                span, old = used[a["id"]] or [0.0, a["duration"]], a.get("checked_span")
                a["checked_span"] = [min(span[0], old[0]), max(span[1], old[1])] if old else span
                if "checked" not in (a.get("tags") or []):
                    a["tags"] = list(a.get("tags") or []) + ["checked"]
                n += 1
        mv_assets.save_pool(store, pool)
    return n


def set_tags(store: JobStore, specs: list[str]) -> int:
    """``"c123abc=battle,mecha"`` (comma tags) for several clips."""
    want = {}
    for spec in specs:
        cid, _, tags = spec.partition("=")
        for one in cid.split("+"):
            want[one.strip()] = [t.strip() for t in tags.split(",") if t.strip()]
    with mv_assets._pool_lock(store):
        pool = mv_assets.load_pool(store)
        n = 0
        for a in pool:
            if a["id"] in want:
                a["tags"] = want[a["id"]]
                n += 1
        mv_assets.save_pool(store, pool)
    return n


def used_in(project_dir: str | Path) -> list[str]:
    """Clip ids another project's montage plan uses (for ``avoid``)."""
    plan = read_json(Path(project_dir) / "render" / "montage_plan.json") or {}
    return sorted({s["asset"] for s in plan.get("shots", []) if str(s.get("asset") or "").startswith("c")})


def credits_by_source(store: JobStore) -> list[str]:
    seen, lines = set(), []
    for a in clips(store):
        key = a.get("library_id")
        if key in seen:
            continue
        seen.add(key)
        url = a.get("source") or ""
        url = re.sub(r"[?&]t=\d+s$", "", url)
        lines.append(f"{a.get('credit') or '视频素材'} — {url}".strip(" —"))
    return lines


# ------------------------------------------------------------------ decoding while rendering
class ClipReader:
    """Frames of one clip at the render frame rate and size (cover-fit, optional
    crop of black bars / logos, optional slow motion), read sequentially from an
    ffmpeg pipe. Reading past the end holds the last frame."""

    def __init__(self, src: str, start: float, w: int, h: int, fps: float, *, crop: list | None = None,
                 speed: float = 1.0, first_index: int = 0, end: float | None = None):
        vf = []
        if crop:
            x, y, cw, ch = crop
            vf.append(f"crop=iw*{cw}:ih*{ch}:iw*{x}:ih*{y}")
        if abs(speed - 1.0) > 1e-3:
            vf.append(f"setpts=PTS/{speed:.4f}")
        vf += [f"fps={fps:g}", f"scale={w}:{h}:force_original_aspect_ratio=increase:flags=bicubic", f"crop={w}:{h}",
               "format=bgra"]
        # ``end`` = the clip's out-point in the file: never play into the next (unreviewed) scene;
        # past it the last frame is held
        limit = ["-t", f"{max(0.05, end - max(0.0, start)):.3f}"] if end is not None else []
        cmd = [ffmpeg_exe(), "-v", "error", "-threads", "2", "-ss", f"{max(0.0, start):.3f}", *limit, "-i", str(src),
               "-an", "-sn", "-vf", ",".join(vf), "-f", "rawvideo", "-pix_fmt", "bgra", "-"]
        self.w, self.h = w, h
        self.size = w * h * 4
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=self.size,
                                     **hidden_subprocess_kwargs())
        self.pos = first_index - 1
        self.buf: bytes | None = None
        self.img = None
        self.eof = False

    def frame(self, index: int):
        from PyQt6.QtGui import QImage

        changed = False
        while self.pos < index and not self.eof:
            data = self.proc.stdout.read(self.size)
            if not data or len(data) < self.size:
                self.eof = True
                break
            self.buf = data
            self.pos += 1
            changed = True
        if self.buf is None:
            return None
        if changed or self.img is None:
            self.img = QImage(self.buf, self.w, self.h, self.w * 4, QImage.Format.Format_RGB32)
        return self.img

    def close(self) -> None:
        try:
            self.proc.kill()
            self.proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass


def youtube_search(query: str, n: int = 15) -> list[dict]:
    from . import mvvideo

    return mvvideo.search(query, n)


def source_label(url: str | None) -> str:
    return urllib.parse.urlparse(url).netloc if url else "本地"


# ------------------------------------------------------------------ AI enhance (RIFE + Real-ESRGAN)
# Low-resolution / low-fps library files get an ``<name>.enhanced.mp4`` copy next to the
# original: ffmpeg unpacks the frames, RIFE doubles the frame rate, Real-ESRGAN upscales,
# ffmpeg re-encodes (picture only). The job's clip pool is then re-pointed at the copy and
# the library gets the new file registered. Strictly serial — one file, one step at a time
# (the user forbids parallel heavy GPU load).
ENHANCE_MIN_SIDE = 1080      # short side below this → super-resolution
ENHANCE_MIN_FPS = 30.0       # frame rate below this → a low-quality source
ENHANCE_RIFE_MAX_FPS = 50.0  # at/above this the source is already smooth: skip RIFE
ENHANCE_MAX_SECONDS = 300.0  # never enhance a whole long recording unless --ids picked it
RIFE_DIR_NAME = "rife-ncnn-vulkan-20221029-windows"
ESRGAN_MODEL = "realesr-animevideov3"


def _enhance_tools() -> dict:
    """RIFE / Real-ESRGAN under ``<install home>/tools`` (existence checked by the caller)."""
    tools = km_home() / "tools"
    rife_d = tools / "rife" / RIFE_DIR_NAME
    return {"rife": rife_d / "rife-ncnn-vulkan.exe", "rife_model": rife_d / "rife-v4.6",
            "esrgan": tools / "realesrgan" / "realesrgan-ncnn-vulkan.exe",
            "esrgan_models": tools / "realesrgan" / "models"}


def _enhance_install_hint(missing: list[str]) -> str:
    return ("缺少 AI 增强工具：" + "、".join(missing) + "。请下载并解压到 " + str(km_home() / "tools") + " ：\n"
            "RIFE：https://gh-proxy.com/https://github.com/nihui/rife-ncnn-vulkan/releases/download/20221029/"
            "rife-ncnn-vulkan-20221029-windows.zip\n"
            "　→ 解压为 tools/rife/rife-ncnn-vulkan-20221029-windows/rife-ncnn-vulkan.exe\n"
            "Real-ESRGAN：https://gh-proxy.com/https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/"
            "realesrgan-ncnn-vulkan-20220424-windows.zip\n"
            "　→ 解压为 tools/realesrgan/realesrgan-ncnn-vulkan.exe（模型目录 tools/realesrgan/models/）")


def _run_tool(cmd: list[str], label: str) -> None:
    res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                         **hidden_subprocess_kwargs())
    if res.returncode != 0:
        tail = (res.stderr or res.stdout or "").strip()[-400:]
        raise RuntimeError(f"{label}失败（退出码 {res.returncode}）：{tail}")


def _count_frames(d: Path) -> int:
    return sum(1 for p in d.iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png")) if d.is_dir() else 0


def _frame_pattern(d: Path) -> str | None:
    """``%06d.jpg`` / ``%08d.png`` … inferred from the first numbered frame in ``d``."""
    names = sorted(p.name for p in d.iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png")) if d.is_dir() else []
    for name in names:
        stem = Path(name).stem
        if stem.isdigit():
            return f"%0{len(stem)}d" + Path(name).suffix.lower()
    return None


_QSV_OK: bool | None = None


def _hevc_qsv() -> bool:
    global _QSV_OK
    if _QSV_OK is None:
        try:
            out = subprocess.run([ffmpeg_exe(), "-hide_banner", "-encoders"], capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=30,
                                 **hidden_subprocess_kwargs()).stdout
            _QSV_OK = "hevc_qsv" in out
        except Exception:  # noqa: BLE001
            _QSV_OK = False
    return _QSV_OK


def _enhance_sidecars(src: Path, out: Path, sr_scale: int) -> None:
    """``<file>.enhanced.mp4.scan.json`` (the original scan with the scaled size — motion /
    scores describe the same pictures) and an ``enhanced`` file entry in the library's
    ``sources.json`` (same section as the original file)."""
    scan = src.with_name(src.name + ".scan.json")
    if scan.is_file():
        data = read_json(scan, {}) or {}
        data["file"] = out.name
        if sr_scale != 1:
            data["w"] = int(round((data.get("w") or 0) * sr_scale))
            data["h"] = int(round((data.get("h") or 0) * sr_scale))
        data["src_size"] = out.stat().st_size
        write_json(out.with_name(out.name + ".scan.json"), data)
    index_p = src.parent.parent / "sources.json"
    index = read_json(index_p, {}) or {}
    for rec in index.values():
        entries = rec.get("files") or []
        hit = next((f for f in entries if _file_path(src.parent.parent, rec, f) == src), None)
        if hit is None:
            continue
        new_name = str(out.resolve()) if Path(hit["file"]).is_absolute() else out.name
        if not any(f.get("file") == new_name for f in entries):
            entries.append({**hit, "file": new_name, "enhanced": True})
            write_json(index_p, index)
        return


def _enhance_one(store: JobStore, src: Path, info: dict, tools: dict, *, do_rife: bool, do_sr: bool,
                 sr_scale: int, rife_multi: int) -> dict:
    """The full pipeline for one library file. Frame dirs live in ``%TEMP%/km_enhance`` and
    a step whose output frame count already matches is skipped (resume after an interrupt);
    the temp dir is removed once the enhanced file is finished."""
    t0 = time.time()
    fps = float(info.get("fps") or 0.0)
    w, h = int(info.get("width") or 0), int(info.get("height") or 0)
    dur = float(info.get("duration") or 0.0)
    out = src.with_name(src.stem + ".enhanced.mp4")
    res = {"src": str(src), "out": str(out), "duration": round(dur, 1), "fps": fps, "w": w, "h": h,
           "sr_scale": sr_scale if do_sr else 1, "steps": [], "seconds": 0.0}
    if out.is_file():  # finished earlier (interrupted before the pool update ran)
        res["note"] = "增强副本已存在"
        return res
    safe = re.sub(r"[^0-9A-Za-z_-]+", "_", f"{src.parent.name}_{src.stem}")[:60]
    root = Path(tempfile.gettempdir()) / "km_enhance"
    prev = sorted(root.glob(f"*_{safe}"), key=lambda p: p.stat().st_mtime) if root.is_dir() else []
    tmp = prev[-1] if prev else root / f"{os.getpid()}_{safe}"
    frames_d = tmp / "src"
    # 1 unpack (jpg frames at the native frame rate)
    expected = max(1, int(round(dur * fps))) if fps else 0
    if not (expected and _count_frames(frames_d) >= expected - 2):
        shutil.rmtree(frames_d, ignore_errors=True)
        frames_d.mkdir(parents=True, exist_ok=True)
        _run_tool([ffmpeg_exe(), "-v", "error", "-i", str(src), "-an", "-q:v", "2",
                   str(frames_d / "%06d.jpg")], "拆帧")
    n_src = _count_frames(frames_d)
    if expected and n_src < expected * 0.9:
        raise RuntimeError(f"拆帧数量不足（{n_src}/{expected}）")
    cur = frames_d
    try:
        # 2 RIFE: one pass per doubling (2x → two passes for 4x)
        if do_rife:
            for k in range(int(round(math.log2(rife_multi)))):
                out_d = tmp / f"rife{k + 1}"
                want = 2 * _count_frames(cur) - 1
                if _count_frames(out_d) < want - 1:
                    shutil.rmtree(out_d, ignore_errors=True)
                    out_d.mkdir(parents=True, exist_ok=True)
                    _run_tool([str(tools["rife"]), "-i", str(cur), "-o", str(out_d),
                               "-m", str(tools["rife_model"])], f"RIFE 补帧（第 {k + 1} 遍）")
                cur = out_d
            res["steps"].append(f"RIFE 补帧 {fps:g}→{fps * rife_multi:g}fps")
        # 3 Real-ESRGAN
        if do_sr:
            out_d = tmp / "sr"
            want = _count_frames(cur)
            if _count_frames(out_d) < want:
                shutil.rmtree(out_d, ignore_errors=True)
                out_d.mkdir(parents=True, exist_ok=True)
                _run_tool([str(tools["esrgan"]), "-i", str(cur), "-o", str(out_d), "-s", str(sr_scale),
                           "-n", ESRGAN_MODEL, "-m", str(tools["esrgan_models"])], "Real-ESRGAN 超分")
            cur = out_d
            res["steps"].append(f"超分 x{sr_scale}（{w}×{h}→{w * sr_scale}×{h * sr_scale}）")
        # 4 re-encode (picture only; hevc_qsv when available, libx264 crf 18 otherwise)
        pattern = _frame_pattern(cur)
        if pattern is None:
            raise RuntimeError("找不到合成用的帧序列")
        fps_out = fps * (rife_multi if do_rife else 1)
        tmp_out = out.with_name(out.stem + ".tmp.mp4")
        tmp_out.unlink(missing_ok=True)
        base = [ffmpeg_exe(), "-y", "-v", "error", "-framerate", f"{fps_out:g}", "-i", str(cur / pattern),
                "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-an", "-movflags", "+faststart"]
        try:
            if _hevc_qsv():
                try:
                    _run_tool(base + ["-c:v", "hevc_qsv", "-global_quality", "18", "-pix_fmt", "nv12",
                                      str(tmp_out)], "合成（hevc_qsv）")
                except RuntimeError:
                    store.log("hevc_qsv 合成失败，改用 libx264")
                    _run_tool(base + ["-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p", str(tmp_out)], "合成")
            else:
                _run_tool(base + ["-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p", str(tmp_out)], "合成")
        except Exception:
            tmp_out.unlink(missing_ok=True)
            raise
        os.replace(tmp_out, out)
        _enhance_sidecars(src, out, sr_scale if do_sr else 1)
    finally:
        if out.is_file():  # keep the frames on failure: the next run resumes at the failed step
            shutil.rmtree(tmp, ignore_errors=True)
    res["seconds"] = round(time.time() - t0, 1)
    return res


def enhance(store: JobStore, *, ids: list[str] | None = None, only_used: bool = False, no_sr: bool = False,
            no_rife: bool = False, sr_scale: int = 2, rife_multi: int = 2) -> dict:
    """Enhance the low-quality source files behind the job's clip pool and re-point the
    pool at the enhanced copies. Sources are processed one at a time; a failed source keeps
    its original file and never stops the rest."""
    from . import media

    if no_sr and no_rife:
        raise SystemExit("--no-sr 与 --no-rife 不能同时使用（那样就没有可做的增强了）")
    if sr_scale not in (2, 3, 4):
        raise SystemExit("--sr-scale 只支持 2 / 3 / 4（realesr-animevideov3 模型）")
    if rife_multi < 2 or rife_multi & (rife_multi - 1):
        raise SystemExit("--rife-multi 只支持 2 的幂（2 / 4 / 8）")
    tools = _enhance_tools()
    missing = []
    if not no_rife and not (tools["rife"].is_file() and tools["rife_model"].is_dir()):
        missing.append("RIFE")
    if not no_sr and not (tools["esrgan"].is_file() and tools["esrgan_models"].is_dir()):
        missing.append("Real-ESRGAN")
    if missing:
        raise SystemExit(_enhance_install_hint(missing))

    selected = clips(store)
    if ids:
        unknown = [x for x in ids if x not in {a["id"] for a in selected}]
        if unknown:
            print("警告：素材池里没有这些镜头 id：" + ",".join(unknown))
        selected = [a for a in selected if a["id"] in set(ids)]
    if only_used:
        plan = read_json(store.path("render", "montage_plan.json")) or {}
        used = {s.get("asset") for s in plan.get("shots", [])}
        if not used:
            raise SystemExit("还没有剪辑计划（render/montage_plan.json），--only-used 无法确定范围")
        selected = [a for a in selected if a["id"] in used]
    srcs, seen = [], set()
    for a in selected:
        key = os.path.normcase(os.path.normpath(a.get("src") or ""))
        if key and key not in seen:
            seen.add(key)
            srcs.append(Path(a["src"]))
    if not srcs:
        raise SystemExit("没有需要增强的源文件（素材池为空，或筛选条件没有命中）")

    results, skipped, failed = [], [], []
    for i, src in enumerate(srcs):
        label = f"[{i + 1}/{len(srcs)}] {src.name}"
        if src.name.lower().endswith(".enhanced.mp4"):
            skipped.append({"src": str(src), "note": "已是增强副本"})
            continue
        if not src.is_file():
            failed.append({"src": str(src), "error": "源文件不存在"})
            print(f"警告：{label} 源文件不存在，跳过")
            continue
        try:
            info = media.probe(src)
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop the rest
            failed.append({"src": str(src), "error": str(exc)})
            print(f"警告：{label} 无法读取：{exc}")
            continue
        fps = float(info.get("fps") or 0.0)
        w, h = int(info.get("width") or 0), int(info.get("height") or 0)
        dur = float(info.get("duration") or 0.0)
        short = min(w, h)
        if not (short < ENHANCE_MIN_SIDE or fps < ENHANCE_MIN_FPS):
            skipped.append({"src": str(src), "note": f"画质已达标（{w}×{h}@{fps:g}）"})
            continue
        if dur > ENHANCE_MAX_SECONDS and not ids:
            skipped.append({"src": str(src), "note": f"时长 {dur / 60:.1f} 分钟，超过 5 分钟保护线"})
            print(f"{label} 时长 {dur / 60:.1f} 分钟，超过 5 分钟，默认跳过（确有需要请用 --ids 指定）")
            continue
        do_rife = not no_rife and 0 < fps < ENHANCE_RIFE_MAX_FPS
        do_sr = not no_sr and 0 < short < ENHANCE_MIN_SIDE
        if not do_rife and not do_sr:
            skipped.append({"src": str(src), "note": "在当前的开关下无需处理"})
            continue
        store.set_status(f"正在增强素材（{i + 1}/{len(srcs)}）：{src.name}", "working")
        try:
            res = _enhance_one(store, src, info, tools, do_rife=do_rife, do_sr=do_sr,
                               sr_scale=sr_scale, rife_multi=rife_multi)
        except Exception as exc:  # noqa: BLE001 - the source stays as it is, continue with the next
            for junk in (src.with_name(src.stem + ".enhanced.mp4"), src.with_name(src.stem + ".enhanced.tmp.mp4")):
                junk.unlink(missing_ok=True)
            failed.append({"src": str(src), "error": str(exc)})
            print(f"警告：{label} 增强失败（{exc}），该源保持原样")
            continue
        results.append(res)
        if res["steps"]:
            store.log(f"素材增强：{src.name} → {Path(res['out']).name}"
                      f"（{' + '.join(res['steps'])}，用时 {res['seconds']:.0f} 秒）")
            print(f"{label} {dur:.0f} 秒 {w}×{h}@{fps:g} → {' + '.join(res['steps'])}，"
                  f"用时 {res['seconds']:.0f} 秒 → {res['out']}")
        else:
            print(f"{label} 增强副本已存在，直接沿用 → {res['out']}")

    # re-point every pool clip of a finished source at the enhanced copy (one locked write)
    done = [r for r in results if r.get("out")]
    pool_updated = 0
    if done:
        by_src = {os.path.normcase(os.path.normpath(r["src"])): r for r in done}
        touched = []
        with mv_assets._pool_lock(store):
            pool = mv_assets.load_pool(store)
            for a in pool:
                if a.get("kind") != "clip":
                    continue
                r = by_src.get(os.path.normcase(os.path.normpath(a.get("src") or "")))
                if r is None:
                    continue
                a["src"] = r["out"]
                if r.get("sr_scale", 1) != 1:
                    a["w"] = int(round((a.get("w") or 0) * r["sr_scale"]))
                    a["h"] = int(round((a.get("h") or 0) * r["sr_scale"]))
                touched.append(a["id"])
            mv_assets.save_pool(store, pool)
        pool_d = mv_assets.pool_dir(store)
        for cid in touched:  # re-rendered from the new source the next time they are needed
            (pool_d / "thumbs" / f"{cid}.jpg").unlink(missing_ok=True)
        pool_updated = len(touched)

    n_new = sum(1 for r in results if r["steps"])
    print(f"已增强 {n_new} 个源文件"
          + (f"，{len(results) - n_new} 个沿用已有增强副本" if len(results) > n_new else "")
          + (f"，跳过 {len(skipped)} 个" if skipped else "")
          + (f"，失败 {len(failed)} 个" if failed else ""))
    return {"enhanced": n_new, "results": results, "skipped": skipped, "failed": failed,
            "pool_updated": pool_updated}
