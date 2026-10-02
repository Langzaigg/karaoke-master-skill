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


def test_sing_particles_skip_whitespace_chars():
    """空格无走字内容：唱字粒子跳过；出入场粒子仍整行参与。"""
    style = Style(
        sing_fx="twinkle",
        entry_anim="ripple",
        entry_lead_ms=600,
        karaoke_anim="none",
    )
    # 「あ い」中间是空格：3 个字符窗。
    windows = [(1200, 1600), (1600, 2000), (2000, 2400)]
    visible = [True, False, True]
    bursts = plan_line_bursts(
        style, 0, 1000, 4000, 3900, windows, char_visible=visible
    )
    sing_starts = [b["start_ms"] for b in bursts if b["kind"] == "twinkle"]
    # 空格（1600 起）不发射唱字粒子。
    assert 1600 not in sing_starts
    assert sorted(sing_starts) == [1200, 2000]
    # 入场涟漪仍按整行逐字（3 枚，含空格位）。
    ripples = [b for b in bursts if b["kind"] == "ripple"]
    assert [b["char_index"] for b in ripples] == [0, 1, 2]


# ---------------------------------------------------------------------------
# 2026-10 粒子颜色模式（单独颜色 / 跟随字体·走字前后 / 复用配色方案）
# 与「入退场同用」联动开关
# ---------------------------------------------------------------------------


def _karaoke_matrix(before: str, after: str):
    from krok_helper.subtitle_render.domain.paint import (
        KaraokeColorState,
        KaraokeColors,
    )
    from krok_helper.subtitle_render.engine.style.style_semantics import solid_fill

    return KaraokeColors(
        before=KaraokeColorState(text=solid_fill(before)),
        after=KaraokeColorState(text=solid_fill(after)),
    )


def test_particle_color_modes_follow_font_states():
    """跟随字体·走字前/后：取所在行有效配色的主文字填充折算实色。"""
    from krok_helper.subtitle_render.engine.render.effects.particles import (
        particle_solid_color,
    )

    style = Style(
        fx_particle_color="#00FF00",
        karaoke_colors=_karaoke_matrix("#123456", "#ABCDEF"),
    )
    assert particle_solid_color(style) == "#00FF00"  # 默认单独颜色
    assert particle_solid_color(
        Style(
            fx_particle_color="#00FF00",
            karaoke_colors=_karaoke_matrix("#123456", "#ABCDEF"),
            fx_particle_color_mode="follow_before",
        )
    ) == "#123456"
    assert particle_solid_color(
        Style(
            fx_particle_color="#00FF00",
            karaoke_colors=_karaoke_matrix("#123456", "#ABCDEF"),
            fx_particle_color_mode="follow_after",
        )
    ) == "#ABCDEF"
    # 旧工程无配色矩阵：走 legacy 推导（before=base_color，after=fill_color）。
    legacy_before = particle_solid_color(
        Style(
            base_color="#2468AC",
            fill_color="#FF00FF",
            fx_particle_color_mode="follow_before",
        )
    )
    legacy_after = particle_solid_color(
        Style(
            base_color="#2468AC",
            fill_color="#FF00FF",
            fx_particle_color_mode="follow_after",
        )
    )
    assert legacy_before == "#2468AC" and legacy_after == "#FF00FF"


def test_particle_color_mode_gradient_averages_stops():
    """渐变/拼色填充折算为停止色平均；图片填充回退单独颜色。"""
    from krok_helper.subtitle_render.domain.paint import (
        KaraokeColorState,
        KaraokeColors,
        PaintFill,
    )
    from krok_helper.subtitle_render.engine.render.effects.particles import (
        fill_to_solid_color,
        particle_solid_color,
    )

    gradient = PaintFill(
        mode="gradient_horizontal",
        start_color="#FF0000",
        end_color="#0000FF",
        gradient_stops=[(0, "#FF0000"), (50, "#00FF00"), (100, "#0000FF")],
    )
    # (FF0000 + 00FF00 + 0000FF) / 3 = (85, 85, 85)。
    assert fill_to_solid_color(gradient, "#123456") == "#555555"
    split = PaintFill(
        mode="split_vertical",
        split_top_color="#FF0000",
        split_bottom_color="#0000FF",
        split_stops=[(0, "#FF0000"), (100, "#0000FF")],
    )
    assert fill_to_solid_color(split, "#123456") == "#7F007F"
    image = PaintFill(mode="image", image_path="x.png")
    assert fill_to_solid_color(image, "#00FF00") == "#00FF00"
    style = Style(
        fx_particle_color="#00FF00",
        fx_particle_color_mode="follow_after",
        karaoke_colors=KaraokeColors(
            after=KaraokeColorState(text=gradient),
        ),
    )
    assert particle_solid_color(style) == "#555555"


def test_particle_color_mode_role_source_and_dangling_fallback():
    """复用配色方案：取来源「走字后-主文字」；悬空引用回退单独颜色。"""
    from krok_helper.subtitle_render.domain.models import (
        SCANLINE_GLOBAL_ROLE_KEY,
        SubtitleStyleScheme,
    )
    from krok_helper.subtitle_render.engine.render.effects.particles import (
        particle_solid_color,
    )

    scheme = SubtitleStyleScheme(
        karaoke_colors=_karaoke_matrix("#000000", "#EE7700")
    )
    style = Style(
        fx_particle_color="#00FF00",
        fx_particle_color_mode="role",
        fx_particle_role_name="主唱",
        custom_style_schemes={"主唱": scheme},
        karaoke_colors=_karaoke_matrix("#111111", "#222222"),
    )
    assert particle_solid_color(style) == "#EE7700"
    # 全局默认 = 主样式自身的走字后。
    assert particle_solid_color(
        Style(
            fx_particle_color="#00FF00",
            fx_particle_color_mode="role",
            fx_particle_role_name=SCANLINE_GLOBAL_ROLE_KEY,
            karaoke_colors=_karaoke_matrix("#111111", "#222222"),
        )
    ) == "#222222"
    # 名字悬空（历史工程/手工 JSON）：回退 color 档。
    assert particle_solid_color(
        Style(
            fx_particle_color="#00FF00",
            fx_particle_color_mode="role",
            fx_particle_role_name="已删除",
        )
    ) == "#00FF00"


def test_fx_apply_to_entry_exit_switch_plans():
    """「入退场同用」：开启后入退场动画粒子的颜色/尺寸吃旋钮；数量恒固定。"""

    def _style(**extra):
        base = dict(
            entry_anim="assemble_in",
            entry_lead_ms=600,
            exit_anim="dissolve_out",
            exit_fade_ms=600,
            sing_fx="twinkle",
            fx_particle_size_em=0.5,
            fx_particle_count=10,
            fx_particle_color="#FF8800",
            font_size_px=100,
            karaoke_anim="none",
        )
        base.update(extra)
        return Style(**base)

    windows = [(1200, 1600), (1600, 3000)]
    off = plan_line_bursts(_style(), 0, 1000, 4000, 3900, windows)
    assemble = next(b for b in off if b["kind"] == "assemble")
    twinkle = next(b for b in off if b["kind"] == "twinkle")
    # 默认关：入退场动画粒子固定白档 + 固定尺寸（40% × 0.75），唱字吃旋钮。
    assert assemble["color"] == "#FFFFFF"
    assert assemble["size_px"] == pytest.approx(100 * 0.40 * 0.75)
    assert assemble["count"] == 7  # 数量固定档不吃旋钮
    assert twinkle["color"] == "#FF8800"
    assert twinkle["size_px"] == pytest.approx(50.0)

    on = plan_line_bursts(
        _style(fx_apply_to_entry_exit=True), 0, 1000, 4000, 3900, windows
    )
    assemble_on = next(b for b in on if b["kind"] == "assemble")
    twinkle_on = next(b for b in on if b["kind"] == "twinkle")
    # 开启后：颜色与尺寸跟随旋钮；数量仍固定。
    assert assemble_on["color"] == "#FF8800"
    assert assemble_on["size_px"] == pytest.approx(50.0 * 0.75)
    assert assemble_on["count"] == 7
    assert twinkle_on["color"] == "#FF8800"
    assert twinkle_on["size_px"] == pytest.approx(50.0)

    # 颜色模式对入退场动画粒子同样生效（开启联动时）。
    follow = plan_line_bursts(
        _style(
            fx_apply_to_entry_exit=True,
            fx_particle_color_mode="follow_after",
            karaoke_colors=_karaoke_matrix("#000000", "#EE7700"),
        ),
        0,
        1000,
        4000,
        3900,
        windows,
    )
    assert next(b for b in follow if b["kind"] == "assemble")["color"] == "#EE7700"
    # 关闭联动时模式只影响唱字粒子，入退场动画粒子仍是固定白档。
    follow_off = plan_line_bursts(
        _style(
            fx_particle_color_mode="follow_after",
            karaoke_colors=_karaoke_matrix("#000000", "#EE7700"),
        ),
        0,
        1000,
        4000,
        3900,
        windows,
    )
    assert (
        next(b for b in follow_off if b["kind"] == "assemble")["color"] == "#FFFFFF"
    )
    assert next(b for b in follow_off if b["kind"] == "twinkle")["color"] == "#EE7700"


def test_particle_color_mode_serialization_roundtrip():
    """颜色模式/来源/联动开关三字段的工程序列化往返与非法值回退。"""
    from krok_helper.subtitle_render.domain.models import style_from_dict, style_to_dict

    style = Style(
        fx_particle_color_mode="follow_after",
        fx_particle_role_name="主唱",
        fx_apply_to_entry_exit=True,
    )
    restored = style_from_dict(style_to_dict(style))
    assert restored.fx_particle_color_mode == "follow_after"
    assert restored.fx_particle_role_name == "主唱"
    assert restored.fx_apply_to_entry_exit is True
    # 旧工程 payload（无新字段）：回落默认，不炸。
    legacy = style_from_dict({})
    assert legacy.fx_particle_color_mode == "color"
    assert legacy.fx_particle_role_name is None
    assert legacy.fx_apply_to_entry_exit is False
    # 非法值回落默认；空串来源名归一 None。
    payload = style_to_dict(Style())
    payload["fx_particle_color_mode"] = "brighten"  # 粒子无此档
    payload["fx_particle_role_name"] = "  "
    payload["fx_apply_to_entry_exit"] = 0
    degraded = style_from_dict(payload)
    assert degraded.fx_particle_color_mode == "color"
    assert degraded.fx_particle_role_name is None
    assert degraded.fx_apply_to_entry_exit is False


def test_particle_role_reference_remap_chain():
    """来源改名连带改写；删除连模式一起回退单独颜色（扫字线同款）。"""
    from krok_helper.subtitle_render.domain.models import (
        remap_particle_role_reference,
    )

    style = Style(fx_particle_color_mode="role", fx_particle_role_name="主唱")
    renamed = remap_particle_role_reference(style, {"主唱": "领唱"})
    assert renamed is not None
    assert renamed.fx_particle_role_name == "领唱"
    assert renamed.fx_particle_color_mode == "role"
    deleted = remap_particle_role_reference(style, {"主唱": None})
    assert deleted is not None
    assert deleted.fx_particle_color_mode == "color"
    assert deleted.fx_particle_role_name is None
    # 引用未触及返回 None（调用方跳过样式写回）。
    assert remap_particle_role_reference(style, {"和声": "伴唱"}) is None
    assert remap_particle_role_reference(Style(), {"主唱": None}) is None


def test_painter_paints_per_burst_particle_color(qapp):
    """CPU painter 按 burst 颜色绘制（与 D2D burst.color 同口径）。

    2026-10 发现的历史分歧：painter 此前把所有 burst 一律画成
    ``fx_particle_color``，而 GPU 侧入退场动画粒子按规划是固定白色——
    旋钮色非白时两后端画面不一致。这里用「无粒子基线」逐像素差分验证
    跟随字体两档的唱字粒子按行配色着色（字形本体两帧完全一致，差分
    只剩粒子贡献）。
    """
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

    def _render(mode: str) -> QImage:
        style = Style(
            sing_fx="twinkle",
            karaoke_anim="utopia",
            fx_particle_size_em=0.8,
            fx_particle_count=16,
            fx_particle_color_mode=mode,
            karaoke_colors=_karaoke_matrix("#FF0000", "#0000FF"),
        )
        img = QImage(800, 450, QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(0xFF101010)
        paint_frame(img, track, 1300, style)
        return img

    def _dominance(mode: str) -> tuple[int, int]:
        """与「无粒子基线」差分中红/蓝主导的像素数。

        半透明粒子叠在异色字形上会向基线色偏移（弱信号不计入主导），
        因此断言用两档之间的相对主导方向判颜色，而不是绝对为零。
        """
        with_fx = _render(mode)
        none_img = QImage(800, 450, QImage.Format.Format_ARGB32_Premultiplied)
        none_img.fill(0xFF101010)
        paint_frame(
            none_img,
            track,
            1300,
            Style(
                sing_fx="none",
                karaoke_anim="utopia",
                karaoke_colors=_karaoke_matrix("#FF0000", "#0000FF"),
            ),
        )
        red_dominant = blue_dominant = 0
        for y in range(0, with_fx.height(), 2):
            for x in range(0, with_fx.width(), 2):
                pixel = with_fx.pixel(x, y)
                if pixel == none_img.pixel(x, y):
                    continue  # 粒子未覆盖的纯字形/背景像素
                red = (pixel >> 16) & 0xFF
                green = (pixel >> 8) & 0xFF
                blue = pixel & 0xFF
                if red > 140 and red > green + 40 and red > blue + 40:
                    red_dominant += 1
                if blue > 140 and blue > red + 40 and blue > green + 40:
                    blue_dominant += 1
        return red_dominant, blue_dominant

    # 跟随字体·走字前：粒子红主导（走字前主文字色）。
    red_before, blue_before = _dominance("follow_before")
    assert red_before > 8, f"走字前粒子应红主导，实测红 {red_before} 蓝 {blue_before}"
    assert red_before > blue_before
    # 跟随字体·走字后：粒子蓝主导（走字后主文字色）。
    red_after, blue_after = _dominance("follow_after")
    assert blue_after > 8, f"走字后粒子应蓝主导，实测蓝 {blue_after} 红 {red_after}"
    assert blue_after > red_after


# ---------------------------------------------------------------------------
# 2026-10 二期：逐字角色解析 + 完整装饰规格（渐变/描边/二重描边 + 极坐标环）
# ---------------------------------------------------------------------------


def _gradient_fill(mode: str, start: str, end: str):
    from krok_helper.subtitle_render.domain.paint import PaintFill

    return PaintFill(
        mode=mode,
        start_color=start,
        end_color=end,
        gradient_stops=[(0, start), (100, end)],
        split_top_color=start,
        split_bottom_color=end,
        split_stops=[(0, start), (100, end)],
    )


def test_particle_paint_spec_follows_each_chars_role():
    """跟随字体逐字取色：同一行内不同角色方案的字符拿到各自的三层规格。"""
    from krok_helper.subtitle_render.domain.models import SubtitleStyleScheme
    from krok_helper.subtitle_render.domain.paint import (
        KaraokeColorState,
        KaraokeColors,
    )
    from krok_helper.subtitle_render.engine.style.style_semantics import style_for_role

    scheme_a = SubtitleStyleScheme(
        karaoke_colors=KaraokeColors(
            after=KaraokeColorState(text=_gradient_fill(
                "gradient_horizontal", "#FF8800", "#00FF88"
            ))
        )
    )
    scheme_b = SubtitleStyleScheme(
        karaoke_colors=KaraokeColors(
            after=KaraokeColorState(text=_gradient_fill(
                "gradient_vertical", "#FF0000", "#0000FF"
            ))
        )
    )
    style = Style(
        font_size_px=100,
        sing_fx="twinkle",
        fx_particle_color_mode="follow_after",
        karaoke_anim="none",
        custom_style_schemes={"主唱": scheme_a, "和声": scheme_b},
        karaoke_colors=_karaoke_matrix("#111111", "#222222"),
    )
    char_styles = [
        style_for_role(style, "主唱"),
        style_for_role(style, "和声"),
        None,  # 未挂角色的字符：回落行样式
    ]
    windows = [(1200, 1600), (1600, 2000), (2000, 2400)]
    bursts = plan_line_bursts(
        style, 0, 1000, 4000, 3900, windows, char_styles=char_styles
    )
    twinkle = sorted(
        (b for b in bursts if b["kind"] == "twinkle"),
        key=lambda b: b["char_index"],
    )
    assert len(twinkle) == 3
    # 字 0：主唱方案的横向渐变；字 1：和声方案的竖向渐变；字 2：行样式纯色。
    assert twinkle[0]["paint"]["fill"]["mode"] == "gradient_horizontal"
    assert twinkle[1]["paint"]["fill"]["mode"] == "gradient_vertical"
    assert twinkle[2]["paint"]["fill"]["mode"] == "solid"
    assert twinkle[2]["paint"]["fill"]["color"] == "#222222"
    # 实色回退同步按字符解析（burst.color 与规格同源）。
    assert twinkle[0]["color"] == "#7FC344"  # (FF8800+00FF88)/2
    assert twinkle[1]["color"] == "#7F007F"
    assert twinkle[2]["color"] == "#222222"
    # 未提供 char_styles 时（旧调用方）：整体回落行样式。
    fallback = plan_line_bursts(style, 0, 1000, 4000, 3900, windows)
    assert all(
        b["paint"]["fill"]["color"] == "#222222"
        for b in fallback
        if b["kind"] == "twinkle"
    )


def test_particle_paint_spec_stroke_widths_scale_with_source_font():
    """描边/二重描边宽按 粒子尺寸/来源字号 缩放，上限半个粒子边长。"""
    style = Style(
        font_size_px=100,
        sing_fx="twinkle",
        fx_particle_size_em=0.5,  # 粒子 50px
        fx_particle_color_mode="follow_after",
        karaoke_anim="none",
        stroke_width_px=8,
        stroke2_enabled=True,
        stroke2_width_px=4,
        karaoke_colors=_karaoke_matrix("#000000", "#FFFFFF"),
    )
    bursts = plan_line_bursts(style, 0, 1000, 4000, 3900, [(1200, 1600)])
    spec = next(b for b in bursts if b["kind"] == "twinkle")["paint"]
    assert spec["stroke_width_px"] == pytest.approx(8 * 0.5)
    assert spec["stroke2_width_px"] == pytest.approx(4 * 0.5)
    # 巨描边钳到半个粒子边长。
    huge = Style(
        font_size_px=20,
        sing_fx="twinkle",
        fx_particle_size_em=0.5,  # 粒子 10px，半边长 5px
        fx_particle_color_mode="follow_after",
        karaoke_anim="none",
        stroke_width_px=40,
        stroke2_enabled=False,
        karaoke_colors=_karaoke_matrix("#000000", "#FFFFFF"),
    )
    capped = plan_line_bursts(huge, 0, 1000, 4000, 3900, [(1200, 1600)])
    assert (
        next(b for b in capped if b["kind"] == "twinkle")["paint"][
            "stroke_width_px"
        ]
        <= 5.0 + 1e-6
    )
    # UseEdge2 关闭：二重描边宽恒 0（N3 语义不回归）。
    off = Style(
        font_size_px=100,
        sing_fx="twinkle",
        fx_particle_color_mode="follow_after",
        karaoke_anim="none",
        stroke_width_px=8,
        stroke2_enabled=False,
        stroke2_width_px=4,
        karaoke_colors=_karaoke_matrix("#000000", "#FFFFFF"),
    )
    off_spec = next(
        b for b in plan_line_bursts(off, 0, 1000, 4000, 3900, [(1200, 1600)])
        if b["kind"] == "twinkle"
    )["paint"]
    assert off_spec["stroke2_width_px"] == 0.0


def test_particle_paint_spec_image_fill_falls_back_to_solid():
    """图片填充折为单独颜色实心（跨端一致回退）。"""
    from krok_helper.subtitle_render.domain.paint import (
        KaraokeColorState,
        KaraokeColors,
        PaintFill,
    )

    image_fill = PaintFill(mode="image", image_path="x.png")
    style = Style(
        font_size_px=100,
        sing_fx="twinkle",
        fx_particle_color="#ABCDEF",
        fx_particle_color_mode="follow_after",
        karaoke_anim="none",
        karaoke_colors=KaraokeColors(
            after=KaraokeColorState(text=image_fill)
        ),
    )
    bursts = plan_line_bursts(style, 0, 1000, 4000, 3900, [(1200, 1600)])
    spec = next(b for b in bursts if b["kind"] == "twinkle")["paint"]
    assert spec["fill"]["mode"] == "solid"
    assert spec["fill"]["color"] == "#ABCDEF"


def test_painter_gradient_particle_and_radial_ring(qapp):
    """CPU painter：规格档粒子按完整 PaintFill 绘制；涟漪竖向渐变做径向
    映射（新出生的内圈环=起点色亮红，扩散中的外圈环沿渐变轴推进）。"""
    from PyQt6.QtGui import QImage

    from krok_helper.subtitle_render.domain.paint import (
        KaraokeColorState,
        KaraokeColors,
    )
    from krok_helper.subtitle_render.domain.timing import TimingChar, TimingTrack
    from krok_helper.subtitle_render.engine.painter import paint_frame

    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar(text="あ", start_ms=1000)],
                end_ms=1600,
            )
        ]
    )
    # 横向渐变填充：粒子内部左端橙、右端绿（主文字纯色不产生绿端）。
    grad_h = _gradient_fill("gradient_horizontal", "#FF8800", "#00FF88")
    style_h = Style(
        sing_fx="twinkle",
        karaoke_anim="utopia",
        fx_particle_size_em=0.8,
        fx_particle_count=16,
        fx_particle_color_mode="follow_after",
        karaoke_colors=KaraokeColors(
            after=KaraokeColorState(text=grad_h)
        ),
    )
    img = QImage(800, 450, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(0xFF101010)
    paint_frame(img, track, 1300, style_h)
    orange = green = 0
    for y in range(0, img.height(), 2):
        for x in range(0, img.width(), 2):
            px = img.pixel(x, y)
            r, g, b = (px >> 16) & 0xFF, (px >> 8) & 0xFF, px & 0xFF
            if r > 170 and 90 < g < 200 and b < 90:
                orange += 1
            if g > 130 and g > r + 30 and g > b + 10:
                green += 1
    assert orange > 5 and green > 5, f"渐变两端都应出现 o={orange} g={green}"

    # 竖向渐变涟漪：径向映射——出生不久的环按进度取到偏红段（阈值
    # 放宽到 60/20：环透明度随生命期 (1-p)² 衰减，老环必然偏暗）。
    grad_v = _gradient_fill("gradient_vertical", "#FF0000", "#0000FF")
    style_ring = Style(
        sing_fx="ripple",
        karaoke_anim="utopia",
        fx_particle_size_em=0.8,
        fx_particle_color_mode="follow_after",
        karaoke_colors=KaraokeColors(after=KaraokeColorState(text=grad_v)),
    )
    img2 = QImage(800, 450, QImage.Format.Format_ARGB32_Premultiplied)
    img2.fill(0xFF101010)
    paint_frame(img2, track, 1100, style_ring)
    red_around = 0
    for y in range(0, img2.height(), 2):
        for x in range(0, img2.width(), 2):
            px = img2.pixel(x, y)
            r, g, b = (px >> 16) & 0xFF, (px >> 8) & 0xFF, px & 0xFF
            if r > 60 and r > g + 20 and r > b + 20:
                red_around += 1
    assert red_around > 20, (
        f"径向环新环应取渐变起点的红段，实测红像素 {red_around}"
    )


def test_fx_radial_ring_samples_gradient_by_expansion():
    """涟漪非横向渐变的径向映射：渐变轴 → 半径方向。

    每颗环按自身扩散进度（eased，量化 1/32 档）在渐变轴上采样一个实心
    色：新出生的小环（内圈）= 起点色，扩散开的旧环（外圈）= 终点色——
    「从圆心内到外圆渐变」（2026-10 用户口径；conic 角向扫过已被否定）。
    """
    from krok_helper.subtitle_render.domain.timing import TimingChar, TimingTrack
    from krok_helper.subtitle_render.engine.painter import _fx_fill_color_at

    grad = _gradient_fill("gradient_vertical", "#FF0000", "#0000FF")
    # 采样器口径：0=起点色（纯红），1=终点色（纯蓝），线性插值。
    quarter = _fx_fill_color_at(grad, 0.25)
    assert abs(quarter.red() - 191) <= 1 and abs(quarter.blue() - 64) <= 1
    half = _fx_fill_color_at(grad, 0.5)
    assert abs(half.red() - 128) <= 1 and abs(half.blue() - 128) <= 1
    # 拼色：分段常数（每段标记该色起点）。
    from krok_helper.subtitle_render.domain.paint import PaintFill

    split = PaintFill(
        mode="split_vertical",
        split_top_color="#00FF00",
        split_bottom_color="#FFFFFF",
        split_stops=[(0, "#00FF00"), (50, "#FFFFFF"), (100, "#FFFFFF")],
    )
    assert _fx_fill_color_at(split, 0.3).name().upper() == "#00FF00"
    assert _fx_fill_color_at(split, 0.9).name().upper() == "#FFFFFF"


def test_ripple_bursts_carry_no_stroke_and_spec_cache_reuses():
    """涟漪光环不带描边（环体发丝线，叠描边显著变粗——2026-10 用户口径）；
    同一「样式 × 角色 × 尺寸」组合的装饰规格全帧复用同一对象（缓存命中）。"""
    from krok_helper.subtitle_render.engine.render.effects.particles import (
        clear_particle_paint_cache,
    )

    clear_particle_paint_cache()
    style = Style(
        font_size_px=64,
        sing_fx="ripple",
        karaoke_anim="none",
        fx_particle_color_mode="follow_after",
        stroke_width_px=8,
        stroke2_enabled=True,
        stroke2_width_px=4,
        karaoke_colors=_karaoke_matrix("#40E0FF", "#FF5A6F"),
    )
    windows = [(1200, 1600), (1600, 2000)]
    first = plan_line_bursts(style, 0, 1000, 4000, 3900, windows)
    rings = [b for b in first if b["kind"] == "ripple"]
    assert rings, "涟漪 burst 应存在"
    for burst in rings:
        assert burst["paint"]["stroke_width_px"] == 0.0
        assert burst["paint"]["stroke2_width_px"] == 0.0
    # 非涟漪档（twinkle）仍带描边——开关只作用于涟漪。
    twinkle_style = Style(
        font_size_px=64,
        sing_fx="twinkle",
        karaoke_anim="none",
        fx_particle_color_mode="follow_after",
        stroke_width_px=8,
        stroke2_enabled=True,
        stroke2_width_px=4,
        karaoke_colors=_karaoke_matrix("#40E0FF", "#FF5A6F"),
    )
    twinkle = plan_line_bursts(twinkle_style, 0, 1000, 4000, 3900, windows)
    spec = next(b for b in twinkle if b["kind"] == "twinkle")["paint"]
    assert spec["stroke_width_px"] > 0.0
    assert spec["stroke2_width_px"] > 0.0
    # 缓存 identity：同参数二次规划，spec 与实色对象复用（只烘焙一次）。
    again = plan_line_bursts(style, 0, 1000, 4000, 3900, windows)
    ring_a = next(b for b in first if b["kind"] == "ripple")
    ring_b = next(b for b in again if b["kind"] == "ripple")
    assert ring_a["paint"] is ring_b["paint"]
    assert ring_a["color"] is ring_b["color"] or ring_a["color"] == ring_b["color"]
    # 样式对象更换（编辑后）后缓存不串：新解析、值正确。
    edited = Style(
        font_size_px=64,
        sing_fx="ripple",
        karaoke_anim="none",
        fx_particle_color_mode="follow_after",
        stroke_width_px=8,
        karaoke_colors=_karaoke_matrix("#40E0FF", "#112233"),
    )
    edited_ring = next(
        b
        for b in plan_line_bursts(edited, 0, 1000, 4000, 3900, windows)
        if b["kind"] == "ripple"
    )
    assert edited_ring["color"] == "#112233"
    clear_particle_paint_cache()


def test_painter_follow_mode_uses_each_chars_role(qapp):
    """回归护栏（2026-10）：CPU painter 必须把逐字角色样式传入规划器。

    曾出现 char_styles 已解析却未传给 plan_line_bursts 的漏洞——CPU 逐字
    粒子全回落行样式而 GPU 逐字生效，两端口径分歧。
    """
    from PyQt6.QtGui import QImage

    from krok_helper.subtitle_render.domain.models import SubtitleStyleScheme
    from krok_helper.subtitle_render.domain.paint import (
        KaraokeColorState,
        KaraokeColors,
    )
    from krok_helper.subtitle_render.domain.timing import TimingChar, TimingTrack
    from krok_helper.subtitle_render.engine.painter import paint_frame
    from krok_helper.subtitle_render.engine.render.effects.particles import (
        clear_particle_paint_cache,
    )

    clear_particle_paint_cache()
    grad_h = _gradient_fill("gradient_horizontal", "#FF8800", "#00FF88")
    scheme = SubtitleStyleScheme(
        karaoke_colors=KaraokeColors(
            after=KaraokeColorState(text=grad_h)
        )
    )
    first = TimingChar("あ", 500)
    first.role_label = "主唱"
    track = TimingTrack(
        lines=[TimingLine(chars=[first, TimingChar("い", 1200)], end_ms=1800)]
    )
    style = Style(
        font_size_px=64,
        sing_fx="twinkle",
        karaoke_anim="utopia",
        fx_particle_size_em=0.8,
        fx_particle_count=16,
        fx_particle_color_mode="follow_after",
        custom_style_schemes={"主唱": scheme},
        karaoke_colors=KaraokeColors(
            # 行样式走字后 = 纯红：不产生绿端像素。
            after=KaraokeColorState(
                text=_gradient_fill("solid", "#FF0000", "#FF0000")
            )
        ),
    )
    img = QImage(800, 450, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(0xFF101010)
    paint_frame(img, track, 700, style)
    # 主唱字在行**左端**：行级横向映射下采样到渐变的橙端（#FF8800）；
    # 行样式（纯红 #FF0000）不可能产生橙——橙像素存在即证明逐字角色
    # 样式 + 行内位置采样同时生效（2026-10 口径）。
    orangeish = 0
    for y in range(0, img.height(), 2):
        for x in range(0, img.width(), 2):
            px = img.pixel(x, y)
            r, g, b = (px >> 16) & 0xFF, (px >> 8) & 0xFF, px & 0xFF
            if r > 170 and 80 < g < 160 and b < 90:
                orangeish += 1
    assert orangeish > 10, f"逐字角色粒子未生效，橙端像素 {orangeish}"
    clear_particle_paint_cache()


def test_painter_horizontal_gradient_maps_to_line_span(qapp):
    """横向渐变的行级映射（2026-10 用户实测 bug 的回归护栏）。

    文字的横向渐变以整条显示行为跨度（n3_main_fill_rect 行墨迹并集 /
    GPU fillBounds）；粒子若在自身小框里重走整段渐变，观感与行配色完全
    不同。修复后每颗粒子按其在行内的横向位置采样实心色——左端字=起点
    色、右端字=终点色，粒子群整体还原行级色带。
    """
    from PyQt6.QtGui import QImage

    from krok_helper.subtitle_render.domain.models import SubtitleStyleScheme
    from krok_helper.subtitle_render.domain.paint import (
        KaraokeColorState,
        KaraokeColors,
        PaintFill,
    )
    from krok_helper.subtitle_render.domain.timing import TimingChar, TimingTrack
    from krok_helper.subtitle_render.engine.painter import paint_frame
    from krok_helper.subtitle_render.engine.render.effects.particles import (
        clear_particle_paint_cache,
    )

    clear_particle_paint_cache()
    grad = PaintFill(
        mode="gradient_horizontal",
        start_color="#FF0000",
        end_color="#0000FF",
        gradient_stops=[(0, "#FF0000"), (100, "#0000FF")],
    )
    dark = PaintFill(mode="solid", color="#202020")
    scheme = SubtitleStyleScheme(
        karaoke_colors=KaraokeColors(
            after=KaraokeColorState(text=grad)
        )
    )
    chars = [TimingChar("詞", 1000 + i * 400) for i in range(6)]
    for char in chars:
        char.role_label = "主唱"
    track = TimingTrack(
        lines=[TimingLine(chars=chars, end_ms=1000 + 6 * 400)]
    )
    style = Style(
        font_size_px=64,
        sing_fx="twinkle",
        karaoke_anim="none",
        fx_particle_size_em=0.7,
        fx_particle_count=24,
        fx_particle_color_mode="role",
        fx_particle_role_name="主唱",
        custom_style_schemes={"主唱": scheme},
        stroke_width_px=0,
        karaoke_colors=KaraokeColors(
            before=KaraokeColorState(text=dark),
            after=KaraokeColorState(text=dark),
        ),
    )
    img = QImage(800, 450, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(0xFF101010)
    paint_frame(img, track, 2600, style)

    def half_counts(left: bool):
        red = blue = 0
        xs = range(0, img.width() // 2) if left else range(
            img.width() // 2, img.width()
        )
        for y in range(0, img.height(), 2):
            for x in xs:
                px = img.pixel(x, y)
                r, g, b = (px >> 16) & 0xFF, (px >> 8) & 0xFF, px & 0xFF
                if r > 100 and r > g + 30 and r > b + 30:
                    red += 1
                if b > 100 and b > r + 30 and b > g + 30:
                    blue += 1
        return red, blue

    left_red, left_blue = half_counts(True)
    right_red, right_blue = half_counts(False)
    # 左半 = 起点红、右半 = 终点蓝，两侧互不越界（行级一条色带）。
    assert left_red > 10 and left_blue == 0, (left_red, left_blue)
    assert right_blue > 10 and right_red == 0, (right_red, right_blue)
    clear_particle_paint_cache()
