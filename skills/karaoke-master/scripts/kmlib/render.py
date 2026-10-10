"""Subtitle rendering through ``krok_helper.subtitle_render`` (CPU QPainter path).

* stage 1 previews: template / effect / singer galleries rendered by the real
  engine on a frame of the user's own material, using the song's first lines;
* stage 3: engine frames at any time, ``.yurika`` project and MP4 export.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import replace
from pathlib import Path

from . import styles
from .jobstore import JobStore, read_json, write_json
from .paths import ensure_repo_on_path, ffmpeg_exe, hidden_subprocess_kwargs

_APP = None


def qt_app():
    """QApplication for off-screen QImage painting.

    On Windows the native ``windows`` platform is used (no window is ever
    shown): the ``offscreen`` plugin there has no system font database and
    renders tofu. Elsewhere / headless, ``offscreen`` + ``QT_QPA_FONTDIR``.
    """
    global _APP
    platform = os.environ.get("KM_QT_PLATFORM")
    if platform:
        os.environ["QT_QPA_PLATFORM"] = platform
    elif os.name != "nt":
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    if os.environ.get("QT_QPA_PLATFORM") == "offscreen" and os.name == "nt":
        os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))
    ensure_repo_on_path()
    from PyQt6.QtWidgets import QApplication

    _APP = QApplication.instance() or QApplication([])
    return _APP


# ----------------------------------------------------------------- style
def build_style(st: dict, height: int, *, include_title: bool = True, first_line_start: float | None = None):
    qt_app()
    from krok_helper.subtitle_render.domain.models import (
        Style,
        TitleOverlay,
        TitleTimeWindow,
        rescale_font_sizes,
        rescale_layout_sizes,
    )

    opts = st.get("options", {})
    tpl = styles.template(opts.get("template"))
    eff = styles.effect(opts.get("effect"))
    style = styles.apply_template(Style(), tpl)
    style = styles.apply_effect(style, eff)
    style = styles.apply_overrides(style, opts.get("overrides"))
    singers = st.get("song", {}).get("singers") or []
    if len(singers) > 1:
        schemes = dict(style.custom_style_schemes)
        for name, c in styles.singer_colors(singers, opts.get("singer_styles")).items():
            schemes[name] = styles.singer_scheme(tpl, c["color"], c["bands"])
        style = replace(style, custom_style_schemes=schemes)
    if include_title and opts.get("title", True) and (st.get("song") or {}).get("title"):
        seg = st.get("segment") or {}
        begin = float(seg.get("start") or 0.0)
        end = begin + 7.0
        if first_line_start is not None and first_line_start - begin > 3.0:
            end = min(begin + 9.0, first_line_start - 0.4)
        title = replace(
            style.title_overlays[0] if style.title_overlays else TitleOverlay(),
            enabled=True, text_template="{title}\n{artist}", show_mode="custom",
            font_family=styles.resolve_font(tpl["fonts"]), font_family_latin=styles.resolve_font(tpl["fonts"]),
            font_size_px=46,
            custom_windows=[TitleTimeWindow(begin_ms=int(begin * 1000), end_ms=int(end * 1000),
                                            fade_in_ms=400, fade_out_ms=600)],
        )
        style = replace(style, title_overlays=[title])
    else:
        style = replace(style, title_overlays=[replace(t, enabled=False) for t in style.title_overlays])
    style = rescale_layout_sizes(style, height)
    style = rescale_font_sizes(style, height)
    return style


def load_track(sug_path: Path, style, *, ruby: bool = True):
    qt_app()
    from krok_helper.subtitle_render.domain.timing import SubtitleLoadingSettings
    from krok_helper.subtitle_render.engine.layout.page.plan import (
        build_page_plan,
        project_page_plan_to_legacy_fields,
    )
    from krok_helper.subtitle_render.sources.sug import load_sug_axis_tracks

    axes = load_sug_axis_tracks(str(sug_path))
    tracks = [a.track for a in axes]
    for t in tracks:
        if not ruby:
            t.rubies = []
        t.page_plan = build_page_plan(t, SubtitleLoadingSettings(), style)
        project_page_plan_to_legacy_fields(t, style)
    return tracks[0], tracks[1:]


# ------------------------------------------------------------ background
def video_frame(path: Path, t: float, w: int, h: int, cache_dir: Path | None = None):
    from PyQt6.QtGui import QImage

    out = None
    if cache_dir:
        out = cache_dir / f"bg_{abs(hash(str(path))) % 10**8}_{int(t * 10)}_{w}.jpg"
        if out.exists():
            return QImage(str(out))
    target = out or Path(os.environ.get("TEMP", ".")) / f"km_bg_{os.getpid()}.jpg"
    cmd = [ffmpeg_exe(), "-y", "-v", "error", "-ss", f"{max(0.0, t):.3f}", "-i", str(path), "-frames:v", "1",
           "-vf", f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black",
           "-q:v", "2", str(target)]
    subprocess.run(cmd, capture_output=True, **hidden_subprocess_kwargs())
    img = QImage(str(target))
    return img if not img.isNull() else None


BACKGROUND_TYPES = {
    "source": "原视频", "video": "视频", "mv": "AMV", "montage": "图片混剪", "subs": "仅 KTV 字幕",
    "oped": "OPED 拼接", "color": "纯色", "image": "图片",
}


def background_spec(store: JobStore, st: dict) -> dict:
    """Effective background: {"kind": video|image|color, "path"?, "color"?} plus
    ``design`` (a rendered AMV / montage video), ``pending_design`` (not rendered
    yet: its still stands in) or ``subs`` (subtitles only on a plain colour)."""
    opts = st.get("options", {})
    bg = dict(opts.get("background") or {})
    has_video = st["media"].get("source", {}).get("has_video")
    kind = bg.get("type") or ("source" if has_video else "mv")
    if kind == "source" and has_video:
        return {"kind": "video", "path": st["media"]["source"]["path"]}
    if kind == "video" and bg.get("path"):
        p = Path(bg["path"])
        p = p if p.is_absolute() else store.abs(bg["path"])
        if p.is_file():
            return {"kind": "video", "path": str(p), "external": True}
    if kind in ("mv", "spectrum", "montage") or (kind in ("source", "video") and not has_video):
        from . import mv

        dk = "montage" if kind == "montage" else "mv"  # "spectrum" = older name of the AMV background
        video = mv.design_video(store, st, dk)
        if video is not None:
            return {"kind": "video", "path": str(video), "design": dk}
        still = ((st.get("media") or {}).get("design_still") or {}).get(dk)
        if still and store.abs(still).is_file():
            return {"kind": "image", "path": str(store.abs(still)), "pending_design": dk}
        return {"kind": "color", "color": "#101426", "pending_design": dk}
    if kind == "subs":
        return {"kind": "color", "color": bg.get("color") or "#000000", "subs": True}
    if kind == "oped":
        op = (st.get("media") or {}).get("oped") or {}
        video = op.get("video")
        if video:
            p = Path(video)
            p = p if p.is_absolute() else store.abs(video)
            if p.is_file():
                return {"kind": "video", "path": str(p), "external": True}
        still = op.get("still")
        if still and store.abs(still).is_file():
            return {"kind": "image", "path": str(store.abs(still))}
        return {"kind": "color", "color": "#101426"}
    if kind == "image" and bg.get("path"):
        p = Path(bg["path"])
        return {"kind": "image", "path": str(p if p.is_absolute() else store.abs(bg["path"]))}
    return {"kind": "color", "color": bg.get("color") or "#0E1022"}


def paint_background(img, bg: dict, t: float, cache_dir: Path | None = None):
    from PyQt6.QtCore import QRectF, Qt
    from PyQt6.QtGui import QColor, QImage, QPainter

    w, h = img.width(), img.height()
    painter = QPainter(img)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    painter.fillRect(0, 0, w, h, QColor(bg.get("color") or "#000000"))
    src = None
    if bg["kind"] == "video":
        src = video_frame(Path(bg["path"]), t, w, h, cache_dir)
    elif bg["kind"] == "image":
        src = QImage(bg["path"])
        if not src.isNull():
            src = src.scaled(w, h, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation)
    if src is not None and not src.isNull():
        painter.drawImage(QRectF((w - src.width()) / 2, (h - src.height()) / 2, src.width(), src.height()), src)
    painter.end()


def render_frame(track, extras, style, t: float, w: int, h: int, bg: dict, *, duration: float | None = None,
                 cache_dir: Path | None = None, dim: float = 0.0, bg_t: float | None = None):
    qt_app()
    from PyQt6.QtGui import QColor, QImage, QPainter

    from krok_helper.subtitle_render.engine.painter import paint_frame

    frame = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
    paint_background(frame, bg, t if bg_t is None else bg_t, cache_dir)
    if dim:
        p = QPainter(frame)
        p.fillRect(0, 0, w, h, QColor(0, 0, 0, int(255 * dim)))
        p.end()
    layer = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
    layer.fill(0)
    paint_frame(layer, track, int(round(t * 1000)), style, extras or None,
                duration_ms=int(duration * 1000) if duration else None)
    p = QPainter(frame)
    p.drawImage(0, 0, layer)
    p.end()
    return frame


# ------------------------------------------------------------ previews
def _sample_project(store: JobStore, st: dict, n_lines: int = 2, per_line: float = 3.2,
                    singer_ids: list[str] | None = None):
    """A tiny SUG project with synthetic, evenly spread timing built from the
    song's own first included lines (ruby included)."""
    from . import sugbridge

    lines = [l for l in st["lyrics"]["lines"] if l.get("include", True) and l["text"].strip()]
    if not lines:
        lines = [{"text": "サンプル歌詞をここに表示", "ruby": [[3, 4, "かし"], [8, 9, "ひょうじ"]]},
                 {"text": "Karaoke Master プレビュー", "ruby": []}]
    picked = []
    for i in range(n_lines):
        src = lines[i % len(lines)]
        picked.append({"text": src["text"], "ruby": [r[:3] for r in src.get("ruby", [])],
                       "singer": (singer_ids[i] if singer_ids and i < len(singer_ids) else src.get("singer"))})
    singers = st["song"].get("singers") or []
    project = sugbridge.build_project(picked, singers=singers,
                                      meta={"title": st["song"].get("title"), "artist": st["song"].get("artist")})
    t = 2.0
    spans = []
    for sentence in project.sentences:
        cps = sum(max(1, ch.check_count) for ch in sentence.characters if ch.check_count > 0) or 1
        step = per_line / cps
        start = t
        for ch in sentence.characters:
            if ch.check_count <= 0:
                ch.timestamps = []
                continue
            ch.timestamps = [int((t + k * step) * 1000) for k in range(ch.check_count)]
            t += step * ch.check_count
            if ch.is_sentence_end:
                ch.sentence_end_ts = int(t * 1000)
        last = sentence.characters[-1]
        if last.is_sentence_end and last.sentence_end_ts is None:
            last.sentence_end_ts = int(t * 1000)
        spans.append((start, t))
        t += 0.25
    path = store.path("previews", "sample.sug")
    sugbridge.save_project(project, path)
    return path, spans


def _preview_background(store: JobStore, st: dict) -> tuple[dict, float]:
    bg = background_spec(store, st)
    seg = st.get("segment") or {}
    dur = st.get("media", {}).get("source", {}).get("duration") or 60
    t_bg = float(seg.get("start", 0)) + (float(seg.get("end", dur)) - float(seg.get("start", 0))) * 0.42
    return bg, t_bg


def _save_scaled(img, path: Path, w: int) -> None:
    from PyQt6.QtCore import Qt

    img.scaledToWidth(w, Qt.TransformationMode.SmoothTransformation).save(str(path), quality=88)


def cmd_previews(store: JobStore, only: list[str] | None = None) -> int:
    qt_app()
    from PyQt6.QtGui import QImage, QPainter

    st = store.load()
    only = set(only or ["templates", "effects", "singers"])
    store.set_status("正在用渲染引擎生成样式预览", "working")
    W, H = 1280, 720
    bg, t_bg = _preview_background(store, st)
    cache = store.path("previews", "cache", "x").parent
    sample, spans = _sample_project(store, st)
    (l1s, l1e), (l2s, l2e) = spans[0], spans[1]
    rev = int(time.time())
    previews = dict(st.get("previews") or {})
    opts = st.get("options", {})

    def frame_for(st_variant: dict, t: float, bg_t: float):
        style = build_style(st_variant, H, include_title=False)
        track, extras = load_track(sample, style, ruby=st_variant.get("options", {}).get("ruby", True))
        return render_frame(track, extras, style, t, W, H, {**bg}, cache_dir=cache, duration=l2e + 2, bg_t=bg_t)

    if "templates" in only:
        out = {}
        for tpl in styles.TEMPLATES:
            variant = json.loads(json.dumps(st))
            variant.setdefault("options", {})["template"] = tpl["id"]
            variant["options"]["effect"] = "classic"
            variant["options"].pop("overrides", None)
            variant["song"]["singers"] = []
            img = frame_for(variant, l1s + (l1e - l1s) * 0.55, t_bg)
            path = store.path("previews", f"tpl_{tpl['id']}.jpg")
            _save_scaled(img, path, 640)
            out[tpl["id"]] = f"previews/tpl_{tpl['id']}.jpg?v={rev}"
            store.log(f"模板预览：{tpl['name']}")
        previews["templates"] = out
    if "effects" in only:
        out = {}
        frames_t = [l1s - 0.55, l1s - 0.15, l1s + (l1e - l1s) * 0.35, l1s + (l1e - l1s) * 0.8,
                    l2s + (l2e - l2s) * 0.5, l2e + 0.05, l2e + 0.35, l2e + 0.9]
        for eff in styles.EFFECTS:
            variant = json.loads(json.dumps(st))
            variant.setdefault("options", {})["effect"] = eff["id"]
            variant["options"].setdefault("template", opts.get("template") or "classic")
            fw, fh = 480, 270
            strip = QImage(fw * len(frames_t), fh, QImage.Format.Format_RGB32)
            p = QPainter(strip)
            for k, t in enumerate(frames_t):
                img = frame_for(variant, t, t_bg)
                p.drawImage(k * fw, 0, img.scaled(fw, fh))
            p.end()
            path = store.path("previews", f"fx_{eff['id']}.jpg")
            strip.save(str(path), quality=85)
            out[eff["id"]] = {"sprite": f"previews/fx_{eff['id']}.jpg?v={rev}", "frames": len(frames_t)}
            store.log(f"特效预览：{eff['name']}")
        previews["effects"] = out
    singers = st["song"].get("singers") or []
    if "singers" in only and singers:
        out = {}
        for k, s in enumerate(singers):
            ids = [s["id"], s["id"]]
            sample_k, spans_k = _sample_project(store, st, n_lines=2, singer_ids=ids)
            style = build_style(st, H, include_title=False)
            track, extras = load_track(sample_k, style, ruby=opts.get("ruby", True))
            a, b = spans_k[0]
            img = render_frame(track, extras, style, a + (b - a) * 0.6, W, H, bg, cache_dir=cache, duration=b + 6,
                               bg_t=t_bg)
            crop = img.copy(0, int(H * 0.52), W, int(H * 0.48))
            path = store.path("previews", f"singer_{s['id']}.jpg")
            _save_scaled(crop, path, 640)
            out[s["id"]] = f"previews/singer_{s['id']}.jpg?v={rev}"
        previews["singers"] = out
        sample.unlink(missing_ok=True)
    previews["catalog"] = styles.catalog()
    store.update(lambda s: s.update(previews=previews))
    write_web_style(store)
    store.set_status("样式预览已更新", "working")
    return 0


def write_web_style(store: JobStore, refresh: bool = True) -> dict:
    qt_app()
    st = store.load()
    opts = st.get("options", {})
    tpl = styles.template(opts.get("template"))
    eff = styles.effect(opts.get("effect"))
    singers = st["song"].get("singers") or []
    colors = styles.singer_colors(singers, opts.get("singer_styles"))
    singers = [dict(s, **colors[s["name"]]) for s in singers]
    ws = styles.web_style(tpl, eff, singers, opts.get("overrides"), font=styles.resolve_font(tpl["fonts"]))
    ws["ruby"] = opts.get("ruby", True)
    ws["by_name"] = {s["name"]: ws["singers"][s["id"]] for s in singers}
    ws["rev"] = int(time.time() * 1000)
    write_json(store.path("render", "web_style.json"), ws)
    store.update(lambda s: s.setdefault("render", {}).update(web_style="render/web_style.json", rev=ws["rev"]))
    if refresh:
        from . import weblayout

        weblayout.refresh(store)  # no-op before timing exists
    return ws


# --------------------------------------------------------------- frames
def _output_size(st: dict) -> tuple[int, int, int]:
    opts = st.get("options", {})
    res = str(opts.get("resolution") or "1920x1080")
    w, h = (int(x) for x in res.lower().split("x"))
    fps = int(opts.get("fps") or 60)
    return w, h, fps if fps in (30, 60, 120) else 60


def _first_line_start(store: JobStore) -> float | None:
    view = read_json(store.dir / "timing" / "timed.json")
    if not view:
        return None
    return next((l["start"] for l in view["lines"] if l.get("start") is not None), None)


def render_job_frame(store: JobStore, t: float, out: Path) -> Path:
    st = store.load()
    w, h, _ = _output_size(st)
    style = build_style(st, h, first_line_start=_first_line_start(store))
    track, extras = load_track(store.dir / "timing" / "project.sug", style, ruby=st.get("options", {}).get("ruby", True))
    bg = background_spec(store, st)
    duration = st["media"].get("source", {}).get("duration")
    img = render_frame(track, extras, style, t, w, h, bg, duration=duration, cache_dir=store.path("previews", "cache", "x").parent)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(out))
    return out


# --------------------------------------------------------------- export
def output_dir(store: JobStore, st: dict) -> Path:
    """Finished files go to ``options.output_dir`` (e.g. an output folder next to
    the user's media) or the project's ``export`` folder."""
    out = (st.get("options") or {}).get("output_dir")
    path = Path(out) if out else store.dir / "export"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _safe_name(st: dict) -> str:
    song = st.get("song", {})
    base = " - ".join(x for x in (song.get("artist"), song.get("title")) if x) or st["id"]
    return re.sub(r"[\\/:*?\"<>|]+", "_", base).strip()[:80]


def _record_export(store: JobStore, kind: str, **fields) -> None:
    def apply(st: dict) -> None:
        items = [e for e in st.get("exports", []) if e.get("kind") != kind]
        items.append({"kind": kind, **fields})
        order = ["preview", "sug", "yurika", "lrc", "mp4", "alpha", "onoff", "mv", "hires"]
        items.sort(key=lambda e: order.index(e["kind"]) if e["kind"] in order else 9)
        st["exports"] = items

    store.update(apply)


def cmd_export(store: JobStore, kinds: list[str], *, clip: float | None = None) -> int:
    """Export deliverables. ``clip`` (seconds) renders only the beginning of the
    karaoke video as a quick preview (``<name> (preview).mp4``)."""
    from . import sugbridge

    st = store.load()
    sug_src = store.dir / "timing" / "project.sug"
    if not sug_src.is_file():
        raise SystemExit("尚未完成打轴")
    name = _safe_name(st)
    out_dir = output_dir(store, st)
    source = st["media"].get("source", {}).get("path")
    sug_out = out_dir / f"{name}.sug"
    project = sugbridge.load_project(sug_src)
    sugbridge.save_project(project, sug_out, media_path=source)
    if "sug" in kinds:
        _record_export(store, "sug", state="done", path=store.rel(sug_out), label="SUG 打轴工程")
        store.log(f"已导出 SUG 工程：{sug_out.name}")
    if "lrc" in kinds:
        lrc = sugbridge.export_lyrics(project, out_dir / f"{name}.lrc", "LRC (逐字)")
        _record_export(store, "lrc", state="done", path=store.rel(lrc), label="逐字 LRC")
    w, h, fps = _output_size(st)
    style = build_style(st, h, first_line_start=_first_line_start(store))
    track, extras = load_track(sug_out, style, ruby=st.get("options", {}).get("ruby", True))
    bg = background_spec(store, st)
    if "yurika" in kinds:
        path = _save_yurika(store, st, out_dir / f"{name}.yurika", sug_out, track, extras, style, bg, w, h, fps)
        _record_export(store, "yurika", state="done", path=store.rel(path), label="字幕渲染工程（.yurika）")
        store.log(f"已导出字幕工程：{path.name}")
    design = bg.get("pending_design") or bg.get("design")
    if design and ("mv" in kinds or ("mp4" in kinds and bg.get("pending_design"))):
        from . import mv

        if clip:
            clip_bg = mv.render_video(store, kind=design, seconds=clip + 1)
            bg = {"kind": "video", "path": str(clip_bg), "design": design}
        elif bg.get("pending_design"):
            mv.render_video(store, kind=design)
            st = store.load()
            bg = background_spec(store, st)
    if "mp4" in kinds:
        out = out_dir / (f"{name} (preview).mp4" if clip else f"{name}.mp4")
        _export_mp4(store, st, out, track, extras, style, bg, w, h, fps, clip=clip)
    if "alpha" in kinds:
        out = out_dir / (f"{name} (透明字幕 preview).mov" if clip else f"{name} (透明字幕).mov")
        _guarded(store, "alpha", "透明字幕层（ProRes 4444 MOV）",
                 lambda: _export_alpha(store, st, out, track, extras, style, w, h, fps, clip=clip))
    if clip:
        return 0
    if "onoff" in kinds:
        from . import versions

        _guarded(store, "onoff", "on / off vocal 双版本", lambda: versions.cmd_versions(store))
    if "mv" in kinds and bg.get("design"):
        from . import mv, versions

        mv_out = out_dir / (f"{name} (混剪).mp4" if bg["design"] == "montage" else f"{name} (MV).mp4")
        shutil.copy2(bg["path"], mv_out)
        label = f"{mv.KIND_LABEL[bg['design']]}（无字幕版）"
        _guarded(store, "mv", label, lambda: versions.cmd_versions(store, video=str(mv_out), kind="mv"))
    if "hires" in kinds:
        from . import hires

        try:
            hires.cmd_hires(store)
        except BaseException as exc:
            _record_export(store, "hires", state="error", label="Hi-Res 混流（MKV）", path="", error=str(exc))
            store.set_status(f"Hi-Res 混流失败：{exc}", "error")
            raise
    return 0


def _guarded(store: JobStore, kind: str, label: str, fn) -> None:
    try:
        fn()
    except BaseException as exc:
        _record_export(store, kind, state="error", label=label, path="", error=str(exc))
        store.set_status(f"{label}失败：{exc}", "error")
        raise


def _background_source(bg: dict):
    from krok_helper.subtitle_render.domain.background import BackgroundSource

    if bg["kind"] == "video":
        return BackgroundSource(kind="video", path=Path(bg["path"]))
    if bg["kind"] == "image":
        return BackgroundSource(kind="image", path=Path(bg["path"]), image_fit="cover")
    return BackgroundSource(kind="solid", color=bg.get("color") or "#000000")


def _save_yurika(store, st, path: Path, sug: Path, track, extras, style, bg, w, h, fps) -> Path:
    from krok_helper.subtitle_render.project.session import SubtitleProjectDocument
    from krok_helper.subtitle_render.project.store import project_output_payload, save_render_project
    from krok_helper.subtitle_render.settings.screen import ScreenSettings, screen_settings_to_dict

    source = _background_source(bg)
    audio = None
    if bg["kind"] != "video":  # Lin-K Lyrics: a video background only plays its own audio track
        from .analysis import timing_audio

        audio = timing_audio(store, st) if st["media"].get("audio") else None
    roles = sorted({c.role_label for line in track.lines for c in line.chars if getattr(c, "role_label", None)})
    doc = SubtitleProjectDocument(
        timing_track=track, subtitle_path=sug.resolve(),
        video_path=Path(bg["path"]) if bg["kind"] == "video" else None,
        background_source=source, audio_path=audio, style=style, role_names=roles)
    preset = "hdtv_1080" if (w, h) == (1920, 1080) else "custom"
    fps = fps if fps in (60, 120) else 60  # Lin-K Lyrics step 5 offers 60 / 120 fps only
    data = doc.to_project_data(
        screen=screen_settings_to_dict(ScreenSettings(preset_key=preset, width=w, height=h, fps=fps)),
        selected_scheme_key="global",
        output=project_output_payload(encoder_mode="cpu", crf=18, preset="medium",
                                      output_path=str(path.with_suffix(".mp4"))),
    )
    save_render_project(path, data)
    return path


def _encoder(st: dict) -> str:
    """User choice, else the acceleration profile's hardware encoder (NVENC / QSV /
    AMF); the renderer falls back to libx264 if the hardware encoder fails."""
    from . import accel

    return (st.get("options") or {}).get("encoder") or accel.profile().get("encoder") or "cpu"


def _codec(st: dict) -> str:
    """Video codec for the MP4 export: ``h264`` (default) or ``hevc``."""
    from . import accel

    value = (st.get("options") or {}).get("codec") or accel.profile().get("codec") or "h264"
    return "hevc" if str(value).lower() == "hevc" else "h264"


def _render_workers() -> int:
    """Renderer processes: ``render_workers`` setting, else a conservative half of
    the cores (max 4) so exporting never saturates the machine."""
    from . import accel

    value = accel.profile().get("render_workers")
    if value:
        return max(1, int(value))
    return max(1, min(4, (os.cpu_count() or 2) // 2))


def _export_alpha(store, st, out: Path, track, extras, style, w, h, fps, *, clip: float | None = None) -> Path:
    """Subtitles alone with an alpha channel (ProRes 4444 MOV + PCM audio) for
    compositing in an editor (PR / AE / DaVinci)."""
    from krok_helper.subtitle_render.engine.export.render_job import OUTPUT_FORMAT_MOV_TRANSPARENT, RenderJob
    from krok_helper.subtitle_render.engine.renderer import render_subtitle_video

    from .analysis import timing_audio

    duration = float(st["media"].get("source", {}).get("duration") or 0)
    if clip:
        duration = min(duration, float(clip))
    audio = timing_audio(store, st) if st["media"].get("audio") else None
    from . import tracks

    if audio is not None and tracks.cast_delay_ms(st):
        audio = tracks.delayed_audio(store, audio, tracks.cast_delay_ms(st))
    job = RenderJob(
        track=track, style=style, background_video_path=None, background_source=None, audio_path=audio,
        output_path=out, width=w, height=h, fps=fps, duration_ms=int(duration * 1000),
        include_audio=audio is not None, output_format=OUTPUT_FORMAT_MOV_TRANSPARENT,
        extra_tracks=tuple(extras), gpu_export_enabled=False, render_workers=_render_workers(),
    )
    label = "透明字幕层（ProRes 4444 MOV）"
    t0, last = time.time(), [0.0]
    _record_export(store, "alpha", state="running", progress=0.0, path=store.rel(out), label=label)
    store.set_status("正在渲染透明字幕层", "working")

    def on_progress(done: int, total: int) -> None:
        now = time.time()
        if now - last[0] < 1.0 and done < total:
            return
        last[0] = now
        frac = done / max(1, total)
        eta = (now - t0) / max(1e-3, frac) - (now - t0) if frac > 0.02 else None
        _record_export(store, "alpha", state="running", progress=round(frac, 4), path=store.rel(out), label=label,
                       eta=None if eta is None else round(eta), frames=f"{done}/{total}")

    render_subtitle_video(job, logger=lambda m: store.log("渲染：" + str(m)), on_progress=on_progress)
    _record_export(store, "alpha", state="done", progress=1.0, path=store.rel(out), label=label,
                   size=out.stat().st_size if out.exists() else 0, seconds=round(time.time() - t0),
                   notes=["带透明通道，导入 PR / AE / 达芬奇等剪辑软件叠加使用；普通播放器可能显示为黑底",
                          "ProRes 4444 体积较大（整首歌通常数 GB）"])
    store.set_status(f"透明字幕层已导出（用时 {time.time() - t0:.0f} 秒）", "done")
    return out


def _export_mp4(store, st, out: Path, track, extras, style, bg, w, h, fps, *, clip: float | None = None) -> Path:
    from krok_helper.subtitle_render.engine.export.render_job import RenderJob
    from krok_helper.subtitle_render.engine.renderer import render_subtitle_video

    duration = float(st["media"].get("source", {}).get("duration") or 0)
    if bg.get("design"):
        from . import media

        duration = max(duration, media.probe(bg["path"])["duration"])
    if clip:
        duration = min(duration, float(clip))
    from . import tracks
    from .analysis import timing_audio

    delay = tracks.cast_delay_ms(st)
    audio = None
    post_audio = None  # muxed in after the render (stream copy of the video)
    if bg["kind"] != "video":
        audio = timing_audio(store, st)
        if delay:  # 投屏延迟: the sound comes `delay` ms after the picture
            audio = tracks.delayed_audio(store, audio, delay)
    elif bg.get("external") or delay or st["media"].get("hires"):
        # the engine only plays a video background's own track: render with it, then put the song
        # audio (external MV: picture only / Hi-Res source / 投屏延迟) in with a remux
        post_audio = tracks.delayed_audio(store, timing_audio(store, st), delay)
    job = RenderJob(
        track=track, style=style,
        background_video_path=Path(bg["path"]) if bg["kind"] == "video" else None,
        background_source=_background_source(bg), audio_path=audio,
        output_path=out, width=w, height=h, fps=fps, duration_ms=int(duration * 1000),
        include_audio=True, encoder_mode=_encoder(st), codec=_codec(st), crf=18, preset="medium",
        extra_tracks=tuple(extras), gpu_export_enabled=False, render_workers=_render_workers(),
    )
    kind, label = ("preview", "试看片段") if clip else ("mp4", "成品 MP4")
    preview_jpg = store.path("render", "export_preview.jpg")
    t0 = time.time()
    last = [0.0]
    _record_export(store, kind, state="running", progress=0.0, path=store.rel(out), label=label,
                   preview="render/export_preview.jpg")
    store.set_status("正在渲染成品 MP4", "working")

    def on_progress(done: int, total: int) -> None:
        now = time.time()
        if now - last[0] < 1.0 and done < total:
            return
        last[0] = now
        frac = done / max(1, total)
        eta = (now - t0) / max(1e-3, frac) - (now - t0) if frac > 0.02 else None
        _record_export(store, kind, state="running", progress=round(frac, 4), path=store.rel(out),
                       label=label, eta=None if eta is None else round(eta), preview="render/export_preview.jpg",
                       frames=f"{done}/{total}")

    try:
        render_subtitle_video(job, logger=lambda m: store.log("渲染：" + str(m)), on_progress=on_progress,
                              preview_image_path=preview_jpg, preview_width=640)
    except Exception as exc:
        _record_export(store, kind, state="error", path=store.rel(out), label=label, error=str(exc))
        store.set_status(f"MP4 导出失败：{exc}", "error")
        raise
    if post_audio is not None:
        from .versions import remux_audio

        store.set_status("正在写入歌曲音轨", "working")
        tmp = out.with_name(out.stem + ".part.mp4")
        remux_audio(out, Path(post_audio), tmp)
        os.replace(tmp, out)
    size = out.stat().st_size if out.exists() else 0
    _record_export(store, kind, state="done", progress=1.0, path=store.rel(out), label=label,
                   size=size, seconds=round(time.time() - t0))
    store.set_status(f"成品 MP4 已导出（用时 {time.time() - t0:.0f} 秒）", "done")
    return out
