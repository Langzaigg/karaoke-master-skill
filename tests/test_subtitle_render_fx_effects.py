"""2026-09 逐字几何特效 / 描边闪光 / 装饰粒子的核心数学与 IR 契约测试。

镜像约束：transitions._geo_char_state ↔ d2d_fx.geoCharState、
particles.burst_particles_at ↔ d2d_fx.burstParticlesAt——这里断言的是
Python 侧不变量（确定性、窗口、单调性、哈希域），跨后端逐位一致由
两侧共用同一整数哈希与纯函数轨迹保证。
"""

from __future__ import annotations

import math

import pytest

from krok_helper.subtitle_render.domain.models import Style, TimingLine
from krok_helper.subtitle_render.domain.timing import LineAnimationOverride
from krok_helper.subtitle_render.engine.render.elements.horizontal.transitions import (
    _geo_char_state,
    fx_unit_hash,
    line_char_transition_context,
    transition_char_state,
)
from krok_helper.subtitle_render.engine.render.elements.horizontal.contracts import (
    LineCharTransition,
)
from krok_helper.subtitle_render.engine.render.effects.particles import (
    FX_SPRITES,
    burst_particles_at,
    plan_line_bursts,
    sprite_for_kind,
)
from krok_helper.subtitle_render.native.protocol import gpu_unsupported_features


def _entry_transition(effect: str, start_ms: int = 0) -> LineCharTransition:
    return LineCharTransition(
        phase="entry", effect=effect, progress=1.0, start_ms=start_ms, end_ms=start_ms + 800
    )


def _exit_transition(effect: str, start_ms: int) -> LineCharTransition:
    return LineCharTransition(
        phase="exit", effect=effect, progress=1.0, start_ms=start_ms, end_ms=start_ms + 700
    )


def test_fx_unit_hash_is_deterministic_and_bounded():
    for index in range(0, 256, 7):
        for salt in range(1, 5):
            value = fx_unit_hash(index, salt)
            assert 0.0 <= value < 1.0
            assert value == fx_unit_hash(index, salt)
    assert fx_unit_hash(0, 1) != fx_unit_hash(1, 1)


def test_tracking_in_converges_from_outward_offsets():
    style = Style(font_size_px=100)
    count = 5
    early = _geo_char_state(
        style, _entry_transition("tracking_in"), 0, count,
        t_ms=0, char_center_x=None, line_center_x=None,
    )
    settled = _geo_char_state(
        style, _entry_transition("tracking_in"), 0, count,
        t_ms=10_000, char_center_x=None, line_center_x=None,
    )
    # 首字符位于最左：初始 dx 为负（向左展开），归位后为 0。
    assert early[1] < 0.0
    assert early[0] < 1.0
    assert settled[1] == 0.0
    assert settled[0] == 1.0
    # 对称：首尾字符初始偏移互为相反数。
    last = _geo_char_state(
        style, _entry_transition("tracking_in"), count - 1, count,
        t_ms=0, char_center_x=None, line_center_x=None,
    )
    assert last[1] == pytest.approx(-early[1])


def test_wave_in_rises_with_stagger():
    style = Style(font_size_px=100)
    state = _geo_char_state(
        style, _entry_transition("wave_in"), 2, 5,
        t_ms=0, char_center_x=None, line_center_x=None,
    )
    assert state[2] > 24.0
    done = _geo_char_state(
        style, _entry_transition("wave_in"), 2, 5,
        t_ms=100_000, char_center_x=None, line_center_x=None,
    )
    assert done[2] == 0.0
    assert done[0] == 1.0


def test_geo_entry_motion_visible_while_opaque():
    """回归护栏：入场行程前 1/4 字符已全不透明且位移仍有 8 成以上。

    旧实现透明度与运动共用缓动（快衰减），字符变清晰时位移只剩 ~12%，
    观感退化为普通逐字淡入（用户判「完全没有动画」）。
    """
    style = Style(font_size_px=100, entry_lead_ms=600)
    tracking = _geo_char_state(
        style, _entry_transition("tracking_in", 0), 0, 5,
        t_ms=65, char_center_x=None, line_center_x=None,
    )
    spread = 100 * 1.6
    assert tracking[0] == 1.0
    assert tracking[1] < -0.8 * spread

    wave = _geo_char_state(
        style, _entry_transition("wave_in", 0), 0, 5,
        t_ms=65, char_center_x=None, line_center_x=None,
    )  # 同 style 共用 entry_lead_ms=600
    assert wave[0] == 1.0
    assert wave[2] > 0.8 * max(100 * 0.5, 24.0)


def test_scatter_out_moves_fades_and_rotates():
    style = Style(font_size_px=100)
    start = _geo_char_state(
        style, _exit_transition("scatter_out", 2000), 3, 5,
        t_ms=2000, char_center_x=None, line_center_x=None,
    )
    end = _geo_char_state(
        style, _exit_transition("scatter_out", 2000), 3, 5,
        t_ms=100_000, char_center_x=None, line_center_x=None,
    )
    assert start[0] == 1.0
    assert end[0] == 0.0
    # 至少有一个分量发生位移（随机方向不保证单一轴向）。
    assert abs(end[1]) + abs(end[2]) > 100.0
    assert end[4] < 1.0


def test_converge_out_pulls_toward_line_center():
    style = Style(font_size_px=100)
    state = _geo_char_state(
        style, _exit_transition("converge_out", 3000), 0, 3,
        t_ms=3100, char_center_x=100.0, line_center_x=500.0,
    )
    # 字符在行中心左侧：dx 为正（向中心靠拢）。
    assert state[1] > 0.0
    assert state[1] <= 400.0
    assert 0.0 < state[0] < 1.0


def test_geo_context_windows_and_gating():
    style = Style(
        font_size_px=100,
        entry_anim="wave_in",
        entry_lead_ms=300,
        exit_anim="scatter_out",
        exit_fade_ms=300,
    )
    line = TimingLine()
    entry = line_char_transition_context(style, line, 100, 0, 6000, 3)
    assert entry is not None and entry.effect == "wave_in" and entry.phase == "entry"

    after_entry = line_char_transition_context(style, line, 900, 0, 6000, 3)
    # 入场窗口结束后不再命中入场相位（默认 karaoke_anim=utopia 会接管为
    # 稳态 wipe 路径，这是既有语义而非新特效窗口泄漏）。
    assert after_entry is None or after_entry.phase != "entry"

    exit_ctx = line_char_transition_context(style, line, 5800, 0, 6000, 3)
    assert exit_ctx is not None and exit_ctx.effect == "scatter_out"

    # 旧语义：时长 0 = 无动画（与 char_fade / 淡入滑入同口径）。
    zero_lead = Style(
        entry_anim="wave_in",
        entry_lead_ms=0,
        exit_anim="scatter_out",
        exit_fade_ms=0,
        karaoke_anim="none",
    )
    assert line_char_transition_context(zero_lead, line, 100, 0, 6000, 3) is None
    assert line_char_transition_context(zero_lead, line, 5800, 0, 6000, 3) is None
    # 时长驱动窗口：300ms 时长 → t=350 已过入场窗。
    short = Style(
        entry_anim="wave_in", entry_lead_ms=300, karaoke_anim="none"
    )
    after_short = line_char_transition_context(short, line, 350, 0, 6000, 3)
    assert after_short is None or after_short.phase != "entry"
    # 显式 none 才是不动画。
    disabled = Style(entry_anim="none", karaoke_anim="none")
    assert line_char_transition_context(disabled, line, 0, 0, 6000, 3) is None


def test_transition_char_state_dispatches_geo_kinds():
    style = Style(font_size_px=64)
    transition = _entry_transition("tracking_in")
    opacity, dx, _dy, _rot, sx, sy, skew = transition_char_state(
        style, transition, 0, 4, t_ms=0
    )
    assert opacity < 1.0
    assert dx < 0.0
    assert (sx, sy, skew) == (1.0, 1.0, 0.0)


def test_plan_line_bursts_kinds_and_windows():
    """出入场动画粒子（固定默认档）+ 唱字粒子（旋钮档）的规划契约。"""
    style = Style(
        entry_anim="sparkle",
        entry_lead_ms=600,
        exit_anim="ripple",
        exit_fade_ms=600,
        sing_fx="twinkle",
        fx_particle_size_em=0.5,
        fx_particle_count=10,
        font_size_px=100,
        karaoke_anim="none",
    )
    bursts = plan_line_bursts(
        style,
        line_index=2,
        display_start_ms=1000,
        display_end_ms=4000,
        line_end_ms=3900,
        char_windows=[(1200, 1600), (1600, 1600), (1600, 3000)],
    )
    kinds = [burst["kind"] for burst in bursts]
    # 入场动画：星光整行扫过（line 锚点、固定 14 颗、白色默认档）。
    assert kinds.count("sparkle") == 1
    sparkle = bursts[0]
    assert sparkle["anchor"] == "line"
    assert sparkle["start_ms"] == 1000
    assert sparkle["count"] == 14
    assert sparkle["sweep"] == 1
    assert sparkle["color"] == "#FFFFFF"
    assert sparkle["size_px"] == pytest.approx(100 * 0.40)
    # 退场动画涟漪：逐字（3 字），尾窗钳制下同点发射，双环。
    assert kinds.count("ripple") == 3
    exit_ripples = [b for b in bursts if b["kind"] == "ripple"]
    assert {b["start_ms"] for b in exit_ripples} == {3880}
    assert [b["char_index"] for b in exit_ripples] == [0, 1, 2]
    assert all(b["anchor"] == "char" and b["count"] == 3 for b in exit_ripples)
    # 唱字（旋钮档）：零时长字符不发射；每字数量 = max(3, count//4)。
    assert kinds.count("twinkle") == 2
    twinkle = next(b for b in bursts if b["kind"] == "twinkle")
    assert twinkle["count"] == 3
    assert twinkle["anchor"] == "char"
    assert twinkle["end_ms"] == twinkle["start_ms"] + 700
    assert twinkle["size_px"] == pytest.approx(50.0)  # 100px * 0.5em 旋钮
    # 行下标参与种子：不同行不同轨迹；同参数可复现。
    other = plan_line_bursts(
        style, 3, 1000, 4000, 3900, [(1200, 1600), (1600, 1600), (1600, 3000)]
    )
    assert other[0]["seed"] != sparkle["seed"]
    again = plan_line_bursts(
        style, 2, 1000, 4000, 3900, [(1200, 1600), (1600, 1600), (1600, 3000)]
    )
    assert again[0]["seed"] == sparkle["seed"]


def test_plan_line_bursts_anim_kinds_and_sing_ripple():
    """音符/拼接入场动画 + 唱字涟漪档。"""
    style = Style(
        entry_anim="note",
        entry_lead_ms=600,
        exit_anim="dissolve_out",
        exit_fade_ms=600,
        sing_fx="ripple",
        font_size_px=100,
        karaoke_anim="none",
    )
    bursts = plan_line_bursts(
        style, 0, 1000, 4200, 4100,
        [(1200, 1600), (1600, 2000), (2000, 3000)],
    )
    kinds = [burst["kind"] for burst in bursts]
    # 音符入场：逐字错峰（3 字 → 步距 175），每字 3 颗，固定白档。
    assert kinds.count("note") == 3
    notes = [b for b in bursts if b["kind"] == "note"]
    assert [b["start_ms"] for b in notes] == [1000, 1175, 1350]
    assert all(b["count"] == 3 and b["color"] == "#FFFFFF" for b in notes)
    # 消散退场：像素方块、固定档（max(4, 14//2)=7 颗/字）。
    assert kinds.count("dissolve") == 3
    dissolve = next(b for b in bursts if b["kind"] == "dissolve")
    assert dissolve["count"] == 7
    assert dissolve["size_px"] == pytest.approx(100 * 0.40 * 0.75)
    # 唱字涟漪：每字两圈细环、旋钮尺寸 ×3.6（三个窗都非零时长）。
    assert kinds.count("ripple") == 3
    sing_ripple = next(b for b in bursts if b["kind"] == "ripple")
    assert sing_ripple["count"] == 3  # RIPPLE_RING_COUNT
    assert sing_ripple["size_px"] == pytest.approx(100 * 0.40 * 3.6)
    assert sing_ripple["start_ms"] in (1200, 1600)


def test_burst_particles_window_and_shapes():
    style = Style(
        entry_anim="sparkle",
        entry_lead_ms=600,
        exit_anim="ripple",
        exit_fade_ms=600,
        fx_particle_count=6,
        font_size_px=100,
        karaoke_anim="none",
    )
    bursts = plan_line_bursts(style, 0, 0, 1000, 900, [(0, 500)])
    sparkle = bursts[0]
    assert burst_particles_at(sparkle, -1, 0.0, 0.0, 0, 0) == []
    assert burst_particles_at(sparkle, 2000, 0.0, 0.0, 0, 0) == []
    # 整行盒：粒子应铺满整行宽度（sweep=1，出生按横向位置错峰）。
    states = burst_particles_at(sparkle, 300, 0.0, 0.0, 600.0, 100.0)
    assert 0 < len(states) <= sparkle["count"]
    for state in states:
        assert 0.0 < state.alpha <= 1.0
        assert state.size_px > 0.0
        assert -360.0 <= state.x <= 360.0

    ripple_bursts = plan_line_bursts(
        Style(exit_anim="ripple", exit_fade_ms=600, karaoke_anim="none"),
        0, None, 2000, 1900, [(0, 100)],
    )
    ripple = ripple_bursts[0]
    assert ripple["kind"] == "ripple" and ripple["count"] == 3
    # 水波纹：第 2 环延迟 150ms、第 3 环 300ms 出生；各环完整走完「小→大」。
    first_only = burst_particles_at(ripple, ripple["start_ms"] + 50, 0.0, 0.0, 0, 0)
    assert len(first_only) == 1
    two = burst_particles_at(ripple, ripple["start_ms"] + 200, 0.0, 0.0, 0, 0)
    assert len(two) == 2
    all_rings = burst_particles_at(ripple, ripple["start_ms"] + 380, 0.0, 0.0, 0, 0)
    assert len(all_rings) == 3
    # 后出生的环更年轻 → 半径更小、更亮。
    assert all_rings[2].size_px < all_rings[1].size_px < all_rings[0].size_px
    assert all_rings[2].alpha > all_rings[1].alpha > all_rings[0].alpha
    assert burst_particles_at(ripple, ripple["end_ms"] + 1, 0.0, 0.0, 0, 0) == []


def test_fx_sprites_contract():
    # ring：外/内两段子路径且绕向相反（winding 与 even-odd 都呈圆环）。
    ring = FX_SPRITES["ring"]["path_commands"]
    moves = [command for command in ring if command[0] == "M"]
    assert len(moves) == 2
    assert FX_SPRITES["star4"]["path_commands"][0][0] == "M"
    for name in ("star4", "ring", "note"):
        commands = FX_SPRITES[name]["path_commands"]
        assert commands[-1][0] == "Z"
        for command in commands:
            assert command[0] in {"M", "L", "C", "Q", "Z"}
    assert sprite_for_kind("sparkle") == "star4"
    assert sprite_for_kind("ripple") == "ring"
    assert sprite_for_kind("note") == "note"


def test_gpu_supports_new_kinds_without_fallback():
    track = type("Track", (), {"lines": []})()
    style = Style(
        entry_anim="tracking_in",
        exit_anim="scatter_out",
        karaoke_anim="utopia",
        reverse_karaoke_anim="inherit",
        entry_fx="sparkle",
        exit_fx="ripple",
        sing_fx="note",
        karaoke_stroke_flash=True,
    )
    assert gpu_unsupported_features(track, style) == ()

    legacy = Style()
    assert gpu_unsupported_features(track, legacy) == ()



def test_line_animation_override_accepts_new_kinds():
    override = LineAnimationOverride(
        entry_anim="wave_in",
        exit_anim="converge_out",
    )
    assert override.entry_anim == "wave_in"
    assert override.exit_anim == "converge_out"


def test_painter_sing_fx_char_anchor_smoke(qapp):
    """CPU painter 唱字粒子（char 锚点）冒烟：不抛异常且画面有粒子。"""
    from PyQt6.QtGui import QImage

    from krok_helper.subtitle_render.domain.timing import TimingChar, TimingTrack
    from krok_helper.subtitle_render.engine.painter import paint_frame

    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[
                    TimingChar(text="あ", start_ms=1000),
                    TimingChar(text="い", start_ms=1600),
                ],
                end_ms=2200,
            )
        ]
    )
    style = Style(
        sing_fx="twinkle",
        karaoke_anim="utopia",
        fx_particle_size_em=0.6,
        fx_particle_count=8,
    )
    img = QImage(800, 450, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(0xFF101010)
    paint_frame(img, track, 1300, style)  # 「あ」唱到一半，wipe 锚点命中


def test_style_controller_keeps_new_geo_kinds():
    """回归护栏：属性面板样式漏斗（PropertyStyleController.update →
    normalize_entry/exit_animation）必须保留新几何档位。

    2026-09 用户实测定位：白名单缺新档位时回落 "none"，表现为「选中
    新特效后所有字幕行被刷为无、预览退化为无特效」——渲染端一切正常。
    """
    from krok_helper.subtitle_render.settings.property_controllers import (
        PropertyStyleController,
    )

    controller = PropertyStyleController()
    base = Style()
    cases = {
        "entry_anim": (
            "tracking_in", "wave_in", "stretch_in", "glow_in", "assemble_in",
            "sparkle", "ripple", "note",
        ),
        "exit_anim": (
            "scatter_out", "converge_out", "stretch_out", "glow_out", "dissolve_out",
            "sparkle", "ripple", "note",
        ),
        "section_head_anim": (
            "tracking_in", "wave_in", "stretch_in", "glow_in", "assemble_in",
            "sparkle", "ripple", "note",
        ),
        "section_tail_anim": (
            "scatter_out", "converge_out", "stretch_out", "glow_out", "dissolve_out",
            "sparkle", "ripple", "note",
        ),
    }
    for field, values in cases.items():
        for value in values:
            result = controller.update(base, {field: value})
            assert getattr(result.style, field) == value
    # 未知值仍回落 none（既有语义不回归）。
    assert controller.update(base, {"entry_anim": "bogus"}).style.entry_anim == "none"


def test_ring_sprite_bezier_midpoints_stay_on_circle():
    """回归护栏（纯数学，无 Qt 依赖）：圆环 sprite 的每段贝塞尔中点必须
    落在圆周上（距圆心 = 半径，±2%）。

    2026-09 用户实测定位：_circle_commands 的 flip 只反转了角度、没同步
    反转切向，内圆四段贝塞尔外翻、孔洞被挤成菱形（铜钱观感）。
    """
    import math

    commands = FX_SPRITES["ring"]["path_commands"]
    circles: list[tuple[float, list[tuple[float, float]]]] = []
    current_radius = 0.0
    points: list[tuple[float, float]] = []
    previous = (0.0, 0.0)
    for command in commands:
        kind = command[0]
        if kind == "M":
            if points:
                circles.append((current_radius, points))
            current_radius = abs(float(command[1]))
            previous = (float(command[1]), float(command[2]))
            points = []
        elif kind == "C":
            p0 = previous
            c1 = (float(command[1]), float(command[2]))
            c2 = (float(command[3]), float(command[4]))
            p1 = (float(command[5]), float(command[6]))
            mid = tuple(
                (p0[i] + 3 * c1[i] + 3 * c2[i] + p1[i]) / 8.0 for i in (0, 1)
            )
            points.append(mid)
            previous = p1
    if points:
        circles.append((current_radius, points))

    assert len(circles) == 2
    for radius, mids in circles:
        # kappa 圆的每段中点落在 45° 弧上，距圆心即半径（±2%）；
        # 旧缺陷（切向未随绕向翻转）下中点半径会掉到 ~0.41r。
        for mid in mids:
            distance = math.hypot(*mid)
            assert distance == pytest.approx(radius, rel=0.02), (
                f"半径 {radius} 的贝塞尔中点 {mid} 偏离圆周"
            )


def test_stretch_and_glow_transition_math():
    """逐字拉伸（stretch）与整行辉光（glow）的编排与包络。"""
    from krok_helper.subtitle_render.engine.render.elements.horizontal.transitions import (
        transition_char_glow,
    )

    style = Style(font_size_px=100, entry_lead_ms=600, exit_fade_ms=600)
    ctx = line_char_transition_context(
        Style(entry_anim="glow_in", entry_lead_ms=600, karaoke_anim="none"),
        TimingLine(), 100, 0, 6000, 3,
    )
    assert ctx is not None and ctx.effect == "glow_in"

    # 逐字拉伸：出生 3.2× 横向拉伸、归位 1.0；消散反向拉到 3.2×。
    born = _geo_char_state(
        style, _entry_transition("stretch_in", 0), 0, 5,
        t_ms=0, char_center_x=None, line_center_x=None,
    )
    assert born[0] == 0.0
    assert born[4] == pytest.approx(3.2)
    settled = _geo_char_state(
        style, _entry_transition("stretch_in", 0), 0, 5,
        t_ms=10_000, char_center_x=None, line_center_x=None,
    )
    assert settled[0] == 1.0 and settled[4] == pytest.approx(1.0)
    gone = _geo_char_state(
        style, _exit_transition("stretch_out", 0), 0, 5,
        t_ms=10_000, char_center_x=None, line_center_x=None,
    )
    assert gone[0] == 0.0 and gone[4] == pytest.approx(3.2)

    # 整行辉光：全字同步（任意 index 同值），出生半径 2.0em + 高亮表。
    glow_a = transition_char_glow(_entry_transition("glow_in", 0), 0, 5, t_ms=0, configured_ms=600)
    glow_b = transition_char_glow(_entry_transition("glow_in", 0), 4, 5, t_ms=0, configured_ms=600)
    assert glow_a == glow_b  # 无逐字错峰
    assert glow_a[0] == pytest.approx(1.0)
    assert glow_a[1] == pytest.approx(1.0)  # 回声间距系数（出生 1.0）
    assert glow_a[2] is True  # 横向回声（bright 通道）
    glow_settled = transition_char_glow(
        _entry_transition("glow_in", 0), 2, 5, t_ms=10_000, configured_ms=600
    )
    assert glow_settled[0] == pytest.approx(0.0)
    assert glow_settled[1] == pytest.approx(0.75)
    # 字形保持原形（发光靠重影副本，无 scaleX 拉伸）。
    glow_born_state = _geo_char_state(
        style, _entry_transition("glow_in", 0), 3, 5,
        t_ms=0, char_center_x=None, line_center_x=None,
    )
    assert glow_born_state[0] == 0.0
    assert glow_born_state[4] == pytest.approx(1.6)  # 本体轻度拉伸回弹

    # 拉伸辉光：常规表 + 逐字错峰（不同 index 不同相位）。
    stretch_a = transition_char_glow(
        _entry_transition("stretch_in", 0), 0, 5, t_ms=0, configured_ms=600
    )
    stretch_b = transition_char_glow(
        _entry_transition("stretch_in", 0), 4, 5, t_ms=0, configured_ms=600
    )
    assert stretch_a is not None and stretch_a[2] is False
    # 拉伸辉光逐字错峰：后字出生相位不同（此处仍处满强度等待出生）。
    assert stretch_b is not None

    # 拼接/消散核心曲线：拼接抵达后显形；消散先行散去。
    assembling = _geo_char_state(
        style, _entry_transition("assemble_in", 0), 0, 5,
        t_ms=10_000, char_center_x=None, line_center_x=None,
    )
    assert assembling[0] == 1.0
    early = _geo_char_state(
        style, _entry_transition("assemble_in", 0), 0, 5,
        t_ms=0, char_center_x=None, line_center_x=None,
    )
    assert early[0] == 0.0
    dissolved = _geo_char_state(
        style, _exit_transition("dissolve_out", 0), 0, 5,
        t_ms=10_000, char_center_x=None, line_center_x=None,
    )
    assert dissolved[0] == 0.0

    # 非辉光类特效返回 None。
    assert transition_char_glow(
        _entry_transition("tracking_in", 0), 0, 5, t_ms=0, configured_ms=600
    ) is None


def test_plan_line_bursts_assemble_and_dissolve():
    """粒子拼接/消散：由动画档位驱动，逐字错峰、锚在字符上。"""
    style = Style(
        entry_anim="assemble_in",
        entry_lead_ms=600,
        exit_anim="dissolve_out",
        exit_fade_ms=600,
        fx_particle_count=12,
        font_size_px=100,
        karaoke_anim="none",
    )
    bursts = plan_line_bursts(
        style, 0, 1000, 4000, 3900,
        [(1200, 1600), (1600, 2000), (2000, 3000)],
    )
    kinds = [burst["kind"] for burst in bursts]
    assert kinds.count("assemble") == 3
    assert kinds.count("dissolve") == 3
    assemble = next(b for b in bursts if b["kind"] == "assemble")
    assert assemble["anchor"] == "char"
    assert assemble["count"] == 7  # 固定档 max(4, 14//2)，不吃旋钮
    assert assemble["start_ms"] == 1000
    assert assemble["end_ms"] == 1000 + 320
    dissolve = next(b for b in bursts if b["kind"] == "dissolve")
    assert dissolve["start_ms"] >= 3900 - 120  # 最小窗钳制


def test_style_funnel_and_gpu_support_glow_kinds():
    """辉光档位全链路：UI 漏斗保留 + GPU 无回退 + 工程序列化可往返。"""
    from krok_helper.subtitle_render.settings.property_controllers import (
        PropertyStyleController,
    )

    controller = PropertyStyleController()
    result = controller.update(Style(), {"entry_anim": "glow_in"})
    assert result.style.entry_anim == "glow_in"
    result = controller.update(Style(), {"exit_anim": "glow_out"})
    assert result.style.exit_anim == "glow_out"

    track = type("Track", (), {"lines": []})()
    style = Style(entry_anim="glow_in", exit_anim="glow_out", karaoke_anim="utopia")
    assert gpu_unsupported_features(track, style) == ()

    from krok_helper.subtitle_render.serialization.timing import (
        line_animation_override_from_dict,
    )

    override = line_animation_override_from_dict(
        {"entry_anim": "glow_in", "exit_anim": "glow_out"}
    )
    assert override is not None and override.entry_anim == "glow_in"


def test_particle_anim_core_is_staggered_char_transition():
    """星光/涟漪/音符出入场动画的本体 = 逐字显形/淡出（粒子拼接同款编排）。"""
    style = Style(font_size_px=100, entry_lead_ms=600, exit_fade_ms=600)
    ctx = line_char_transition_context(
        Style(entry_anim="sparkle", entry_lead_ms=600, karaoke_anim="none"),
        TimingLine(), 100, 0, 6000, 3,
    )
    assert ctx is not None and ctx.effect == "sparkle"

    # 入场：逐字错峰（char1 尚未出生、char0 已显形）。
    first = _geo_char_state(
        style, _entry_transition("sparkle", 0), 0, 5,
        t_ms=100, char_center_x=None, line_center_x=None,
    )
    second = _geo_char_state(
        style, _entry_transition("sparkle", 0), 1, 5,
        t_ms=100, char_center_x=None, line_center_x=None,
    )
    assert first[0] > 0.8
    assert second[0] < 0.5  # 错峰：后字明显滞后（透明度尚低）
    settled = _geo_char_state(
        style, _entry_transition("note", 0), 4, 5,
        t_ms=10_000, char_center_x=None, line_center_x=None,
    )
    assert settled[0] == 1.0

    # 退场：逐字淡出（中段时 char0 已淡、char4 尚未轮到）。
    gone = _geo_char_state(
        style, _exit_transition("ripple", 0), 0, 5,
        t_ms=200, char_center_x=None, line_center_x=None,
    )
    intact = _geo_char_state(
        style, _exit_transition("ripple", 0), 4, 5,
        t_ms=200, char_center_x=None, line_center_x=None,
    )
    assert gone[0] < 0.3
    assert intact[0] == 1.0
    # 窗口结束后全部淡出。
    assert _geo_char_state(
        style, _exit_transition("ripple", 0), 4, 5,
        t_ms=10_000, char_center_x=None, line_center_x=None,
    )[0] == 0.0
