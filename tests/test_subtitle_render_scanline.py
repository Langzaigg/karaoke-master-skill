"""Karaoke scan-line（扫字线）model/serialization/painter/GPU contracts."""

from __future__ import annotations

import os
from dataclasses import replace

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QRectF  # noqa: E402
from PyQt6.QtGui import QColor, QImage, QPainter, QPainterPath  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from krok_helper.subtitle_render.domain.models import (  # noqa: E402
    Style,
    SubtitleStyleScheme,
    effective_karaoke_animation,
    effective_karaoke_scanline,
    rescale_font_sizes,
    style_from_dict,
    style_to_dict,
    style_with_line_animation,
)
from krok_helper.subtitle_render.domain.timing import (  # noqa: E402
    LineAnimationOverride,
    TimingChar,
    TimingLine,
    TimingTrack,
)
from krok_helper.subtitle_render.native.protocol import (  # noqa: E402
    gpu_unsupported_features,
)
from krok_helper.subtitle_render.serialization.timing import (  # noqa: E402
    line_animation_override_from_dict,
    line_animation_override_to_dict,
)
from krok_helper.subtitle_render.engine.painter import paint_frame  # noqa: E402
from krok_helper.subtitle_render.engine.render.elements.horizontal.scanline import (  # noqa: E402
    ScanlineParams,
    _brighten_color_hsv,
    paint_scanline_strip,
)


def _scanline_style(**changes) -> Style:
    base = dict(
        entry_anim="none",
        exit_anim="none",
        sync_entry=False,
        sync_ending=False,
        sync_each_page=False,
        line_lead_in_ms=0,
        line_tail_ms=200,
        scanline_width_px=18,
        scanline_color="#40E0FF",
        scanline_glow_px=6,
    )
    base.update(changes)
    return Style(**base)


def _wiping_track() -> TimingTrack:
    return TimingTrack(
        lines=[
            TimingLine(
                chars=[
                    TimingChar("歌", 0),
                    TimingChar("詞", 400),
                    TimingChar("測", 800),
                ],
                end_ms=1200,
            )
        ]
    )


# ---------------------------------------------------------------------------
# 模型与序列化
# ---------------------------------------------------------------------------


def test_scanline_modes_resolve_to_base_karaoke_animation() -> None:
    assert (
        effective_karaoke_animation(_scanline_style(karaoke_anim="scanline")) == "none"
    )
    assert (
        effective_karaoke_animation(_scanline_style(karaoke_anim="utopia_scanline"))
        == "utopia"
    )
    assert effective_karaoke_animation(_scanline_style(karaoke_anim="none")) == "none"
    assert (
        effective_karaoke_animation(_scanline_style(karaoke_anim="utopia")) == "utopia"
    )


def test_effective_karaoke_scanline_only_accepts_explicit_modes() -> None:
    assert effective_karaoke_scanline(_scanline_style(karaoke_anim="scanline"))
    assert effective_karaoke_scanline(_scanline_style(karaoke_anim="utopia_scanline"))
    assert not effective_karaoke_scanline(_scanline_style(karaoke_anim="utopia"))
    assert not effective_karaoke_scanline(_scanline_style(karaoke_anim="none"))
    # inherit 的旧推导（入退场含 utopia）不产生扫字线。
    assert not effective_karaoke_scanline(
        _scanline_style(karaoke_anim="inherit", entry_anim="utopia")
    )


def test_per_line_override_controls_scanline_enablement() -> None:
    style = _scanline_style(karaoke_anim="utopia_scanline")
    line = TimingLine(
        chars=[TimingChar("歌", 0)],
        end_ms=500,
        animation_override=LineAnimationOverride(karaoke_anim="none"),
    )
    assert effective_karaoke_scanline(style_with_line_animation(style, line)) is False
    line_keep = TimingLine(chars=[TimingChar("歌", 0)], end_ms=500)
    assert effective_karaoke_scanline(style_with_line_animation(style, line_keep))


def test_scanline_style_fields_round_trip() -> None:
    style = _scanline_style(
        karaoke_anim="utopia_scanline",
        scanline_width_px=33,
        scanline_mode="brighten",
        scanline_color="#123456",
        scanline_brightness_pct=45,
        scanline_glow_px=12,
    )
    restored = style_from_dict(style_to_dict(style))
    assert restored.karaoke_anim == "utopia_scanline"
    assert restored.scanline_width_px == 33
    assert restored.scanline_mode == "brighten"
    assert restored.scanline_color == "#123456"
    assert restored.scanline_brightness_pct == 45
    assert restored.scanline_glow_px == 12
    # 非法模式回落 color，亮度越界钳制在 style 层由参数面板/渲染端兜底。
    assert style_from_dict({"scanline_mode": "wat"}).scanline_mode == "color"


def test_scanline_pixel_fields_use_fixed_1080_base() -> None:
    """扫字线像素字段固定 1080 基准:渲染按画布映射,存储不随高度重写。"""
    from krok_helper.subtitle_render.domain.models import (
        SCANLINE_BASE_HEIGHT,
        scanline_base_px_from_output,
        scanline_px_for_output,
        style_with_output_scanline,
    )

    style = _scanline_style(
        karaoke_anim="utopia_scanline",
        scanline_width_px=25,
        scanline_glow_px=8,
        scanline_mode="brighten",
        scanline_brightness_pct=60,
    )

    # 双向换算:4K(高 2160)实画 100px → 基准 50;基准 25 → 2K(1440) 实画 33。
    assert scanline_base_px_from_output(100, 2160) == 50
    assert scanline_px_for_output(25, 1440) == 33
    assert scanline_px_for_output(25, 1080) == 25
    # 0 在任何高度保持 0。
    assert scanline_px_for_output(0, 2160) == 0

    # 输出高度重算(字号走 SizeAndRatio)不再改写扫字线:任何画布切换后
    # 扫字线都从同一基准重新推导,与切换历史无关。
    up = rescale_font_sizes(style, 2160)
    assert up.font_reference_height == 2160
    assert up.scanline_width_px == 25
    assert up.scanline_glow_px == 8

    # 渲染入口把基准值换算为输出高度下的实画值;1080/非法高度原对象返回。
    scaled = style_with_output_scanline(up, 1440)
    assert scaled.scanline_width_px == 33
    assert scaled.scanline_glow_px == 11
    assert style_with_output_scanline(scaled, 1080) is scaled
    assert style_with_output_scanline(scaled, 0) is scaled

    # 幂等:同一基准在 4K↔2K 间任意切换,实画值只由(基准,画布)决定。
    assert style_with_output_scanline(up, 2160).scanline_width_px == 50
    back = rescale_font_sizes(up, 1440)
    assert style_with_output_scanline(back, 2160).scanline_width_px == 50
    assert SCANLINE_BASE_HEIGHT == 1080


def test_scanline_px_base_migration_on_load() -> None:
    """旧 payload(无基准标记)按工程 font_reference_height 一次性折算到 1080。"""
    legacy = style_from_dict(
        {
            "font_reference_height": 2160,
            "scanline_width_px": 100,
            "scanline_glow_px": 16,
        }
    )
    # 4K 工程里的实画值 100/16 迁移为 1080 基准 50/8。
    assert legacy.scanline_width_px == 50
    assert legacy.scanline_glow_px == 8

    # 新格式(带标记)原样采用,即使工程基准不是 1080。
    current = style_from_dict(
        {
            "font_reference_height": 2160,
            "scanline_width_px": 25,
            "scanline_glow_px": 8,
            "scanline_px_base": 1080,
        }
    )
    assert current.scanline_width_px == 25
    assert current.scanline_glow_px == 8

    # 存盘往返:写出端带基准标记,值保持 1080 语义。
    payload = style_to_dict(Style(scanline_width_px=33, scanline_glow_px=9))
    assert payload["scanline_px_base"] == 1080
    restored = style_from_dict(payload)
    assert restored.scanline_width_px == 33
    assert restored.scanline_glow_px == 9


def test_reverse_karaoke_scanline_bakes_per_line() -> None:
    style = _scanline_style(
        karaoke_anim="utopia",
        reverse_karaoke_anim="scanline",
    )
    forward = TimingLine(chars=[TimingChar("歌", 0)], end_ms=500)
    reverse = TimingLine(chars=[TimingChar("歌", 0)], end_ms=500, wipe_reverse=True)
    assert not effective_karaoke_scanline(style_with_line_animation(style, forward))
    assert effective_karaoke_scanline(style_with_line_animation(style, reverse))
    # inherit 沿用正向档位。
    follow = _scanline_style(karaoke_anim="scanline", reverse_karaoke_anim="inherit")
    assert effective_karaoke_scanline(style_with_line_animation(follow, reverse))


def test_scanline_line_override_serialization_round_trip() -> None:
    override = LineAnimationOverride(karaoke_anim="utopia_scanline")
    data = line_animation_override_to_dict(override)
    assert data["karaoke_anim"] == "utopia_scanline"
    restored = line_animation_override_from_dict(
        {"entry_anim": "none", "exit_anim": "none", "karaoke_anim": "scanline"}
    )
    assert restored is not None and restored.karaoke_anim == "scanline"
    legacy = line_animation_override_from_dict(
        {"entry_anim": "none", "exit_anim": "none"}
    )
    assert legacy is not None and legacy.karaoke_anim == "inherit"
    invalid = line_animation_override_from_dict(
        {"entry_anim": "none", "exit_anim": "none", "karaoke_anim": "wat"}
    )
    assert invalid is not None and invalid.karaoke_anim == "inherit"


def test_scanline_modes_do_not_force_gpu_fallback() -> None:
    style = _scanline_style(karaoke_anim="utopia_scanline")
    assert gpu_unsupported_features(_wiping_track(), style) == ()
    plain = _scanline_style(karaoke_anim="scanline")
    assert gpu_unsupported_features(_wiping_track(), plain) == ()


def test_render_only_anim_fields_leave_layout_signature_intact() -> None:
    from krok_helper.subtitle_render.engine.value_signature import (
        lyric_layout_style_signature,
    )

    base = lyric_layout_style_signature(_scanline_style())
    assert base == lyric_layout_style_signature(
        _scanline_style(
            karaoke_anim="scanline",
            reverse_karaoke_anim="utopia_scanline",
            scanline_mode="brighten",
            scanline_width_px=99,
            scanline_color="#123456",
            scanline_brightness_pct=20,
            scanline_glow_px=30,
        )
    )
    # 出入场类型仍参与签名（跨 none 会移动显示窗口，必须重建计划）。
    assert base != lyric_layout_style_signature(_scanline_style(entry_anim="fade"))


def test_anim_type_delta_window_check() -> None:
    from krok_helper.subtitle_render.frontend.main_window import (
        _ANIM_TYPE_STYLE_FIELDS,
        _RENDER_ONLY_ANIM_STYLE_FIELDS,
        _animation_windows_unchanged,
        _style_delta_only_in,
    )

    base = _scanline_style(entry_anim="fade", exit_anim="fade")
    # 类型等价切换（fade→slide，时长不变）：逐行有效动画时长不变 → 窗口不动。
    changed = replace(base, entry_anim="slide_in", exit_anim="slide_out")
    assert _style_delta_only_in(base, changed, _ANIM_TYPE_STYLE_FIELDS)
    track = _wiping_track()
    assert _animation_windows_unchanged(base, changed, (track,))
    # 跨 none 边界：有效时长 250ms→0，窗口会缩。
    assert not _animation_windows_unchanged(
        base, replace(base, entry_anim="none"), (track,)
    )
    # 唱字/扫字线字段属于渲染专属集合；混入非动画字段则不成立。
    scan = replace(base, karaoke_anim="scanline", scanline_width_px=40)
    assert _style_delta_only_in(base, scan, _RENDER_ONLY_ANIM_STYLE_FIELDS)
    mixed = replace(base, karaoke_anim="scanline", font_size_px=80)
    assert not _style_delta_only_in(base, mixed, _RENDER_ONLY_ANIM_STYLE_FIELDS)


def test_render_ir_paint_scope_reuses_plan_across_scanline_edits() -> None:
    """唱字档位变更走 paint scope：布局计划命中复用，IR 逐行动画仍更新。"""

    from krok_helper.subtitle_render.engine.render.render_ir import build_render_ir

    track = _wiping_track()
    base = build_render_ir(
        track, _scanline_style(karaoke_anim="utopia"), width=640, height=360, fps=60
    )
    # 唱字/扫字线字段不在歌词布局签名里：paint scope 应命中上一份计划缓存，
    # 由 _rebind_plan_line_styles 把新动画样式刷进 IR 行。
    reused = build_render_ir(
        track,
        _scanline_style(karaoke_anim="utopia_scanline", scanline_width_px=40),
        width=640,
        height=360,
        fps=60,
        relayout_scope="paint",
    )
    assert [line["scanline"] for line in base["track"]["lines"]] == [False]
    assert [line["karaoke_anim"] for line in base["track"]["lines"]] == ["utopia"]
    assert [line["scanline"] for line in reused["track"]["lines"]] == [True]
    assert [line["karaoke_anim"] for line in reused["track"]["lines"]] == ["utopia"]
    # IR 携带的是输出高度下的实画值:基准 40 在 360 高画布上换算为 13。
    assert reused["style"]["scanline_width_px"] == 13


def test_render_ir_stamps_per_line_scanline_flag() -> None:
    from krok_helper.subtitle_render.engine.render.render_ir import build_render_ir

    track = _wiping_track()
    for karaoke, expected in (
        ("scanline", True),
        ("utopia_scanline", True),
        ("utopia", False),
        ("none", False),
    ):
        ir = build_render_ir(
            track,
            _scanline_style(karaoke_anim=karaoke),
            width=640,
            height=360,
            fps=60,
        )
        flags = [line["scanline"] for line in ir["track"]["lines"]]
        assert flags == [expected], karaoke
        # 唱字动画本体仍按基础档位下发（GPU 的 Wipe/Utopia 机制复用）。
        assert ir["track"]["lines"][0]["karaoke_anim"] == effective_karaoke_animation(
            _scanline_style(karaoke_anim=karaoke)
        )
        # 参数随样式整包进入 IR,sidecar 解析后即可绘制;像素值为 360 高
        # 画布下的实画换算值(基准 18/6 → 6/2)。
        assert ir["style"]["scanline_width_px"] == 6
        assert ir["style"]["scanline_color"] == "#40E0FF"
        assert ir["style"]["scanline_glow_px"] == 2
        assert ir["style"]["scanline_mode"] == "color"
        assert ir["style"]["scanline_brightness_pct"] == 60


# ---------------------------------------------------------------------------
# CPU Painter
# ---------------------------------------------------------------------------


def test_scanline_brighten_raises_hsv_value_without_washing_out_hue() -> None:
    from PyQt6.QtGui import QColor

    source = QColor("#804020")
    raised = QColor(_brighten_color_hsv(source.name(), 0.5))
    source_h, source_s, source_v, _ = source.getHsvF()
    raised_h, raised_s, raised_v, _ = raised.getHsvF()
    assert raised_h == pytest.approx(source_h, abs=0.01)
    assert raised_s == pytest.approx(source_s, abs=0.01)
    assert raised_v > source_v
    assert raised != QColor("#BFA090")  # 旧的半透明白色叠加结果


def test_scanline_soft_radius_consumes_the_hard_core() -> None:
    sharp = ScanlineParams(width_px=20, color="#FFFFFF", glow_px=0)
    softened = ScanlineParams(width_px=20, color="#FFFFFF", glow_px=6)
    fully_soft = ScanlineParams(width_px=20, color="#FFFFFF", glow_px=10)
    assert sharp.solid_half_width == 10
    assert softened.solid_half_width == 4
    assert fully_soft.solid_half_width == 0


def test_scanline_softness_stays_inside_glyph_geometry() -> None:
    image = QImage(120, 60, QImage.Format.Format_RGBA8888)
    image.fill(0)
    path = QPainterPath()
    path.addRect(QRectF(30, 10, 40, 40))
    painter = QPainter(image)
    try:
        # 调整层语义：先画原始字形（真实链路里扫字线永远叠在已画内容上），
        # 否则空画布上无内容可调，高亮 alpha 恒为 0。
        painter.fillPath(path, QColor("#806050"))
        paint_scanline_strip(
            painter,
            path,
            path.boundingRect(),
            front=50,
            params=ScanlineParams(width_px=20, color="#FFFFFF", glow_px=10),
            style=_scanline_style(stroke_width_px=0, stroke2_width_px=0),
        )
    finally:
        painter.end()
    pixels = np.frombuffer(image.bits().asstring(image.sizeInBytes()), dtype=np.uint8)
    alpha = pixels.reshape(60, 120, 4)[:, :, 3]
    assert np.count_nonzero(alpha[:, :30]) == 0
    assert np.count_nonzero(alpha[:, 70:]) == 0
    assert np.count_nonzero(alpha[:, 30:70]) > 0


def test_scanline_is_empty_while_front_crosses_a_transparent_glyph_gap() -> None:
    image = QImage(120, 60, QImage.Format.Format_RGBA8888)
    image.fill(0)
    path = QPainterPath()
    path.addRect(QRectF(10, 10, 20, 40))
    path.addRect(QRectF(70, 10, 20, 40))
    painter = QPainter(image)
    try:
        paint_scanline_strip(
            painter,
            path,
            path.boundingRect(),
            front=50,
            params=ScanlineParams(width_px=10, color="#FFFFFF", glow_px=5),
            style=_scanline_style(stroke_width_px=0, stroke2_width_px=0),
        )
    finally:
        painter.end()
    pixels = np.frombuffer(image.bits().asstring(image.sizeInBytes()), dtype=np.uint8)
    assert np.count_nonzero(pixels.reshape(60, 120, 4)[:, :, 3]) == 0


def _frame_bytes(track: TimingTrack, style: Style, t_ms: int) -> bytes:
    image = QImage(800, 450, QImage.Format.Format_RGBA8888)
    image.fill(0)
    paint_frame(image, track, t_ms, style)
    image = image.convertToFormat(QImage.Format.Format_RGBA8888)
    bits = image.constBits()
    bits.setsize(image.sizeInBytes())
    return bytes(bits)


def _visible_diff(left: bytes, right: bytes) -> int:
    a = np.frombuffer(left, dtype=np.uint8).reshape(-1, 4).astype(np.int16)
    b = np.frombuffer(right, dtype=np.uint8).reshape(-1, 4).astype(np.int16)
    return int(np.count_nonzero(np.abs(a - b).max(axis=1) > 2))


def test_painter_scanline_highlights_the_moving_front(qapp) -> None:
    track = _wiping_track()
    base = _scanline_style(karaoke_anim="none")
    scanline = _scanline_style(karaoke_anim="scanline")

    # 走字进行中：锋面高亮带带来可见差异。
    mid_base = _frame_bytes(track, base, 500)
    mid_scan = _frame_bytes(track, scanline, 500)
    assert _visible_diff(mid_base, mid_scan) > 50

    # 未开始与已唱完：锋面不存在，两档画面一致。
    early_base = _frame_bytes(track, base, 0)
    early_scan = _frame_bytes(track, scanline, 0)
    assert _visible_diff(early_base, early_scan) == 0
    late_base = _frame_bytes(track, base, 1200)
    late_scan = _frame_bytes(track, scanline, 1200)
    assert _visible_diff(late_base, late_scan) == 0


def test_painter_utopia_scanline_varies_with_width(qapp) -> None:
    track = _wiping_track()
    narrow = _frame_bytes(
        track,
        _scanline_style(karaoke_anim="utopia_scanline", scanline_width_px=6),
        500,
    )
    wide = _frame_bytes(
        track,
        _scanline_style(karaoke_anim="utopia_scanline", scanline_width_px=60),
        500,
    )
    # 更粗的高亮带覆盖更多文字，两帧可见差异。
    assert _visible_diff(narrow, wide) > 50


def test_painter_scanline_brighten_mode_lifts_existing_colors(qapp) -> None:
    track = _wiping_track()
    # 450 高测试画布下扫字线按 1080 基准映射:基准 43/14 ≈ 实画 18/6,
    # 与旧直读语义同带宽,保持本测试的像素差异阈值口径。
    lifted = dict(scanline_width_px=43, scanline_glow_px=14)
    base = _frame_bytes(track, _scanline_style(karaoke_anim="none"), 500)
    color = _frame_bytes(
        track,
        _scanline_style(karaoke_anim="scanline", **lifted),
        500,
    )
    brighten = _frame_bytes(
        track,
        _scanline_style(
            karaoke_anim="scanline",
            scanline_mode="brighten",
            scanline_brightness_pct=70,
            **lifted,
        ),
        500,
    )
    # 底色发光相对基准有提亮差异，且与单独颜色的画面不同。
    assert _visible_diff(base, brighten) > 50
    assert _visible_diff(color, brighten) > 50
    # 亮度 0 = 不提亮：与基准一致。
    zero = _frame_bytes(
        track,
        _scanline_style(
            karaoke_anim="scanline",
            scanline_mode="brighten",
            scanline_brightness_pct=0,
            **lifted,
        ),
        500,
    )
    assert _visible_diff(base, zero) == 0


def test_painter_reverse_line_scanline_follows_reverse_front(qapp) -> None:
    from krok_helper.subtitle_render.domain.timing import normalize_reversed_wipe_lines

    # 整行严格逆序（[5]歌[3]词[>1]）在加载入口镜像理顺并打反向标记。
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar("歌", 5), TimingChar("詞", 3)],
                end_ms=1,
            )
        ]
    )
    normalize_reversed_wipe_lines(track)
    assert track.lines[0].wipe_reverse
    base = _scanline_style(karaoke_anim="none", reverse_karaoke_anim="inherit")
    reverse = _scanline_style(karaoke_anim="none", reverse_karaoke_anim="scanline")
    base_frame = _frame_bytes(track, base, 3)
    reverse_frame = _frame_bytes(track, reverse, 3)
    assert _visible_diff(base_frame, reverse_frame) > 30


def test_painter_scanline_covers_ruby_front(qapp) -> None:
    from krok_helper.subtitle_render.domain.timing import RubyAnnotation

    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar("歌", 0), TimingChar("詞", 600)],
                end_ms=1200,
            )
        ],
        rubies=[
            RubyAnnotation(
                kanji="歌",
                reading="うた",
                reading_parts=["う", "た"],
                reading_part_ms=[300],
                pos_start_ms=0,
                pos_end_ms=600,
            ),
            RubyAnnotation(
                kanji="詞",
                reading="し",
                reading_parts=["し"],
                reading_part_ms=[],
                pos_start_ms=600,
                pos_end_ms=1200,
            ),
        ],
    )
    base = _scanline_style(karaoke_anim="none", font_size_px=48)
    scanline = _scanline_style(karaoke_anim="scanline", font_size_px=48)
    mid_base = _frame_bytes(track, base, 300)
    mid_scan = _frame_bytes(track, scanline, 300)
    assert _visible_diff(mid_base, mid_scan) > 20


# ---------------------------------------------------------------------------
# GPU sidecar（Direct2D）
# ---------------------------------------------------------------------------

_IS_WINDOWS = os.name == "nt"


@pytest.mark.skipif(not _IS_WINDOWS, reason="Direct2D GPU backend is Windows-only")
def test_gpu_scanline_paints_front_band(monkeypatch) -> None:
    pytest.importorskip("PyQt6.QtWidgets")
    from krok_helper.subtitle_render.native.backend import (
        NativeRendererProcess,
        SharedFrameRingReader,
        resolve_native_renderer_path,
    )

    renderer_path = resolve_native_renderer_path()
    if renderer_path is None:
        pytest.skip("native renderer sidecar not built")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    QApplication.instance() or QApplication([])

    track = _wiping_track()

    def gpu_frames(style: Style) -> list[bytes]:
        with NativeRendererProcess(renderer_path, response_timeout_s=60.0) as renderer:
            renderer.configure_gpu(
                track,
                style,
                width=640,
                height=360,
                fps=60,
                force_warp=True,
            )
            frames: list[bytes] = []
            reader: SharedFrameRingReader | None = None
            try:
                for index, t_ms in enumerate((0, 500, 1200)):
                    event = renderer.render_gpu_frame(
                        t_ms, force_warp=True, frame_index=index
                    )
                    if reader is None:
                        reader = SharedFrameRingReader.from_event(event)
                        reader.attach()
                    image = reader.read_qimage(event).convertToFormat(
                        QImage.Format.Format_RGBA8888
                    )
                    bits = image.constBits()
                    bits.setsize(image.sizeInBytes())
                    frames.append(bytes(bits))
            finally:
                if reader is not None:
                    reader.close()
            return frames

    base = gpu_frames(_scanline_style(karaoke_anim="none"))
    scan = gpu_frames(_scanline_style(karaoke_anim="scanline"))
    # 走字中帧有高亮带差异；起止帧（未开始/已完成）保持一致。
    assert _visible_diff(base[0], scan[0]) == 0
    assert _visible_diff(base[1], scan[1]) > 50
    assert _visible_diff(base[2], scan[2]) == 0


@pytest.mark.skipif(not _IS_WINDOWS, reason="Direct2D GPU backend is Windows-only")
def test_gpu_scanline_brighten_mode(monkeypatch) -> None:
    pytest.importorskip("PyQt6.QtWidgets")
    from krok_helper.subtitle_render.native.backend import (
        NativeRendererProcess,
        SharedFrameRingReader,
        resolve_native_renderer_path,
    )

    renderer_path = resolve_native_renderer_path()
    if renderer_path is None:
        pytest.skip("native renderer sidecar not built")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    QApplication.instance() or QApplication([])

    track = _wiping_track()

    def gpu_frame(style: Style, t_ms: int) -> bytes:
        with NativeRendererProcess(renderer_path, response_timeout_s=60.0) as renderer:
            renderer.configure_gpu(
                track, style, width=640, height=360, fps=60, force_warp=True
            )
            event = renderer.render_gpu_frame(t_ms, force_warp=True, frame_index=0)
            reader = SharedFrameRingReader.from_event(event)
            try:
                reader.attach()
                image = reader.read_qimage(event).convertToFormat(
                    QImage.Format.Format_RGBA8888
                )
                bits = image.constBits()
                bits.setsize(image.sizeInBytes())
                return bytes(bits)
            finally:
                reader.close()

    base = gpu_frame(_scanline_style(karaoke_anim="none"), 500)
    brighten = gpu_frame(
        _scanline_style(
            karaoke_anim="scanline",
            scanline_mode="brighten",
            scanline_brightness_pct=70,
        ),
        500,
    )
    zero = gpu_frame(
        _scanline_style(
            karaoke_anim="scanline",
            scanline_mode="brighten",
            scanline_brightness_pct=0,
        ),
        500,
    )
    assert _visible_diff(base, brighten) > 50
    assert _visible_diff(base, zero) == 0


@pytest.mark.skipif(not _IS_WINDOWS, reason="Direct2D GPU backend is Windows-only")
def test_gpu_utopia_scanline_paints_front_band(monkeypatch) -> None:
    pytest.importorskip("PyQt6.QtWidgets")
    from krok_helper.subtitle_render.native.backend import (
        NativeRendererProcess,
        SharedFrameRingReader,
        resolve_native_renderer_path,
    )

    renderer_path = resolve_native_renderer_path()
    if renderer_path is None:
        pytest.skip("native renderer sidecar not built")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    QApplication.instance() or QApplication([])

    track = _wiping_track()

    def gpu_frames(style: Style, t_ms: int) -> bytes:
        with NativeRendererProcess(renderer_path, response_timeout_s=60.0) as renderer:
            renderer.configure_gpu(
                track, style, width=640, height=360, fps=60, force_warp=True
            )
            event = renderer.render_gpu_frame(t_ms, force_warp=True, frame_index=0)
            reader = SharedFrameRingReader.from_event(event)
            try:
                reader.attach()
                image = reader.read_qimage(event).convertToFormat(
                    QImage.Format.Format_RGBA8888
                )
                bits = image.constBits()
                bits.setsize(image.sizeInBytes())
                return bytes(bits)
            finally:
                reader.close()

    base = gpu_frames(_scanline_style(karaoke_anim="utopia"), 500)
    scan = gpu_frames(_scanline_style(karaoke_anim="utopia_scanline"), 500)
    assert _visible_diff(base, scan) > 50


# ---------------------------------------------------------------------------
# 复用角色配色（role 模式）
# ---------------------------------------------------------------------------

from krok_helper.subtitle_render.domain.models import (  # noqa: E402
    remap_scanline_role_reference,
)
from krok_helper.subtitle_render.domain.paint import (  # noqa: E402
    KaraokeColorState,
    KaraokeColors,
    _paint_fill,
)
from krok_helper.subtitle_render.engine.render.elements.horizontal.scanline import (  # noqa: E402
    scanline_params_for_style,
    scanline_role_fill,
)


def _gradient_fill() -> "object":
    return _paint_fill("#FF0040", mode="gradient_horizontal", end="#0040FF")


def _role_scheme_style(**changes) -> Style:
    """带一个「锋面」角色的样式：走字后-主文字为横向渐变。"""

    gradient = _gradient_fill()
    scheme = SubtitleStyleScheme(
        karaoke_colors=KaraokeColors(
            before=KaraokeColorState(text=_paint_fill("#303030")),
            after=KaraokeColorState(text=gradient),
        )
    )
    base = dict(
        karaoke_anim="scanline",
        scanline_mode="role",
        scanline_role_name="锋面",
        custom_style_schemes={
            "锋面": scheme,
            "标题": SubtitleStyleScheme(),
        },
    )
    base.update(changes)
    return _scanline_style(**base)


def test_scanline_role_mode_round_trip() -> None:
    style = _role_scheme_style()
    restored = style_from_dict(style_to_dict(style))
    assert restored.scanline_mode == "role"
    assert restored.scanline_role_name == "锋面"
    # 跟随字体两档同样进出序列化。
    for mode in ("follow_before", "follow_after"):
        follow = style_from_dict(style_to_dict(_scanline_style(scanline_mode=mode)))
        assert follow.scanline_mode == mode
    # 非法模式回落 color；空串/缺失名字归 None。
    assert style_from_dict({"scanline_mode": "wat"}).scanline_mode == "color"
    assert style_from_dict({"scanline_role_name": ""}).scanline_role_name is None
    assert style_from_dict({"scanline_role_name": "  "}).scanline_role_name is None
    # 悬空名字（历史项目/手工 JSON）原样保留：清洗交给渲染端回退与
    # remap 维护链，加载端不静默改数据。
    assert (
        style_from_dict({"scanline_mode": "role", "scanline_role_name": "幽灵"})
        .scanline_role_name
        == "幽灵"
    )
    # 全局默认保留键原样保留。
    assert (
        style_from_dict(
            {"scanline_mode": "role", "scanline_role_name": "__global__"}
        ).scanline_role_name
        == "__global__"
    )


def test_scanline_role_fill_resolves_after_text() -> None:
    fill = scanline_role_fill(_role_scheme_style())
    assert fill is not None
    assert fill.mode == "gradient_horizontal"
    assert fill.start_color == "#FF0040"
    assert fill.end_color == "#0040FF"

    # karaoke_colors 缺失的方案：与 effective_karaoke_colors 的 legacy 回退
    # 同口径，走字后文字取 fill_color 纯色。
    legacy = SubtitleStyleScheme(fill_color="#0A6CFF", karaoke_colors=None)
    legacy_fill = scanline_role_fill(
        _role_scheme_style(
            custom_style_schemes={
                "锋面": legacy,
                "标题": SubtitleStyleScheme(),
            }
        )
    )
    assert legacy_fill is not None
    assert legacy_fill.mode == "solid"
    assert legacy_fill.color == "#0A6CFF"

    # 名字悬空 / 空 / 非 role 模式：无填充，渲染回退单独颜色。
    assert scanline_role_fill(_role_scheme_style(scanline_role_name="不存在")) is None
    assert scanline_role_fill(_role_scheme_style(scanline_role_name=None)) is None
    assert (
        scanline_role_fill(_role_scheme_style(scanline_mode="color", scanline_color="#00FF00"))
        is None
    )
    # 「标题」方案是合法来源：没建过任何角色的项目也能复用它（下拉恒有
    # 该项；karaoke_colors 缺省时按 legacy 口径回落 fill_color 纯色）。
    title_fill = scanline_role_fill(
        _role_scheme_style(
            scanline_role_name="标题",
            custom_style_schemes={
                "标题": SubtitleStyleScheme(),
                "锋面": SubtitleStyleScheme(),
            },
        )
    )
    assert title_fill is not None
    assert title_fill.mode == "solid"
    assert title_fill.color == SubtitleStyleScheme().fill_color
    # 全局默认保留键：取主样式自身的走字后文字填充（legacy 回退 fill_color）。
    global_fill = scanline_role_fill(
        _role_scheme_style(scanline_role_name="__global__")
    )
    assert global_fill is not None
    assert global_fill.mode == "solid"
    assert global_fill.color == Style().fill_color
    # params 悬空回退：mode 仍标 role 但 is_role 为 False，走 solid 单色。
    params = scanline_params_for_style(
        _role_scheme_style(scanline_role_name="不存在", scanline_color="#00FF00")
    )
    assert params.mode == "role"
    assert params.is_role is False


def test_scanline_role_uniform_state_paints_whole_stack() -> None:
    """role 取来源走字后-主文字一份填充，全层（含描边）统一用它重绘。"""

    from krok_helper.subtitle_render.engine.render.elements.horizontal.scanline import (
        _uniform_scanline_state,
    )

    fill = _gradient_fill()
    state = _uniform_scanline_state(fill)
    assert state.text is fill
    assert state.stroke is fill
    assert state.stroke2 is fill
    assert state.shadow is fill


def test_painter_scanline_role_mode_paints_role_fill(qapp) -> None:
    track = _wiping_track()
    base = _frame_bytes(track, _scanline_style(karaoke_anim="none"), 500)
    color = _frame_bytes(track, _scanline_style(karaoke_anim="scanline"), 500)
    role = _frame_bytes(track, _role_scheme_style(), 500)

    # 角色渐变填充的锋面带可见，且与单独颜色（青 #40E0FF）画面不同。
    assert _visible_diff(base, role) > 50
    assert _visible_diff(color, role) > 50

    # 名字悬空：回退单独颜色，与 color 模式逐像素一致。
    dangling = _frame_bytes(track, _role_scheme_style(scanline_role_name="不存在"), 500)
    assert _visible_diff(color, dangling) == 0


def test_remap_scanline_role_reference_follows_rename_and_delete() -> None:
    style = _role_scheme_style()

    # 未引用被改名的角色：不动。
    assert remap_scanline_role_reference(style, {"别人": "新名"}) is None

    # 改名：只改名字，模式保持 role。
    renamed = remap_scanline_role_reference(style, {"锋面": "新锋面"})
    assert renamed is not None
    assert renamed.scanline_mode == "role"
    assert renamed.scanline_role_name == "新锋面"

    # 删除：名字与模式一起回退单独颜色，不留悬空 role 模式。
    deleted = remap_scanline_role_reference(style, {"锋面": None})
    assert deleted is not None
    assert deleted.scanline_mode == "color"
    assert deleted.scanline_role_name is None

    # 名字为空（历史悬空）时删除映射同样清干净模式。
    dangling = _role_scheme_style(scanline_role_name=None)
    assert remap_scanline_role_reference(dangling, {"锋面": None}) is None


def test_remap_appearance_role_references_follow_rename_and_delete() -> None:
    from krok_helper.subtitle_render.domain.models import remap_appearance_role_references

    style = Style(
        volume_appearance_mode="role",
        volume_role_name="锋面",
        lit_appearance_mode="role",
        lit_role_name="标题",
    )

    # 未引用被改名的角色：不动；「标题」不在映射里也不动。
    assert remap_appearance_role_references(style, {"别人": "新名"}) is None

    # 改名：只改音量柱引用的名字，指示灯引用与模式保持 role。
    renamed = remap_appearance_role_references(style, {"锋面": "新锋面"})
    assert renamed is not None
    assert renamed.volume_appearance_mode == "role"
    assert renamed.volume_role_name == "新锋面"
    assert renamed.lit_appearance_mode == "role"
    assert renamed.lit_role_name == "标题"

    # 删除：名字与模式一起回退 auto（渲染端悬空回退本就落到 auto 口径），
    # 不留悬空 role 档。
    deleted = remap_appearance_role_references(style, {"锋面": None})
    assert deleted is not None
    assert deleted.volume_appearance_mode == "auto"
    assert deleted.volume_role_name is None

    # 两字段独立改写：同一次映射同时改名 + 删除互不影响。
    both = remap_appearance_role_references(
        style, {"锋面": "新锋面", "标题": None}
    )
    assert both is not None
    assert both.volume_role_name == "新锋面"
    assert both.lit_appearance_mode == "auto"
    assert both.lit_role_name is None

    # 名字为空（历史悬空）时删除映射不动。
    dangling = Style(volume_appearance_mode="role", volume_role_name=None)
    assert remap_appearance_role_references(dangling, {"锋面": None}) is None


def test_gpu_scanline_role_mode_uses_role_fill(monkeypatch) -> None:
    """GPU 口径：role 模式用角色渐变填充画锋面带，悬空回退单独颜色。"""

    pytest.importorskip("PyQt6.QtWidgets")
    from krok_helper.subtitle_render.native.backend import (
        NativeRendererProcess,
        SharedFrameRingReader,
        resolve_native_renderer_path,
    )

    renderer_path = resolve_native_renderer_path()
    if renderer_path is None:
        pytest.skip("native renderer sidecar not built")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    QApplication.instance() or QApplication([])

    track = _wiping_track()

    def gpu_frames(style: Style, t_ms: int) -> bytes:
        with NativeRendererProcess(renderer_path, response_timeout_s=60.0) as renderer:
            renderer.configure_gpu(
                track, style, width=640, height=360, fps=60, force_warp=True
            )
            event = renderer.render_gpu_frame(t_ms, force_warp=True, frame_index=0)
            reader = SharedFrameRingReader.from_event(event)
            try:
                reader.attach()
                image = reader.read_qimage(event).convertToFormat(
                    QImage.Format.Format_RGBA8888
                )
                bits = image.constBits()
                bits.setsize(image.sizeInBytes())
                return bytes(bits)
            finally:
                reader.close()

    base = gpu_frames(_scanline_style(karaoke_anim="none"), 500)
    color = gpu_frames(_scanline_style(karaoke_anim="scanline"), 500)
    role = gpu_frames(_role_scheme_style(), 500)
    dangling = gpu_frames(_role_scheme_style(scanline_role_name="不存在"), 500)

    assert _visible_diff(base, role) > 50
    assert _visible_diff(color, role) > 50
    # 悬空名字：sidecar 端归一回 color 模式，与单独颜色逐像素一致。
    assert _visible_diff(color, dangling) == 0


def test_painter_scanline_follow_modes_paint_fixed_state(qapp) -> None:
    """跟随字体两档：与底色发光同通路，整带固定用一态且有可见差异。"""

    track = _wiping_track()
    base = _frame_bytes(track, _scanline_style(karaoke_anim="none"), 500)
    follow_before = _frame_bytes(
        track, _scanline_style(karaoke_anim="scanline", scanline_mode="follow_before"), 500
    )
    follow_after = _frame_bytes(
        track, _scanline_style(karaoke_anim="scanline", scanline_mode="follow_after"), 500
    )
    brighten = _frame_bytes(
        track,
        _scanline_style(
            karaoke_anim="scanline",
            scanline_mode="brighten",
            scanline_brightness_pct=0,
        ),
        500,
    )
    # 两档各自可见（锋面前侧被提前染成后色 / 后侧暂回前色）。
    assert _visible_diff(base, follow_before) > 50
    assert _visible_diff(base, follow_after) > 50
    # 两档互相不同（前后态颜色不同），也与提亮档不同。
    assert _visible_diff(follow_before, follow_after) > 50
    assert _visible_diff(follow_before, brighten) > 50
    assert _visible_diff(follow_after, brighten) > 50
    # 底色发光 0 提亮 = 无差异基线（对照：follow 0 提亮仍有差异）。
    assert _visible_diff(base, brighten) == 0
    # 提亮参数对 follow 生效（与底色发光共用）。默认 after 色 #FF5A6F 的
    # HSV 明度已满（提亮数学上无效），这里换暗色 after 才能量化提亮。
    dim_after_colors = KaraokeColors(
        before=KaraokeColorState(text=_paint_fill("#201030")),
        after=KaraokeColorState(text=_paint_fill("#802050")),
    )
    follow_dim = _frame_bytes(
        track,
        _scanline_style(
            karaoke_anim="scanline",
            scanline_mode="follow_after",
            scanline_brightness_pct=0,
            karaoke_colors=dim_after_colors,
        ),
        500,
    )
    follow_dim_lifted = _frame_bytes(
        track,
        _scanline_style(
            karaoke_anim="scanline",
            scanline_mode="follow_after",
            scanline_brightness_pct=80,
            karaoke_colors=dim_after_colors,
        ),
        500,
    )
    assert _visible_diff(follow_dim, follow_dim_lifted) > 50


def test_gpu_scanline_follow_and_global_role_modes(monkeypatch) -> None:
    """GPU 口径：跟随字体固定一态；role 模式 __global__ 取主样式后色。"""

    pytest.importorskip("PyQt6.QtWidgets")
    from krok_helper.subtitle_render.native.backend import (
        NativeRendererProcess,
        SharedFrameRingReader,
        resolve_native_renderer_path,
    )

    renderer_path = resolve_native_renderer_path()
    if renderer_path is None:
        pytest.skip("native renderer sidecar not built")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    QApplication.instance() or QApplication([])

    track = _wiping_track()

    def gpu_frames(style: Style, t_ms: int) -> bytes:
        with NativeRendererProcess(renderer_path, response_timeout_s=60.0) as renderer:
            renderer.configure_gpu(
                track, style, width=640, height=360, fps=60, force_warp=True
            )
            event = renderer.render_gpu_frame(t_ms, force_warp=True, frame_index=0)
            reader = SharedFrameRingReader.from_event(event)
            try:
                reader.attach()
                image = reader.read_qimage(event).convertToFormat(
                    QImage.Format.Format_RGBA8888
                )
                bits = image.constBits()
                bits.setsize(image.sizeInBytes())
                return bytes(bits)
            finally:
                reader.close()

    base = gpu_frames(_scanline_style(karaoke_anim="none"), 500)
    follow_before = gpu_frames(
        _scanline_style(karaoke_anim="scanline", scanline_mode="follow_before"), 500
    )
    follow_after = gpu_frames(
        _scanline_style(karaoke_anim="scanline", scanline_mode="follow_after"), 500
    )
    assert _visible_diff(base, follow_before) > 50
    assert _visible_diff(base, follow_after) > 50
    assert _visible_diff(follow_before, follow_after) > 50

    # role + 全局默认：整带用主样式走字后文字填充。零描边样式下与
    # follow_after 同构（同源配色、同为仅填充层），可逐像素对齐；默认
    # 描边下 follow_after 还重绘带内描边，两者必有差异。
    global_role = gpu_frames(
        _role_scheme_style(
            scanline_role_name="__global__",
            stroke_width_px=0,
            stroke2_enabled=False,
        ),
        500,
    )
    follow_after_plain = gpu_frames(
        _scanline_style(
            karaoke_anim="scanline",
            scanline_mode="follow_after",
            scanline_brightness_pct=0,
            stroke_width_px=0,
            stroke2_enabled=False,
        ),
        500,
    )
    assert _visible_diff(base, global_role) > 50
    assert _visible_diff(follow_after_plain, global_role) == 0


def test_painter_brighten_never_reduces_band_opacity(qapp) -> None:
    """半透明走字后色：提亮/跟随的羽化调整不得把带内压得更透明。

    回归 2026-09：精确 lerp 曾把带内 alpha 拉到重绘层自己的 alpha（如
    #80xxxxxx 的 0x80），而原始画面由多层合成为不透明——带内出现一条
    比周围更透明的「沟」。调整层语义：alpha 恒等于原画面。
    """

    track = _wiping_track()
    base = _scanline_style(
        karaoke_colors=KaraokeColors(
            before=KaraokeColorState(text=_paint_fill("#303030")),
            after=KaraokeColorState(text=_paint_fill("#80307060")),
        ),
    )

    def frame(style: Style) -> np.ndarray:
        return np.frombuffer(
            _frame_bytes(track, style, 500), dtype=np.uint8
        ).reshape(450, 800, 4).astype(int)

    ref = frame(replace(base, karaoke_anim="none"))
    for mode in ("brighten", "follow_after", "follow_before"):
        scan = frame(
            replace(
                base,
                karaoke_anim="scanline",
                scanline_mode=mode,
                scanline_brightness_pct=60,
            )
        )
        drops = int(((scan[:, :, 3] - ref[:, :, 3]) < -10).sum())
        assert drops == 0, f"{mode}: band lost opacity on {drops} pixels"
