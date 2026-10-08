"""MV background downloaded from YouTube / Bilibili.

Uses Lin-K Lyrics' video-download module (``krok_helper.video_download``,
the yt-dlp service of the desktop app's step 1) to fetch the song's MV, then
puts its picture on the project's own timeline: the MV's audio is
waveform-aligned to the project audio (usually the user's lossless file) and an
aligned, picture-only copy ``media/mv_bg.mp4`` is rendered (start trimmed or
padded, cut to the song length). The karaoke video then shows the MV while every
audio track (on vocal / off vocal / Hi-Res) comes from the lossless source.

Downloading needs the user's go-ahead (see SKILL.md): name the video (title,
channel, duration, size) before calling :func:`download`.
"""

from __future__ import annotations

import contextlib
import importlib
import os
import subprocess
import sys
import time
import types
import urllib.request
from pathlib import Path
from types import SimpleNamespace

from .jobstore import JobStore, write_json
from .paths import ensure_repo_on_path, ffmpeg_exe, hidden_subprocess_kwargs


def _service():
    """(module, YtDlpService) without the desktop app's GUI page or settings file."""
    ensure_repo_on_path()
    import krok_helper

    pkg = "krok_helper.video_download"
    if pkg not in sys.modules:  # the package __init__ imports the Qt download page: bypass it
        mod = types.ModuleType(pkg)
        mod.__path__ = [str(Path(krok_helper.__file__).parent / "video_download")]
        mod.__package__ = pkg
        sys.modules[pkg] = mod
    svc = importlib.import_module(pkg + ".ytdlp_service")
    # the desktop app reads proxy / ffmpeg from its own settings.json; the skill
    # uses the environment (HTTPS_PROXY) and its own ffmpeg instead
    proxy = os.environ.get("KM_PROXY") or os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or ""
    settings = SimpleNamespace(ffmpeg_dir=str(Path(ffmpeg_exe()).parent), updater={})
    svc.load_current_app_settings = lambda: settings
    svc.proxy_url_for_app_settings = lambda _s=None: proxy
    svc.proxy_cli_args_for_app_settings = lambda _s=None: (["--proxy", proxy] if proxy else [])
    svc.subprocess_env_for_app_settings = lambda _s=None: os.environ.copy()
    svc.build_urllib_opener_for_app_settings = lambda _s=None, *h: urllib.request.build_opener(*h)
    return svc, svc.YtDlpService(app_settings=settings)


# ------------------------------------------------------------------ search
def search(query: str, n: int = 8) -> list[dict]:
    """YouTube search (no download). Bilibili / direct URLs go straight to :func:`info`."""
    import yt_dlp

    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "extract_flat": "in_playlist"}
    proxy = os.environ.get("KM_PROXY") or os.environ.get("HTTPS_PROXY") or ""
    if proxy:
        opts["proxy"] = proxy
    with yt_dlp.YoutubeDL(opts) as ydl:
        res = ydl.extract_info(f"ytsearch{int(n)}:{query}", download=False) or {}
    out = []
    for e in res.get("entries") or []:
        vid = e.get("id")
        if not vid:
            continue
        out.append({"id": vid, "url": e.get("url") or f"https://www.youtube.com/watch?v={vid}",
                    "title": e.get("title"), "uploader": e.get("channel") or e.get("uploader"),
                    "duration": e.get("duration"), "views": e.get("view_count"),
                    "thumb": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"})
    return out


def _pick_format(formats, max_height: int):
    """Best format at or below ``max_height``; H.264 preferred at equal height (fast to decode)."""
    usable = [f for f in formats if 0 < (f.height or 0) <= max_height] or list(formats)
    return max(usable, key=lambda f: ((f.height or 0), "avc" in (f.video_codec or "").lower(), f.filesize or 0),
               default=None)


def info(url: str, max_height: int = 1080) -> dict:
    """Title / channel / duration / chosen format and size, for asking the user before downloading."""
    _svc, service = _service()
    vi = service.extract_info(url)
    fmt = _pick_format(vi.formats, max_height)
    return {"url": vi.webpage_url or url, "title": vi.title, "uploader": vi.uploader, "duration": vi.duration,
            "source": vi.source, "format": fmt.format_label if fmt else None,
            "resolution": fmt.resolution if fmt else None,
            "size_mb": round((fmt.filesize or vi.filesize or 0) / 1e6, 1) if fmt else None}


# ------------------------------------------------------------------ download + align
def download(store: JobStore, url: str, *, max_height: int = 1080, progress=None) -> Path:
    svc, service = _service()
    from krok_helper.video_download.download_task import DownloadOptions, DownloadTask

    vi = service.extract_info(url)
    fmt = _pick_format(vi.formats, max_height)
    out_dir = store.dir / "media" / "mv_download"
    out_dir.mkdir(parents=True, exist_ok=True)
    task = DownloadTask(task_id=f"km{int(time.time())}", url=vi.webpage_url or url, title=vi.title,
                        source=vi.source, selected_format=fmt, info=vi)
    options = DownloadOptions(save_dir=str(out_dir), merge_video_audio=True, retry_count=3, timeout=30)

    last = [0.0]

    def cb(event: dict) -> None:  # yt-dlp progress (per stream: video, then audio)
        total = event.get("total_bytes") or event.get("total_bytes_estimate") or 0
        now = time.time()
        if progress and total and now - last[0] > 1.0:
            last[0] = now
            progress(min(1.0, (event.get("downloaded_bytes") or 0) / total))

    with contextlib.redirect_stdout(sys.stderr):  # yt-dlp prints its own progress bar; keep stdout for JSON
        service.download(task, options, cb)
    if not task.local_file or not Path(task.local_file).is_file():
        raise RuntimeError("下载完成但找不到视频文件")
    store.update(lambda s: s["media"].update(mv_video={
        "url": task.url, "title": vi.title, "uploader": vi.uploader, "duration": vi.duration, "source": vi.source,
        "format": fmt.format_label if fmt else None, "file": store.rel(task.local_file)}))
    return Path(task.local_file)


def align(store: JobStore, video: Path, *, progress=None) -> dict:
    """Put the MV picture on the project timeline -> ``media/mv_bg.mp4`` (picture only)."""
    from . import media, tracks
    from .analysis import timing_audio

    st = store.load()
    ref = timing_audio(store, st)  # the lossless original for audio-only projects
    info_ = tracks.estimate_shift(ref, video, log=store.log)  # shift for the MV so it lines up with ref
    shift = float(info_["shift"])
    duration = float(st["media"]["source"]["duration"])
    notes = []
    if info_["confidence"] < 0.2:
        notes.append(f"⚠ MV 的音频与歌曲对不上（置信 {info_['confidence']:.0%}），可能不是同一版本，建议换一个视频")
    elif not info_["consistent"]:
        notes.append(f"⚠ MV 与歌曲无法用单一偏移对齐（MV 可能剪辑过 / 是不同版本），已按整体偏移 {shift:+.2f} 秒处理，部分段落画面会提前或滞后")
    notes.append(f"MV 画面对齐：{shift:+.2f} 秒（置信 {info_['confidence']:.0%}）")
    w, h = (int(x) for x in str((st.get("options") or {}).get("resolution") or "1920x1080").lower().split("x"))
    vinfo = media.probe(video)
    fps = min(60.0, float(vinfo.get("fps") or 30.0))
    out = store.path("media", "mv_bg.mp4")
    cmd = [ffmpeg_exe(), "-y", "-v", "error"]
    if shift < 0:
        cmd += ["-ss", f"{-shift:.3f}"]  # MV has a longer intro: skip it
    cmd += ["-i", str(video), "-an", "-t", f"{duration:.3f}"]
    vf = [f"scale={w}:{h}:force_original_aspect_ratio=decrease", f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black",
          f"fps={fps:g}"]
    if shift > 0:  # the song starts before the MV picture: hold black at the start
        vf.insert(0, f"tpad=start_duration={shift:.3f}:color=black")
    vf.append(f"tpad=stop_mode=clone:stop_duration={max(0.0, duration):.3f}")  # MV shorter than the song
    cmd += ["-vf", ",".join(vf), "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", str(out)]
    if progress:
        progress(0.5)
    res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                         **hidden_subprocess_kwargs())
    if res.returncode != 0:
        raise RuntimeError("生成对齐的 MV 画面失败：" + res.stderr[-400:])
    still = store.path("media", "mv_bg_still.jpg")
    subprocess.run([ffmpeg_exe(), "-y", "-v", "error", "-ss", f"{min(duration * 0.4, 60):.2f}", "-i", str(out),
                    "-frames:v", "1", "-vf", "scale=1280:-2", str(still)], capture_output=True,
                   **hidden_subprocess_kwargs())
    result = {"shift": shift, "confidence": info_["confidence"], "consistent": info_["consistent"], "notes": notes,
              "aligned": store.rel(out), "still": store.rel(still) if still.exists() else None}
    store.update(lambda s: (s["media"].setdefault("mv_video", {}).update(result),
                            s.setdefault("options", {}).update(background={"type": "video", "path": store.rel(out),
                                                                           "origin": "download"})))
    for n in notes:
        store.log(n)
    return result


def use(store: JobStore, url: str, *, max_height: int = 1080) -> dict:
    """Download ``url`` and make it the project's background (aligned to the song)."""
    def prog(p: float) -> None:
        store.update(lambda s: s["media"].update(mv_video_progress=round(min(1.0, p) * 0.8, 3)))
        store.set_status(f"正在下载 MV {min(1.0, p):.0%}", "working")

    store.set_status("正在解析 MV 链接", "working")
    path = download(store, url, max_height=max_height, progress=prog)
    store.set_status("正在把 MV 画面对齐到歌曲", "working")
    res = align(store, path)
    store.update(lambda s: s["media"].update(mv_video_progress=1.0))
    store.set_status("MV 背景已就绪" if res["confidence"] >= 0.2 else "MV 与歌曲对不上，请换一个视频", None)
    return res


def set_candidates(store: JobStore, query: str, items: list[dict]) -> None:
    """Keep the results (with locally cached thumbnails) for the page."""
    import requests

    for it in items:
        target = store.path("previews", "mv_video", f"{it['id']}.jpg")
        if not target.exists():
            try:
                r = requests.get(it["thumb"], timeout=10)
                if r.ok and r.headers.get("content-type", "").startswith("image/"):
                    target.write_bytes(r.content)
            except Exception:  # noqa: BLE001 - a missing thumbnail is fine
                pass
        if target.exists():
            it["thumb_local"] = store.rel(target)
    store.update(lambda s: s.setdefault("previews", {}).update(mv_video_candidates={"query": query, "items": items}))
    write_json(store.path("analysis", "mv_video_search.json"), {"query": query, "items": items})
