"""Hi-Res 混流 (Lin-K Lyrics step 6, optional) for the rendered karaoke video.

Muxes the karaoke MP4 with the 原唱 and 伴奏 tracks (see :mod:`tracks`) into
MKVs through ``krok_helper.pipeline.run_pipeline``: every track becomes FLAC
32-bit, >= 48 kHz, stereo and the video stream is copied, exactly like the
workbench.
"""

from __future__ import annotations

import time
from pathlib import Path

from .jobstore import JobStore
from .paths import ensure_repo_on_path
from .tracks import master_mp4, resolve_tracks


def cmd_hires(store: JobStore, *, on: str | None = None, offs: list[str] | None = None, video: str | None = None,
              align: bool = True) -> list[Path]:
    from . import render

    ensure_repo_on_path()
    from krok_helper.pipeline import OUTPUT_NAME_MODE_TEMPLATE, run_pipeline

    st = store.load()
    opts = (st.get("options") or {}).get("hires") or {}
    on = on if on is not None else (opts.get("on") or None)
    offs = offs if offs is not None else [p for p in (opts.get("off") or []) if p]
    mp4 = Path(video) if video else master_mp4(store, st)
    if mp4 is None or not mp4.is_file():
        raise SystemExit("请先导出成品 MP4（km.py export <job> --kinds mp4），Hi-Res 混流以它为视频轨")

    def say(msg: str) -> None:
        render._record_export(store, "hires", state="running", progress=0.1, label="Hi-Res 混流（MKV）",
                              path=store.rel(mp4), detail=msg)
        store.set_status("Hi-Res：" + msg, "working")

    tracks = resolve_tracks(store, mp4, on=on, offs=offs, align=align,
                            separate_from_on=opts.get("separate_from_on", True), progress=say)
    if tracks.on is None and not tracks.offs:
        raise SystemExit("没有可用的原唱或伴奏音轨")
    say("FLAC 32bit 无损混流")
    t0 = time.time()
    from .versions import base_name

    base = base_name(mp4).replace("{", "(").replace("}", ")")
    outputs = run_pipeline(
        mp4, tracks.on, None, output_dir=mp4.parent, output_name_mode=OUTPUT_NAME_MODE_TEMPLATE,
        on_name_template=f"{base} (Hi-Res on vocal)", off_name_template=f"{base} (Hi-Res off vocal)",
        logger=lambda m: store.log("混流：" + str(m)), off_vocal_paths=tracks.offs or None,
    )
    files = [store.rel(p) for p in outputs]
    render._record_export(store, "hires", state="done", progress=1.0, label="Hi-Res 混流（MKV）",
                          path=files[0], files=files, notes=tracks.notes, seconds=round(time.time() - t0))
    for n in tracks.notes:
        store.log(n)
    store.set_status(f"Hi-Res 混流完成：{len(outputs)} 个 MKV", "done")
    return outputs
