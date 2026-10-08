"""Engine-exact data for the review page's karaoke overlay.

The page used to draw an approximation of the subtitle layout in JS, which
drifted from the renderer (row assignment per page, smart horizontal margins,
ruby placement). Instead, this module asks the real renderer
(``krok_helper.subtitle_render.engine.painter``) for every display line:

* its on-screen window and row (``display_schedule_for_style``);
* its exact rendering in the *before singing* and *after singing* states, as
  cropped transparent sprites — produced by re-invoking the painter's own
  ``_paint_line`` with the arguments it resolved for that line in a real frame
  (page offsets, baseline, x, lane, layout plan);
* the wipe geometry: the line layout's ``fill_segments`` and each ruby's
  ``wipe_segments`` mapped into sprite coordinates.

The browser only composites the two sprites and clips the "after" sprite to the
engine's wipe position at the current time, so positions, fonts, ruby and
colours match the export; only entry/exit animations are simplified to fades.
Written to ``render/web_layout.json`` + ``render/web_sprites/``.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

import numpy as np

from .jobstore import JobStore, read_json, write_json


def _alpha_bbox(img) -> tuple[int, int, int, int] | None:
    ptr = img.constBits()
    ptr.setsize(img.sizeInBytes())
    arr = np.frombuffer(ptr, np.uint8).reshape(img.height(), img.bytesPerLine() // 4, 4)[:, : img.width(), 3]
    ys, xs = np.nonzero(arr > 3)
    if len(xs) == 0:
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def _compress(samples: list[tuple[int, float | None]], tol: float = 0.6) -> list[list[float]]:
    """``[(t_ms, x|None)]`` → keyframes ``[[t_s, x], ...]`` (``x = None`` = nothing
    sung yet), dropping points that linear interpolation reproduces within ``tol`` px."""
    pts = [(t, x) for t, x in samples if x is not None]
    if not pts:
        return []
    keep = [pts[0]]
    for k in range(1, len(pts) - 1):
        (ta, xa), (tb, xb) = keep[-1], pts[k + 1]
        tm, xm = pts[k]
        pred = xa + (xb - xa) * (tm - ta) / max(1e-6, tb - ta)
        if abs(pred - xm) > tol:
            keep.append(pts[k])
    if len(pts) > 1:
        keep.append(pts[-1])
    first_t = pts[0][0]
    out = [[round((first_t - 20) / 1000.0, 3), None]] if samples and samples[0][1] is None else []
    return out + [[round(t / 1000.0, 3), round(x, 1)] for t, x in keep]


def build(store: JobStore) -> dict:
    from . import render

    render.qt_app()
    from PyQt6.QtCore import QPointF, QRect
    from PyQt6.QtGui import QImage, QPainter

    import krok_helper.subtitle_render.engine.painter as P

    st = store.load()
    sug = store.dir / "timing" / "project.sug"
    if not sug.is_file():
        return {}
    W, H, _fps = render._output_size(st)
    style = render.build_style(st, H, first_line_start=render._first_line_start(store))
    track, extras = render.load_track(sug, style, ruby=(st.get("options") or {}).get("ruby", True))
    duration = float(st["media"]["source"]["duration"])
    schedule = P.display_schedule_for_style(track, style, logical_w=W, logical_h=H)

    sprite_dir = store.dir / "render" / "web_sprites"
    if sprite_dir.exists():
        shutil.rmtree(sprite_dir, ignore_errors=True)
    sprite_dir.mkdir(parents=True, exist_ok=True)
    rev = int(time.time() * 1000)

    captured: dict = {}
    original = P._paint_line

    def spy(painter, img_w, img_h, trk, line, t_ms, sty, **kw):
        if trk is track:
            captured[id(line)] = (painter.transform(), img_w, img_h, sty, kw)
        return original(painter, img_w, img_h, trk, line, t_ms, sty, **kw)

    def blank():
        im = QImage(W, H, QImage.Format.Format_ARGB32_Premultiplied)
        im.fill(0)
        return im

    lines_out = []
    P._paint_line = spy
    try:
        for idx, (lane, start_ms, end_ms) in sorted(schedule.items()):
            line = track.lines[idx]
            starts = [c.start_ms for c in line.chars if c.start_ms is not None]
            if not starts:
                continue
            first = min(starts)
            last_end = int(line.end_ms or max(starts))
            t_before = max(start_ms + 1, min(first - 5, end_ms - 1))
            exit_ms = int(getattr(style, "exit_fade_ms", 300) or 300)
            t_after = max(t_before, min(last_end + 20, end_ms - exit_ms - 5))
            captured.clear()
            P.paint_frame(blank(), track, t_before, style, extras or None, duration_ms=int(duration * 1000))
            cap = captured.get(id(line))
            if cap is None:
                continue
            transform, iw, ih, sty, kw = cap

            def render_state(t: int):
                im = blank()
                p = QPainter(im)
                p.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing
                                 | QPainter.RenderHint.SmoothPixmapTransform)
                p.setTransform(transform)
                original(p, iw, ih, track, line, t, sty, **kw)
                p.end()
                return im

            before, after = render_state(t_before), render_state(t_after)
            boxes = [b for b in (_alpha_bbox(before), _alpha_bbox(after)) if b]
            if not boxes:
                continue
            x0 = max(0, min(b[0] for b in boxes) - 2)
            y0 = max(0, min(b[1] for b in boxes) - 2)
            x1 = min(W, max(b[2] for b in boxes) + 2)
            y1 = min(H, max(b[3] for b in boxes) + 2)
            rect = QRect(x0, y0, x1 - x0, y1 - y0)
            name_b, name_a = f"l{idx:03d}_b.png", f"l{idx:03d}_a.png"
            before.copy(rect).save(str(sprite_dir / name_b))
            after.copy(rect).save(str(sprite_dir / name_a))

            line_style = kw.get("resolved_style") or P._style_for_line_display_window(
                sty, line, kw.get("display_start_ms"), kw.get("display_end_ms"))
            layout = P._layout_line(track, line, line_style, iw, ih, baseline_y=kw.get("baseline_y"),
                                    line_x=kw.get("line_x"), lane=kw.get("lane"),
                                    cache_sig=kw.get("layout_cache_sig"), line_plan=kw.get("line_plan"))

            def mx(x: float, y: float = 0.0) -> float:
                return transform.map(QPointF(float(x), float(y))).x() - x0

            def my(y: float) -> float:
                return transform.map(QPointF(0.0, float(y))).y() - y0

            wipe, rubies, split, left = [], [], None, None
            if layout is not None and layout.fill_segments:
                # sample the engine's own clip band (ruby-unit progress, N3 hand-off …)
                t0 = min(first, start_ms) - 40
                t1 = max(last_end, max(s.end_ms for s in layout.fill_segments)) + 60
                samples = []
                for t in range(int(t0), int(t1) + 20, 20):
                    band = P._fill_clip_band(layout.fill_segments, t, layout.rtl)
                    if band is None:
                        samples.append((t, None))
                    else:
                        samples.append((t, mx(band[1] if not layout.rtl else band[0])))
                        if left is None:
                            left = round(mx(band[0] if not layout.rtl else band[1]), 1)
                wipe = _compress(samples)
                for rl in getattr(layout, "ruby_layouts", ()) or ():
                    segs = sorted(rl.wipe_segments, key=lambda w: w.start_ms)
                    if not segs:
                        continue
                    r_left = mx(min(w.axis_start for w in segs))
                    r_samples = []
                    for t in range(int(segs[0].start_ms) - 20, int(segs[-1].end_ms) + 40, 20):
                        ext = None
                        for w in segs:
                            if t <= w.start_ms:
                                break
                            ratio = 1.0 if w.end_ms <= w.start_ms else min(1.0, (t - w.start_ms) / (w.end_ms - w.start_ms))
                            ext = mx(w.axis_start + (w.axis_end - w.axis_start) * ratio)
                        r_samples.append((t, ext))
                    rubies.append({"left": round(r_left, 1), "right": round(mx(max(w.axis_end for w in segs)), 1),
                                   "keys": _compress(r_samples)})
                asc = layout.metrics.ascent() if layout.metrics is not None else 0
                sw = int(getattr(line_style, "stroke_width_px", 0) or 0)
                if getattr(line_style, "stroke2_enabled", False):
                    sw += int(getattr(line_style, "stroke2_width_px", 0) or 0)
                split = round(my(layout.baseline_y - asc - sw * 0.6 - 2), 1) if rubies else None
            lines_out.append({
                "i": idx, "lane": lane, "start": start_ms / 1000.0, "end": end_ms / 1000.0,
                "box": [x0, y0, x1 - x0, y1 - y0],
                "before": f"render/web_sprites/{name_b}?v={rev}", "after": f"render/web_sprites/{name_a}?v={rev}",
                "left": left, "wipe": wipe, "rubies": rubies, "split": split,
            })
    finally:
        P._paint_line = original

    data = {"version": 1, "w": W, "h": H, "rev": rev, "lines": lines_out,
            "entry_ms": int(getattr(style, "entry_lead_ms", 300) or 300),
            "exit_ms": int(getattr(style, "exit_fade_ms", 300) or 300)}
    data["title"] = _title_sprite(store, track, style, W, H, duration, rev)
    write_json(store.path("render", "web_layout.json"), data)
    store.update(lambda s: s.setdefault("render", {}).update(web_layout="render/web_layout.json", layout_rev=rev))
    return data


def _title_sprite(store, track, style, W, H, duration, rev):
    """Title card: render the title overlay alone (track without lyric lines)."""
    from copy import copy

    from PyQt6.QtCore import QRect
    from PyQt6.QtGui import QImage

    import krok_helper.subtitle_render.engine.painter as P

    overlays = [t for t in (style.title_overlays or []) if t.enabled and t.custom_windows]
    if not overlays:
        return None
    win = overlays[0].custom_windows[0]
    empty = copy(track)
    empty.lines = []
    img = QImage(W, H, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(0)
    mid = int((win.begin_ms + win.end_ms) / 2)
    try:
        P.paint_frame(img, empty, mid, style, None, duration_ms=int(duration * 1000))
    except Exception:
        return None
    box = _alpha_bbox(img)
    if not box:
        return None
    x0, y0, x1, y1 = box
    img.copy(QRect(x0, y0, x1 - x0, y1 - y0)).save(str(store.dir / "render" / "web_sprites" / "title.png"))
    return {"src": f"render/web_sprites/title.png?v={rev}", "box": [x0, y0, x1 - x0, y1 - y0],
            "start": win.begin_ms / 1000.0, "end": win.end_ms / 1000.0,
            "fade_in": (win.fade_in_ms or 0) / 1000.0, "fade_out": (win.fade_out_ms or 0) / 1000.0}


def refresh(store: JobStore) -> None:
    """Best-effort rebuild after timing / style changes (never breaks the caller)."""
    try:
        build(store)
    except Exception as exc:  # pragma: no cover - diagnostics only
        store.log(f"网页预览布局刷新失败（引擎帧预览不受影响）：{exc}")
