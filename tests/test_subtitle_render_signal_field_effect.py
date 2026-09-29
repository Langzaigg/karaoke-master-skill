"""音量柱/指示灯全部样式字段的生效性审计（翻转测试）。

对每个 lit_/volume_/signals_ 字段改值后逐帧比对，任何后端上画面完全
不变的字段即为「实际不生效」。探针时间戳与基准样式的相位强耦合
（亮闪/闪烁爬坡/半填充/倒计时中段/保持态），改基准样式时需同步校准。
"""
import os

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows-only")

from krok_helper.subtitle_render.domain.models import Style
from krok_helper.subtitle_render.domain.timing import (
    TimingChar,
    TimingLine,
    TimingTrack,
)

TIMESTAMPS = (880, 980, 2_600, 3_000, 4_400)

VARIANTS = {
    "lit_enabled": False,
    "volume_enabled": False,
    # 不翻成 "image"：基准样式没有图片素材，图片模式回退圆形会与基准
    # circle 帧相同（图片模式由专属测试覆盖）。
    "lit_style": "square",
    "lit_number": 6,
    "lit_size": 20,
    "lit_offset_x": -30,
    "lit_offset_y": 6,
    "lit_tracking": 14,
    "lit_fill_color": "#00FF00",
    "lit_stroke_color": "#FF8800",
    "lit_stroke_width": 6,
    "lit_stroke_soften": 5,
    "lit_opacity_pct": 45,
    "lit_edge_brightness_pct": 15,
    "lit_shadow": False,
    "lit_time_offset_ms": 600,
    "lit_waiting_time_ms": 1_500,
    "lit_transition_mode": "fade",
    "lit_transition_ratio_pct": 30,
    "lit_transition_angle_deg": -70,
    "lit_transition_distance": 60,
    "signals_duration_ms": 2_500,
    "volume_duration_ms": 2_500,
    "volume_waiting_time_ms": 1_200,
    "volume_time_offset_ms": 700,
    "volume_stroke_width": 7,
    "volume_opacity_pct": 35,
    "volume_size": 24,
    "volume_offset_x": -40,
    "volume_offset_y": 10,
    "volume_column_width": 20,
    "volume_column_count": 8,
    "volume_column_spacing": 9,
    "volume_align": 0,
    "volume_ratio": 1.2,
    "volume_fill_color": "#FF00FF",
    "volume_stroke_color": "#00FFFF",
    "volume_overlay_fill_color": "#FF0000",
    "volume_overlay_stroke_color": "#000080",
    "volume_flash_times": 6,
    "volume_flash_duration_ratio": 0.2,
    "volume_transition_ratio_pct": 90,
}


def _base_style() -> Style:
    return Style(
        font_size_px=60,
        stroke_width_px=0,
        stroke2_enabled=False,
        decoration_kind="none",
        dual_line_layout=False,
        line_horizontal_layout="center",
        line_lead_in_ms=500,
        line_tail_ms=500,
        entry_anim="none",
        lit_enabled=True,
        lit_style="circle",
        lit_number=4,
        lit_size=34,
        lit_offset_x=-6,
        lit_offset_y=-12,
        lit_tracking=5,
        lit_fill_color="#2040FF",
        lit_stroke_color="#FFFFFF",
        lit_stroke_width=3,
        lit_stroke_soften=2,
        lit_opacity_pct=85,
        lit_edge_brightness_pct=70,
        lit_shadow=True,
        lit_time_offset_ms=0,
        lit_waiting_time_ms=500,
        lit_transition_mode="slide",
        lit_transition_ratio_pct=67,
        lit_transition_angle_deg=35,
        lit_transition_distance=28,
        signals_duration_ms=4_000,
        volume_enabled=True,
        # 翻转审计要覆盖 volume_* 独立字段，须钉在 custom 档；出厂默认 auto
        # 下这些字段由主文字字号/配色推导，改值不落画面。
        volume_appearance_mode="custom",
        volume_duration_ms=4_000,
        volume_waiting_time_ms=0,
        volume_time_offset_ms=0,
        volume_stroke_width=3,
        volume_opacity_pct=75,
        volume_size=44,
        volume_offset_x=8,
        volume_offset_y=-6,
        volume_column_width=12,
        volume_column_count=5,
        volume_column_spacing=3,
        volume_align=2,
        volume_ratio=2.5,
        volume_fill_color="#FFFFFF",
        volume_stroke_color="#0000FF",
        volume_overlay_fill_color="#0000FF",
        volume_overlay_stroke_color="#FFFFFF",
        volume_flash_times=3,
        volume_flash_duration_ratio=0.5,
        volume_transition_ratio_pct=40,
    )


def _track() -> TimingTrack:
    return TimingTrack(
        lines=[TimingLine(chars=[TimingChar("Signal", 4_000)], end_ms=5_000)]
    )


def _cpu_frames(style: Style) -> list[bytes]:
    import sys

    sys.path.insert(0, os.path.dirname(__file__))
    from test_subtitle_render_gpu_backend import _render_painter_oracle

    return [
        _render_painter_oracle(style, t_ms=t, track=_track())
        for t in TIMESTAMPS
    ]


def test_flip_audit_cpu(qapp, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    baseline = _cpu_frames(_base_style())
    inert = []
    for field, value in VARIANTS.items():
        variant = _base_style()
        object.__setattr__(variant, field, value)
        frames = _cpu_frames(variant)
        if frames == baseline:
            inert.append(field)
    print("\nCPU inert fields:", inert or "NONE")
    assert not inert


def test_flip_audit_gpu(qapp, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    import sys

    sys.path.insert(0, os.path.dirname(__file__))
    from test_subtitle_render_gpu_backend import (
        NativeRendererProcess,
        _render_g1_frames,
        _renderer_path,
    )

    inert = []
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        _, baseline = _render_g1_frames(
            renderer, _base_style(), TIMESTAMPS, force_warp=True, track=_track()
        )
        for field, value in VARIANTS.items():
            variant = _base_style()
            object.__setattr__(variant, field, value)
            _, frames = _render_g1_frames(
                renderer, variant, TIMESTAMPS, force_warp=True, track=_track()
            )
            if frames == baseline:
                inert.append(field)
    print("\nGPU inert fields:", inert or "NONE")
    assert not inert
