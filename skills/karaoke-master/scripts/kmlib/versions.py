"""on vocal / off vocal versions of a rendered video.

No re-rendering: the video stream is copied and only the audio is swapped
(AAC 320 kbps, 48 kHz), so both versions show exactly the same frames.

* on vocal  — ``<name> (on vocal).mp4``: the best 原唱 (registered Hi-Res
  source, the lossless original, or the user's file) at AAC 320 kbps; it replaces
  the master ``<name>.mp4`` so a finished project has exactly the two versions;
* off vocal — ``<name> (off vocal).mp4``: the 伴奏 from
  :func:`tracks.resolve_tracks` (separated instrumental, or the user's
  off-vocal file aligned to the video).
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

from .jobstore import JobStore
from .media import probe, run
from .paths import ffmpeg_exe
from .tracks import master_mp4, resolve_tracks


def remux_audio(video: Path, audio: Path, out: Path, *, duration: float | None = None) -> Path:
    duration = duration or probe(video)["duration"]
    cmd = [ffmpeg_exe(), "-y", "-v", "error", "-i", str(video), "-i", str(audio),
           "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
           "-af", "apad", "-c:a", "aac", "-b:a", "320k", "-ar", "48000", "-ac", "2",
           "-t", f"{duration:.3f}", "-movflags", "+faststart", str(out)]
    res = run(cmd)
    if res.returncode != 0:
        raise RuntimeError(f"混流失败：{res.stderr.strip()[-400:]}")
    return out


def base_name(path: Path) -> str:
    """``<name> (on vocal)`` → ``<name>`` (a renamed master is used again)."""
    return re.sub(r" \((on vocal|off vocal(?: \d+)?)\)$", "", path.stem)


def _versioned(path: Path, tag: str) -> Path:
    return path.with_name(f"{base_name(path)} ({tag}){path.suffix}")


def cmd_versions(store: JobStore, *, video: str | None = None, on: str | None = None, offs: list[str] | None = None,
                 align: bool = True, kind: str = "onoff") -> list[Path]:
    """Write on/off vocal versions of ``video`` (default: the karaoke master MP4)."""
    from . import render

    st = store.load()
    opts = (st.get("options") or {}).get("hires") or {}  # same source fields as Hi-Res
    on = on if on is not None else (opts.get("on") or None)
    offs = offs if offs is not None else [p for p in (opts.get("off") or []) if p]
    master = Path(video) if video else master_mp4(store, st)
    if master is None or not master.is_file():
        raise SystemExit("请先导出成品 MP4（km.py export <job> --kinds mp4）")
    label = "on / off vocal 双版本" if kind == "onoff" else "MV on / off vocal"

    def say(msg: str) -> None:
        render._record_export(store, kind, state="running", progress=0.2, label=label, path=store.rel(master),
                              detail=msg)
        store.set_status(f"{label}：{msg}", "working")

    t0 = time.time()
    tracks = resolve_tracks(store, master, on=on, offs=offs, align=align,
                            separate_from_on=opts.get("separate_from_on", True), progress=say)
    from .tracks import cast_delay_ms, delayed_audio

    delay = cast_delay_ms(st)
    if delay:  # 投屏延迟: same picture, the sound `delay` ms later
        tracks.on = delayed_audio(store, tracks.on, delay) if tracks.on is not None else None
        tracks.offs = [delayed_audio(store, p, delay) for p in tracks.offs]
        tracks.notes.append(f"投屏延迟 {delay:+d} ms：画面比声音提前 {delay / 1000:.2f} 秒")
    duration = probe(master)["duration"]
    outputs: list[Path] = []
    on_out = _versioned(master, "on vocal")
    for i, off in enumerate(tracks.offs):
        say("写入 off vocal")
        tag = "off vocal" if len(tracks.offs) == 1 else f"off vocal {i + 1}"
        outputs.append(remux_audio(master, off, _versioned(master, tag), duration=duration))
    if tracks.on is not None:
        say("写入 on vocal")
        tmp = on_out.with_name(on_out.stem + ".part.mp4")
        remux_audio(master, tracks.on, tmp, duration=duration)
        os.replace(tmp, on_out)
    elif master != on_out:
        os.replace(master, on_out)  # no better 原唱: the master's own audio is the on vocal
    outputs.insert(0, on_out)
    if not video and master != on_out and master.exists():
        # the master is now redundant (same frames, on-vocal audio): keep exactly the two versions
        master.unlink()
        st_now = store.load()
        for e in st_now.get("exports", []):
            if e.get("kind") == "mp4" and e.get("state") == "done":
                render._record_export(store, "mp4", **{**{k: v for k, v in e.items() if k != "kind"},
                                                       "path": store.rel(on_out), "label": "成品 MP4（on vocal）"})
    elif video and master != on_out and master.exists() and kind == "mv":
        master.unlink()  # the plain MV copy made by export
    if len(outputs) < 2:
        tracks.notes.append("没有可用的伴奏音轨：先运行 km.py analyze（人声分离）或提供伴奏文件")
    files = [store.rel(p) for p in outputs]
    render._record_export(store, kind, state="done", progress=1.0, label=label, path=files[0], files=files,
                          notes=tracks.notes, seconds=round(time.time() - t0))
    store.set_status(f"{label}已生成：{len(outputs)} 个文件", "done")
    return outputs
