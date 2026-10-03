"""gpu_configure_style 差分协议：段等价、行级字段零漂移、端到端帧一致。

改色/改装饰参数的预览更新走「样式差分重放」：Python 只发
style/titles/fx_sprites/逐行样式字段，sidecar 在既有行数据上重放。
本文件把三条正确性底线钉死：
1. 差分载荷的各段与全量 IR 逐字节同源（同一序列化口径）；
2. 行级样式字段（动画/信号旗标/粒子 bursts）与 track_to_ir 零漂移；
3. 真实 sidecar 上「差分重配后渲染的帧」==「全量重配后渲染的帧」。
"""

from __future__ import annotations

import os
import uuid
from dataclasses import replace
from pathlib import Path

import pytest

from krok_helper.subtitle_render.domain.models import Style
from krok_helper.subtitle_render.domain.timing import (
    TimingChar,
    TimingLine,
    TimingTrack,
)
from krok_helper.subtitle_render.engine.render.render_ir import (
    build_render_ir,
    build_style_patch_ir,
)
from krok_helper.subtitle_render.frontend.preview.preview_async import (
    style_patch_base_key,
)

_WIDTH, _HEIGHT, _FPS = 640, 360, 60


def _patch_track() -> TimingTrack:
    return TimingTrack(
        lines=[
            TimingLine(
                chars=[
                    TimingChar("K", 0),
                    TimingChar("a", 400),
                    TimingChar("歌", 800),
                ],
                end_ms=1500,
            ),
            TimingLine(
                chars=[TimingChar("咏", 2000), TimingChar("唱", 600)],
                end_ms=3000,
            ),
        ]
    )


def _extra_track() -> TimingTrack:
    return TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar("副", 0), TimingChar("轨", 500)],
                end_ms=1200,
            )
        ]
    )


def _patch_style(**changes) -> Style:
    style = Style(
        font_family="Arial",
        font_size_px=64,
        font_reference_height=_HEIGHT,
        base_color="#FFFFFF",
        fill_color="#FF2030",
        stroke_color="#101010",
        stroke_width_px=3,
        decoration_kind="shadow",
        shadow_color="#00FF40",
        line_y_position="center",
        line_horizontal_layout="center",
        line_lead_in_ms=0,
        line_tail_ms=0,
        fx_particle_count=8,
    )
    return replace(style, **changes)


def test_style_patch_ir_sections_match_full_ir(qapp):
    """差分载荷四段与全量 IR 同源：同一套序列化，不允许口径分叉。"""
    track = _patch_track()
    style = _patch_style()

    full = build_render_ir(
        track,
        style,
        width=_WIDTH,
        height=_HEIGHT,
        fps=_FPS,
        extra_tracks=[_extra_track()],
        duration_ms=4000,
        relayout_scope=None,
    )
    patch = build_style_patch_ir(
        track,
        style,
        width=_WIDTH,
        height=_HEIGHT,
        fps=_FPS,
        extra_tracks=[_extra_track()],
        duration_ms=4000,
    )

    assert patch["screen"] == full["screen"]
    assert patch["style"] == full["style"]
    assert patch["titles"] == full["titles"]
    assert patch["fx_sprites"] == full["fx_sprites"]


_LINE_STYLE_FIELDS = (
    "signal_head",
    "signal_band_join",
    "entry_anim",
    "entry_duration_ms",
    "exit_anim",
    "exit_duration_ms",
    "karaoke_anim",
    "scanline",
    "zoom_pulse",
    "stroke_flash",
    "fx_bursts",
)


def test_lines_style_matches_track_to_ir_line_fields(qapp):
    """行级样式字段与 track_to_ir 的行字典逐字段相等（防两处实现漂移）。

    覆盖主轨（source_index=0）与副轨（source_index=1..N）的行号对齐。
    """
    track = _patch_track()
    extra = _extra_track()
    style = _patch_style()

    full = build_render_ir(
        track,
        style,
        width=_WIDTH,
        height=_HEIGHT,
        fps=_FPS,
        extra_tracks=[extra],
        duration_ms=4000,
        relayout_scope=None,
    )
    patch_entries = {
        (entry["source_index"], entry["source_line_index"]): entry
        for entry in build_style_patch_ir(
            track,
            style,
            width=_WIDTH,
            height=_HEIGHT,
            fps=_FPS,
            extra_tracks=[extra],
            duration_ms=4000,
        )["lines_style"]
    }

    sources = [(0, full["track"]["lines"])]
    sources.extend(
        (index + 1, source["lines"])
        for index, source in enumerate(full["extra_tracks"])
    )
    assert len(patch_entries) == sum(len(lines) for _, lines in sources)
    for source_index, lines in sources:
        for line_index, line_ir in enumerate(lines):
            entry = patch_entries[(source_index, line_index)]
            for field in _LINE_STYLE_FIELDS:
                assert entry[field] == line_ir[field], (
                    f"source={source_index} line={line_index} field={field}"
                )


def test_style_patch_key_gates_pure_style_edits():
    """闸门 key：纯改色不变（可差分），改字号/改轨道/改画面必变（全量）。"""
    track = _patch_track()
    style = _patch_style()
    extra = [_extra_track()]

    base = style_patch_base_key(
        track, style, extra, width=_WIDTH, height=_HEIGHT, fps=_FPS, dpr=1.0
    )
    # 纯改色（含方案矩阵颜色）：key 不变。
    recolored = style_patch_base_key(
        track,
        _patch_style(fill_color="#3366FF"),
        extra,
        width=_WIDTH,
        height=_HEIGHT,
        fps=_FPS,
        dpr=1.0,
    )
    assert recolored == base
    # 布局输入变化：key 必变。
    assert style_patch_base_key(
        track,
        _patch_style(font_size_px=80),
        extra,
        width=_WIDTH,
        height=_HEIGHT,
        fps=_FPS,
        dpr=1.0,
    ) != base
    # 轨道内容变化：key 必变。
    track_edited = _patch_track()
    track_edited.lines[0].chars.append(TimingChar("新", 1200))
    assert style_patch_base_key(
        track_edited, style, extra, width=_WIDTH, height=_HEIGHT, fps=_FPS, dpr=1.0
    ) != base
    # 副轨内容变化：key 必变。
    extra_edited = [_extra_track()]
    extra_edited[0].lines[0].end_ms = 1500
    assert style_patch_base_key(
        track, style, extra_edited, width=_WIDTH, height=_HEIGHT, fps=_FPS, dpr=1.0
    ) != base
    # 画面参数变化：key 必变。
    assert style_patch_base_key(
        track, style, extra, width=_WIDTH + 1, height=_HEIGHT, fps=_FPS, dpr=1.0
    ) != base


@pytest.mark.skipif(
    os.name != "nt", reason="Native GPU renderer is Windows-only"
)
def test_configure_style_gpu_frame_matches_full_configure(qapp):
    """端到端金标准：差分重配的帧 == 全量重配的帧（checksum 相等）。"""
    from krok_helper.subtitle_render.native.backend import (
        NativeRendererError,
        NativeRendererProcess,
        resolve_native_renderer_path,
    )

    renderer_path = resolve_native_renderer_path(root=Path.cwd())
    if renderer_path is None:
        pytest.skip("native subtitle renderer executable is not built")

    track = _patch_track()
    style_a = _patch_style()
    style_b = _patch_style(fill_color="#3366FF", fx_particle_count=12)
    shm_key = f"test-gpu-style-patch-{uuid.uuid4().hex}"

    def render_checksum(renderer: NativeRendererProcess, frame_index: int) -> str:
        event = renderer.render_gpu_frame(
            500,
            generation=0,
            frame_index=frame_index,
            shm_key=shm_key,
            include_checksum=True,
        )
        return str(event["checksum"])

    with NativeRendererProcess(response_timeout_s=30.0) as renderer:
        # 尚未全量 configure：差分命令必须报协议错误（调用方据此回落全量）。
        with pytest.raises(NativeRendererError):
            renderer.configure_style_gpu(
                build_style_patch_ir(
                    track,
                    style_a,
                    width=_WIDTH,
                    height=_HEIGHT,
                    fps=_FPS,
                    duration_ms=4000,
                )
            )

        renderer.configure_gpu(
            track,
            style_a,
            width=_WIDTH,
            height=_HEIGHT,
            fps=_FPS,
            duration_ms=4000,
            prewarm_t_ms=500,
            worker_count=1,
            defer_followers=True,
            defer_realizations_until_first_frame=True,
        )
        checksum_full_a = render_checksum(renderer, 0)

        # 差分重配（改色 + 改粒子数量 → style 段与行级 bursts 同时变化）。
        renderer.configure_style_gpu(
            build_style_patch_ir(
                track,
                style_b,
                width=_WIDTH,
                height=_HEIGHT,
                fps=_FPS,
                duration_ms=4000,
            ),
            prewarm_t_ms=500,
            worker_count=1,
        )
        checksum_patch_b = render_checksum(renderer, 1)

        # 同一新样式的全量重配：帧必须与差分逐字节一致。
        renderer.configure_gpu(
            track,
            style_b,
            width=_WIDTH,
            height=_HEIGHT,
            fps=_FPS,
            duration_ms=4000,
            prewarm_t_ms=500,
            worker_count=1,
            defer_followers=True,
            defer_realizations_until_first_frame=True,
        )
        checksum_full_b = render_checksum(renderer, 2)

        assert checksum_patch_b == checksum_full_b
        # 改色确实改变了画面（防两帧同图导致假等价）。
        assert checksum_full_b != checksum_full_a


# ── 表化（schema 3 开发期）等价测试：展开 == 内联 ──────────────────────


def _expand_bursts(bursts, colors, paints):
    out = []
    for burst in bursts:
        expanded = {
            key: value
            for key, value in burst.items()
            if key not in ("color_id", "paint_id", "char_color_ids")
        }
        expanded["color"] = colors[burst["color_id"]]
        if "paint_id" in burst:
            expanded["paint"] = paints[burst["paint_id"]]
        if "char_color_ids" in burst:
            expanded["char_colors"] = [
                colors[index] for index in burst["char_color_ids"]
            ]
        out.append(expanded)
    return out


def test_fx_tables_expand_to_inline_bursts(qapp):
    """表化 bursts 展开后与无表内联构建逐字段相等（幂等金标准）。"""
    from krok_helper.subtitle_render.engine.render.adapters.layout_plan import (
        build_track_layout_plan,
    )
    from krok_helper.subtitle_render.native.protocol import (
        VectorGlyphTable,
        track_to_ir,
    )

    track = _patch_track()
    style = _patch_style(
        entry_anim="sparkle",
        exit_anim="note",
        sing_fx="note",
        fx_particle_color_mode="follow_after",
        fx_apply_to_entry_exit=True,
    )

    full = build_render_ir(
        track, style, width=_WIDTH, height=_HEIGHT, fps=_FPS,
        duration_ms=4000, relayout_scope=None,
    )
    plan = build_track_layout_plan(
        track, style, logical_w=_WIDTH, logical_h=_HEIGHT
    )
    inline = track_to_ir(
        track, style, layout_plan=plan, glyph_table=VectorGlyphTable()
    )

    colors = full["fx_color_table"]
    paints = full["fx_paint_table"]
    assert colors and paints
    assert len(full["track"]["lines"]) == len(inline["lines"])
    for tabulated_line, inline_line in zip(
        full["track"]["lines"], inline["lines"], strict=True
    ):
        expanded = _expand_bursts(
            tabulated_line["fx_bursts"], colors, paints
        )
        assert expanded == inline_line["fx_bursts"]


def test_line_layout_table_expands_to_inline_layout(qapp):
    """行布局快照表展开后与无表内联构建相等。"""
    from krok_helper.subtitle_render.engine.render.adapters.layout_plan import (
        build_track_layout_plan,
    )
    from krok_helper.subtitle_render.native.protocol import (
        VectorGlyphTable,
        track_to_ir,
    )

    track = _patch_track()
    style = _patch_style()

    full = build_render_ir(
        track, style, width=_WIDTH, height=_HEIGHT, fps=_FPS,
        duration_ms=4000, relayout_scope=None,
    )
    plan = build_track_layout_plan(
        track, style, logical_w=_WIDTH, logical_h=_HEIGHT
    )
    inline = track_to_ir(
        track, style, layout_plan=plan, glyph_table=VectorGlyphTable()
    )

    table = full["line_layout_table"]
    assert table  # 布局快照表在场
    for tabulated_line, inline_line in zip(
        full["track"]["lines"], inline["lines"], strict=True
    ):
        assert table[tabulated_line["layout_id"]] == inline_line["layout"]


def test_char_ir_omits_default_fields_keeps_explicit(qapp):
    """字符级「缺省即空」：默认值整个键不发，显式值保留。"""
    from krok_helper.subtitle_render.native.protocol import timing_char_to_ir

    plain = timing_char_to_ir(TimingChar("歌", 100))
    assert "explicit_start" not in plain
    assert "explicit_end" not in plain
    assert "pause_release_ms" not in plain
    assert "role_label" not in plain
    assert "bitmap_guide" not in plain

    full = timing_char_to_ir(
        TimingChar(
            "歌", 100, explicit_start=True, explicit_end=True,
            pause_release_ms=450, role_label="A",
        )
    )
    assert full["explicit_start"] is True
    assert full["explicit_end"] is True
    assert full["pause_release_ms"] == 450
    assert full["role_label"] == "A"


def test_vector_glyphs_hash_gate_omits_unchanged_table():
    """哈希门：同一 sidecar 连接内轨道未变时第二次 configure 不带轮廓表。"""
    from krok_helper.subtitle_render.domain.timing import GuideSymbol
    from krok_helper.subtitle_render.native.backend import NativeRendererProcess

    symbol = GuideSymbol(
        path_commands=(("M", 0.0, 0.0), ("L", 100.0, 100.0), ("Z",)),
        units_per_em=1000,
        advance_width=1000.0,
    )
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar("歌", 0)],
                end_ms=1000,
                guide_symbol=symbol,
            )
        ]
    )
    renderer = NativeRendererProcess()
    payloads: list[dict] = []
    # 不启动真进程：spy 直接记载荷并伪造 configured 响应（哈希门是纯
    # Python 状态机，sidecar 保留行为由真实 e2e 覆盖）。
    responses = iter([
        {"ok": True, "event": "configured"},
        {"ok": True, "event": "configured"},
    ])
    renderer._send = payloads.append
    renderer._read_until_event = lambda event, timeout_s=None: next(responses)

    style = _patch_style()
    kwargs = {"width": 640, "height": 360, "fps": 60}
    renderer.configure(track, style, **kwargs)
    renderer.configure(track, style, **kwargs)

    irs = [p["ir"] for p in payloads if p.get("cmd") == "configure"]
    assert len(irs) == 2
    assert "vector_glyphs" in irs[0]
    assert "vector_glyphs_hash" in irs[0]
    # 第二次：表省发，哈希仍在；失配时 sidecar 拒绝、发送端回落整表。
    assert "vector_glyphs" not in irs[1]
    assert irs[1]["vector_glyphs_hash"] == irs[0]["vector_glyphs_hash"]


def test_titles_patch_omits_lines_style(qapp):
    """titles scope 差分：行数据不变时省掉 lines_style（载荷与规划成本）。"""
    from krok_helper.subtitle_render.engine.render.render_ir import (
        build_style_patch_ir,
    )

    track = _patch_track()
    style = _patch_style()

    full = build_style_patch_ir(
        track, style, width=_WIDTH, height=_HEIGHT, fps=_FPS,
        duration_ms=4000, include_lines_style=False,
    )
    assert "lines_style" not in full
    # 去重表随 lines_style 一起省发（表只服务于 bursts 引用展开）。
    assert full["fx_color_table"] == []


def test_fx_render_only_params_stay_patch_eligible():
    """粒子物理参数/装饰档是渲染专属字段：不改变差分闸门的布局签名。"""
    from krok_helper.subtitle_render.engine.value_signature import (
        lyric_layout_style_signature,
    )
    from krok_helper.subtitle_render.frontend.preview.preview_async import (
        style_patch_base_key,
    )

    track = _patch_track()
    style = _patch_style()
    base = style_patch_base_key(
        track, style, [], width=_WIDTH, height=_HEIGHT, fps=_FPS, dpr=1.0
    )
    tweaked = style_patch_base_key(
        track,
        _patch_style(fx_particle_count=24, fx_particle_size_em=0.6,
                     sing_fx="twinkle", entry_fx="glow"),
        [],
        width=_WIDTH, height=_HEIGHT, fps=_FPS, dpr=1.0,
    )
    assert tweaked == base  # 闸门 key 稳定 → 差分可用
    # 布局签名同样不受 fx 参数影响（计划缓存命中）。
    assert lyric_layout_style_signature(
        _patch_style(fx_particle_count=24)
    ) == lyric_layout_style_signature(style)
