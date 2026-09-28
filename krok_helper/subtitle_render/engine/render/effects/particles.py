"""入场/退场/唱字装饰粒子：sprite 常量、burst 规划器与轨迹求值器。

设计约束（与 C++ ``d2d_backend_render.cpp`` 的粒子求值互为镜像）：

- 所有随机量经 :func:`transitions.fx_unit_hash` 的同源整数哈希派生，轨迹是
  时间的纯函数（无状态累积），QPainter 与 D2D 两条后端逐帧一致、导出可复现。
- burst 由 Python 侧规划（``plan_line_bursts`` 同一函数供渲染 IR 与 Painter
  调用），锚点坐标由各后端按自身布局解析（行中心 / 字符 pivot），布局一致性
  由既有双后端布局奇偶校验保证。
- sprite 轮廓是本模块定义的常量（M/L/C/Q/Z，1000 单位 em 空间、以 (0,0) 为
  中心），随场景 IR 下发（``fx_sprites``），C++ 不内置副本——单一事实源。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from krok_helper.subtitle_render.domain.models import Style
from krok_helper.subtitle_render.engine.render.elements.horizontal.transitions import (
    fx_unit_hash,
)

# 编排时长（ms）；改动需与 C++ 镜像同步。
SPARKLE_ENTRY_LIFE_MS = 700
SPARKLE_EXIT_LIFE_MS = 600
RIPPLE_LIFE_MS = 600
# 水波纹每个落点的圈数与错峰间隔（Aegisub/AE 同款：细环 2-3 圈先后扩散）。
RIPPLE_RING_COUNT = 3
RIPPLE_RING_STAGGER_MS = 150
# 唱字粒子的发射窗 + 最长寿命：不受字符唱字窗口约束（用户口径：在正确
# 时间正确地点发射、以合适的感觉播完即可），保证 twinkle/note 完整走完
# 轨迹而不是在换字瞬间被硬截断。
SING_TWINKLE_TOTAL_MS = 700
SING_NOTE_TOTAL_MS = 800
# 涟漪每个字的逐字错峰（与 char_fade / 几何入退场的 350ms 编排同构）。
RIPPLE_CHAR_STAGGER_MS = 350
# 粒子拼接/消散（assemble / dissolve）：每字飞入/飞离的行程与寿命。
ASSEMBLE_TRAVEL_EM = 2.2
ASSEMBLE_LIFE_MS = 320
# 出入场动画驱动的粒子（星光/涟漪/音符/拼接/消散）使用固定默认档
#（用户口径：粒子旋钮只影响唱字装饰粒子）。
ANIM_PARTICLE_SIZE_EM = 0.40
ANIM_PARTICLE_COUNT = 14
ANIM_PARTICLE_COLOR = "#FFFFFF"
# sparkle 整句扫过的额外错峰时长（按粒子横向位置从一端排到另一端）。
SPARKLE_SWEEP_MS = 350


# ---------------------------------------------------------------------------
# sprite 轮廓常量（±500 em 空间、中心原点；even-odd 填充口径）
# ---------------------------------------------------------------------------

def _circle_commands(
    cx: float, cy: float, radius: float, *, flip: bool = False
) -> list[list[object]]:
    """以 4 段三次贝塞尔近似圆（kappa=0.552284），返回 M/C/Z 命令列表。

    ``flip`` 反转绕向：光环 sprite 的内圆与外圆反向，使轮廓在 nonzero
    winding（D2D ``vectorGlyphGeometry`` 固定 WINDING）与 even-odd
    （QPainterPath 默认）两种填充规则下都呈圆环。
    """
    sign = -1.0 if flip else 1.0
    kappa = 0.552284
    commands: list[list[object]] = [["M", cx + radius, cy]]
    for k in range(4):
        a0 = k * (math.pi / 2.0) * sign
        a1 = (k + 1) * (math.pi / 2.0) * sign
        p0 = (cx + radius * math.cos(a0), cy + radius * math.sin(a0))
        p1 = (cx + radius * math.cos(a1), cy + radius * math.sin(a1))
        # 切向随绕向同步翻转（sign）：反向绕行时控制点必须也换到行进方向
        # 另一侧，否则四段贝塞尔外翻、孔洞被挤成菱形。
        t0 = (-math.sin(a0) * sign, math.cos(a0) * sign)
        t1 = (-math.sin(a1) * sign, math.cos(a1) * sign)
        commands.append([
            "C",
            p0[0] + radius * kappa * t0[0], p0[1] + radius * kappa * t0[1],
            p1[0] - radius * kappa * t1[0], p1[1] - radius * kappa * t1[1],
            p1[0], p1[1],
        ])
    commands.append(["Z"])
    return commands


def _star4_commands() -> list[list[object]]:
    """四角星光：外径 500、内控点 0.11，四段二次贝塞尔凹边。"""
    inner = 55.0
    return [
        ["M", 0.0, -500.0],
        ["Q", inner, -inner, 500.0, 0.0],
        ["Q", inner, inner, 0.0, 500.0],
        ["Q", -inner, inner, -500.0, 0.0],
        ["Q", -inner, -inner, 0.0, -500.0],
        ["Z"],
    ]


def _note_commands() -> list[list[object]]:
    """八分音符剪影（单轮廓，无自交叠；装饰用途，28px 级别足够可读）。"""
    return [
        ["M", -260.0, 300.0],
        # 音头下半椭圆（中心 -90,300，rx 170 ry 120）
        ["C", -260.0, 366.0, -178.0, 420.0, -90.0, 420.0],
        ["C", -2.0, 420.0, 80.0, 366.0, 80.0, 300.0],
        # 符干右缘上行
        ["L", 80.0, -380.0],
        # 符尾外弧
        ["C", 200.0, -330.0, 260.0, -180.0, 205.0, -30.0],
        ["L", 135.0, -110.0],
        ["C", 130.0, -220.0, 110.0, -290.0, 30.0, -330.0],
        # 符干左缘下行
        ["L", 30.0, 300.0],
        # 音头上半椭圆
        ["C", 30.0, 234.0, -2.0, 180.0, -90.0, 180.0],
        ["C", -178.0, 180.0, -260.0, 234.0, -260.0, 300.0],
        ["Z"],
    ]


def _ring_commands() -> list[list[object]]:
    """水波纹环：外径 490 / 内径 462 的细描边圆环（厚约 2.8% em，60px 级
    直径下 ≈1.7px 发丝线）；内圆反向绕行（见 _circle_commands）。"""
    return (
        _circle_commands(0.0, 0.0, 490.0)
        + _circle_commands(0.0, 0.0, 462.0, flip=True)
    )


def _sprite_ir(commands: list[list[object]]) -> dict[str, object]:
    return {
        "path_commands": [list(command) for command in commands],
        "units_per_em": 1000,
        "advance_width": 1000,
    }


def _pixel_commands() -> list[list[object]]:
    """像素风方块：±180 em 方形（0.36em 边长），拼接/消散粒子的形体。"""
    return [
        ["M", -180.0, -180.0],
        ["L", 180.0, -180.0],
        ["L", 180.0, 180.0],
        ["L", -180.0, 180.0],
        ["Z"],
    ]


FX_SPRITES: dict[str, dict[str, object]] = {
    "star4": _sprite_ir(_star4_commands()),
    "ring": _sprite_ir(_ring_commands()),
    "note": _sprite_ir(_note_commands()),
    "pixel": _sprite_ir(_pixel_commands()),
}

_SPRITE_FOR_KIND = {
    "sparkle": "star4",
    "ripple": "ring",
    "twinkle": "star4",
    "note": "note",
    "assemble": "pixel",
    "dissolve": "pixel",
}


def sprite_for_kind(kind: str) -> str:
    return _SPRITE_FOR_KIND.get(kind, "star4")


# ---------------------------------------------------------------------------
# burst 规划：同一函数供渲染 IR（native/protocol）与 QPainter 调用
# ---------------------------------------------------------------------------

def plan_line_bursts(
    style: Style,
    line_index: int,
    display_start_ms: int | None,
    display_end_ms: int | None,
    line_end_ms: int | None,
    char_windows: list[tuple[int, int]],
    *,
    char_visible: list[bool] | None = None,
) -> list[dict[str, object]]:
    """把 style 的粒子档位规划成逐行 burst 列表（IR-ready dict）。

    时间锚与入退场动画同源：入场自显示窗口起点，退场自
    ``max(行末, 显示末-600)`` 回溯；涟漪光环逐字跟随（每字一枚同心水波
    环，按 char_fade 的 350ms 错峰节奏）；唱字自各字符唱字窗起点发射、
    但窗口延伸到轨迹自然播完（不受唱字窗约束）。
    尺寸语义：``fx_particle_size_em`` 为相对主字号的 em 比例（0.40 =
    40% 字号），此处按全局字号折算成像素下发，两后端同值。
    ``line_index`` 参与种子，保证同曲目每行轨迹不同且重开可复现。
    ``char_visible``（与 char_windows 等长）：False = 空白字符（空格等
    无字形内容）——唱字粒子跳过（无走字内容），出入场仍整行参与。
    """
    bursts: list[dict[str, object]] = []
    count = max(2, min(64, int(style.fx_particle_count)))
    font_px = max(float(getattr(style, "font_size_px", 0.0) or 0.0), 1.0)
    size = font_px * max(0.05, min(2.0, float(getattr(style, "fx_particle_size_em", 0.40))))
    color = str(style.fx_particle_color)
    seed_base = ((int(line_index) & 0xFFFF) * 0x9E37) & 0xFFFFFFFF
    char_count = len(char_windows)
    stagger = (
        RIPPLE_CHAR_STAGGER_MS // (char_count - 1) if char_count > 1 else 0
    )
    # 出入场动画粒子：固定默认档（不受粒子旋钮影响）；但编排窗口随
    # 「入场/退场动画时长」旋钮等比缩放（0 = 默认 600ms 基准）。
    anim_size = font_px * ANIM_PARTICLE_SIZE_EM
    anim_count = ANIM_PARTICLE_COUNT
    anim_color = ANIM_PARTICLE_COLOR

    def _duration_scale(configured: int) -> float:
        total = min(max(int(configured), 120), 3000)
        return total / 600.0

    # 旧语义：时长 0 = 无动画（粒子同样不发）。
    entry_active = int(getattr(style, "entry_lead_ms", 0) or 0) > 0
    exit_active = int(getattr(style, "exit_fade_ms", 0) or 0) > 0
    entry_scale = _duration_scale(
        int(getattr(style, "entry_lead_ms", 0) or 0)
    )
    exit_scale = _duration_scale(int(getattr(style, "exit_fade_ms", 0) or 0))

    def _char_ripples(
        base_ms: int,
        seed_offset: int,
        *,
        stagger_ms: int,
        ring_size: float | None = None,
        ring_color: str | None = None,
        scale: float = 1.0,
    ) -> None:
        px = anim_size * 3.6 if ring_size is None else ring_size
        paint = anim_color if ring_color is None else ring_color
        for char_index in range(char_count):
            start = int(base_ms) + int(stagger_ms * scale) * char_index
            bursts.append({
                "kind": "ripple", "anchor": "char",
                "char_index": int(char_index),
                "start_ms": start,
                "end_ms": start + int(RIPPLE_LIFE_MS * scale),
                "count": RIPPLE_RING_COUNT,
                "seed": (seed_base + seed_offset + char_index) & 0xFFFFFFFF,
                # 水波纹基准直径 ≈ 1.4× 字高，扩散峰值约 1.6× 字高。
                "size_px": px, "travel_px": 0.0, "front": False,
                "sweep": 0, "color": paint,
            })

    entry_anim = str(getattr(style, "entry_anim", "none") or "none")
    if entry_anim == "sparkle" and entry_active and display_start_ms is not None:
        bursts.append({
            "kind": "sparkle", "anchor": "line", "char_index": -1,
            "start_ms": int(display_start_ms),
            "end_ms": int(display_start_ms)
            + int(SPARKLE_ENTRY_LIFE_MS * entry_scale),
            "count": anim_count, "seed": (seed_base + 1) & 0xFFFFFFFF,
            "size_px": anim_size, "travel_px": anim_size * 3.2, "front": True,
            "sweep": 1, "color": anim_color,
        })
    elif entry_anim == "ripple" and entry_active and display_start_ms is not None:
        _char_ripples(
            int(display_start_ms), 2, stagger_ms=stagger, scale=entry_scale
        )
    elif entry_anim == "note" and entry_active and display_start_ms is not None:
        for char_index in range(char_count):
            start = int(display_start_ms) + int(stagger * entry_scale) * char_index
            bursts.append({
                "kind": "note", "anchor": "char",
                "char_index": int(char_index),
                "start_ms": start,
                "end_ms": start + int(SING_NOTE_TOTAL_MS * entry_scale),
                "count": 3, "seed": (seed_base + 6 + char_index) & 0xFFFFFFFF,
                "size_px": anim_size, "travel_px": anim_size * 1.8,
                "front": True, "sweep": 0, "color": anim_color,
            })

    exit_anim = str(getattr(style, "exit_anim", "none") or "none")
    if exit_anim == "sparkle" and exit_active and display_end_ms is not None:
        exit_start = max(
            int(line_end_ms) if line_end_ms is not None else 0,
            int(display_end_ms) - int(SPARKLE_EXIT_LIFE_MS * exit_scale),
        )
        if int(display_end_ms) - exit_start < 120:
            exit_start = int(display_end_ms) - 120
        bursts.append({
            "kind": "sparkle", "anchor": "line", "char_index": -1,
            "start_ms": exit_start, "end_ms": int(display_end_ms),
            "count": anim_count, "seed": (seed_base + 3) & 0xFFFFFFFF,
            "size_px": anim_size, "travel_px": anim_size * 3.2, "front": True,
            "sweep": -1, "color": anim_color,
        })
    elif exit_anim == "ripple" and exit_active and display_end_ms is not None:
        exit_start = max(
            int(line_end_ms) if line_end_ms is not None else 0,
            int(display_end_ms)
            - int((RIPPLE_CHAR_STAGGER_MS + RIPPLE_LIFE_MS) * exit_scale),
        )
        if int(display_end_ms) - exit_start < 120:
            exit_start = int(display_end_ms) - 120
        # 尾窗不足时压缩逐字错峰，尽量让全部水波环在行消失前完整播完。
        tail_ms = int(display_end_ms) - exit_start - int(RIPPLE_LIFE_MS * exit_scale)
        stagger_exit = min(
            int(stagger * exit_scale),
            max(0, tail_ms) // max(1, char_count - 1),
        )
        _char_ripples(exit_start, 4, stagger_ms=stagger_exit, scale=exit_scale)
    elif exit_anim == "note" and exit_active and display_end_ms is not None:
        exit_start = max(
            int(line_end_ms) if line_end_ms is not None else 0,
            int(display_end_ms)
            - int((RIPPLE_CHAR_STAGGER_MS + SING_NOTE_TOTAL_MS) * exit_scale),
        )
        if int(display_end_ms) - exit_start < 120:
            exit_start = int(display_end_ms) - 120
        tail_ms = (
            int(display_end_ms) - exit_start
            - int(SING_NOTE_TOTAL_MS * exit_scale)
        )
        stagger_exit = min(
            int(stagger * exit_scale),
            max(0, tail_ms) // max(1, char_count - 1),
        )
        for char_index in range(char_count):
            start = exit_start + stagger_exit * char_index
            bursts.append({
                "kind": "note", "anchor": "char",
                "char_index": int(char_index),
                "start_ms": start,
                "end_ms": start + int(SING_NOTE_TOTAL_MS * exit_scale),
                "count": 3, "seed": (seed_base + 8 + char_index) & 0xFFFFFFFF,
                "size_px": anim_size, "travel_px": anim_size * 1.8,
                "front": True, "sweep": 0, "color": anim_color,
            })

    # 粒子拼接/消散：像素风方块粒子，固定默认档（不吃粒子旋钮）。
    per_char_assemble = max(4, ANIM_PARTICLE_COUNT // 2)
    if entry_anim == "assemble_in" and entry_active and display_start_ms is not None:
        for char_index in range(char_count):
            start = int(display_start_ms) + int(stagger * entry_scale) * char_index
            bursts.append({
                "kind": "assemble", "anchor": "char",
                "char_index": int(char_index),
                "start_ms": start,
                "end_ms": start + int(ASSEMBLE_LIFE_MS * entry_scale),
                "count": per_char_assemble,
                "seed": (seed_base + 7 + char_index) & 0xFFFFFFFF,
                "size_px": anim_size * 0.75,
                "travel_px": font_px * ASSEMBLE_TRAVEL_EM,
                "front": True, "sweep": 0, "color": color,
            })
    if exit_anim == "dissolve_out" and exit_active and display_end_ms is not None:
        exit_start_assemble = max(
            int(line_end_ms) if line_end_ms is not None else 0,
            int(display_end_ms)
            - int((RIPPLE_CHAR_STAGGER_MS + ASSEMBLE_LIFE_MS) * exit_scale),
        )
        tail = (
            int(display_end_ms) - exit_start_assemble
            - int(ASSEMBLE_LIFE_MS * exit_scale)
        )
        stagger_dissolve = min(
            int(stagger * exit_scale),
            max(0, tail) // max(1, char_count - 1),
        )
        for char_index in range(char_count):
            start = exit_start_assemble + stagger_dissolve * char_index
            bursts.append({
                "kind": "dissolve", "anchor": "char",
                "char_index": int(char_index),
                "start_ms": start,
                "end_ms": start + int(ASSEMBLE_LIFE_MS * exit_scale),
                "count": per_char_assemble,
                "seed": (seed_base + 9 + char_index) & 0xFFFFFFFF,
                "size_px": anim_size * 0.75,
                "travel_px": font_px * ASSEMBLE_TRAVEL_EM,
                "front": True, "sweep": 0, "color": color,
            })

    # 唱字装饰粒子：唯一吃粒子旋钮（尺寸/数量/颜色）的档位。
    if style.sing_fx == "ripple":
        for char_index, (start_ms, end_ms) in enumerate(char_windows):
            duration = int(end_ms) - int(start_ms)
            if duration <= 0:
                continue
            if char_visible is not None and not char_visible[char_index]:
                continue
            bursts.append({
                "kind": "ripple", "anchor": "char",
                "char_index": int(char_index),
                "start_ms": int(start_ms),
                "end_ms": int(start_ms) + RIPPLE_LIFE_MS,
                "count": RIPPLE_RING_COUNT,
                "seed": (seed_base + 5 + char_index) & 0xFFFFFFFF,
                "size_px": size * 3.6, "travel_px": 0.0, "front": False,
                "sweep": 0, "color": color,
            })
    elif style.sing_fx in ("twinkle", "note"):
        per_char = max(3, count // 4) if style.sing_fx == "twinkle" else 3
        total = SING_TWINKLE_TOTAL_MS if style.sing_fx == "twinkle" else SING_NOTE_TOTAL_MS
        for char_index, (start_ms, end_ms) in enumerate(char_windows):
            duration = int(end_ms) - int(start_ms)
            if duration <= 0:
                continue
            if char_visible is not None and not char_visible[char_index]:
                continue
            seed = (seed_base + 5 + char_index) & 0xFFFFFFFF
            bursts.append({
                # 叠加在正在唱的那个字上（用户口径）：锚点=该字符自身
                # 中心；发射时刻=该字符唱字窗起点，窗口延伸到轨迹自然
                # 播完（换字后余韵继续，不硬截断）。
                "kind": style.sing_fx, "anchor": "char",
                "char_index": int(char_index),
                "start_ms": int(start_ms),
                "end_ms": int(start_ms) + total,
                "count": per_char,
                "seed": seed,
                "size_px": size, "travel_px": size * 1.8, "front": True,
                "sweep": 0, "color": color,
            })
    return bursts


# ---------------------------------------------------------------------------
# 轨迹求值：t 的纯函数（Python/C++ 镜像；全部 double 运算）
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ParticleDraw:
    """一颗粒子在 t 时刻的绘制状态。"""

    x: float
    y: float
    rotation_deg: float
    size_px: float
    alpha: float


def burst_particles_at(
    burst: dict[str, object],
    t_ms: int,
    origin_x: float,
    origin_y: float,
    box_w: float,
    box_h: float,
) -> list[ParticleDraw]:
    """求值一个 burst 在 ``t_ms`` 的全部粒子。窗口外返回空。

    ``origin_x/origin_y``：burst 锚点（行中心或字符中心，调用方布局解析）。
    ``box_w/box_h``：锚点盒（twinkle 的散布范围 / note 的上升行程参照）。
    """
    start_ms = int(burst["start_ms"])
    end_ms = int(burst["end_ms"])
    if t_ms < start_ms or t_ms > end_ms:
        return []
    kind = str(burst["kind"])
    seed = int(burst["seed"])
    size = float(burst["size_px"])
    travel = float(burst["travel_px"])
    sweep = int(burst.get("sweep", 0) or 0)
    life = max(end_ms - start_ms, 1)
    tau = float(t_ms - start_ms)
    out: list[ParticleDraw] = []

    if kind == "ripple":
        # 水波纹（Aegisub/AE 同款口径）：每个落点 3 枚细描边圆环错峰扩散
        # （第 i 环延迟 150ms），各自「自小圈快速扩开 → 接近满径时淡出」，
        # 呈现一石入水的连漪感；环体是发丝线，不是实心铜钱。
        for i in range(max(1, int(burst["count"]))):
            delay = float(i) * RIPPLE_RING_STAGGER_MS
            life_i = max(life - delay, 1.0)
            p = min(max((tau - delay) / life_i, 0.0), 1.0)
            if p <= 0.0:
                continue
            eased = 1.0 - (1.0 - p) * (1.0 - p)
            scale = size * (0.25 + 0.85 * eased)
            out.append(ParticleDraw(
                origin_x, origin_y, 0.0, scale, (1.0 - p) * (1.0 - p),
            ))
        return out

    for i in range(int(burst["count"])):
        if kind == "sparkle":
            # 整句扫过：粒子伪随机铺满整行宽度（box_w），入场从左到右、退场
            # 反向，按横向位置错峰出生 → 连续顺滑地跟随扫过整句；每颗随机
            # 初始/末了大小、随机向上漂移方向。
            u1 = fx_unit_hash(seed + i, 1)
            u2 = fx_unit_hash(seed + i, 2)
            u3 = fx_unit_hash(seed + i, 3)
            u4 = fx_unit_hash(seed + i, 4)
            u5 = fx_unit_hash(seed + i, 5)
            x_norm = u1
            if sweep > 0:
                delay = x_norm * SPARKLE_SWEEP_MS + u5 * 90.0
            elif sweep < 0:
                delay = (1.0 - x_norm) * SPARKLE_SWEEP_MS + u5 * 90.0
            else:
                delay = u5 * 130.0
            denom = max(life - delay, 1.0)
            p = min(max((tau - delay) / denom, 0.0), 1.0)
            if p <= 0.0:
                continue
            size_i = size * (0.65 + 0.70 * u3)
            size_f = size_i * (0.15 + 0.30 * u4)
            scale = size_i + (size_f - size_i) * p
            drift_x = (u4 - 0.5) * travel * 0.35 * p
            drift_y = -(0.30 + 0.70 * u2) * travel * 0.45 * p
            out.append(ParticleDraw(
                origin_x + (x_norm - 0.5) * box_w + drift_x,
                origin_y + (u2 - 0.5) * box_h * 0.55 + drift_y,
                u3 * 90.0 + 60.0 * p,
                scale,
                1.0 - p,
            ))
        elif kind == "twinkle":
            # 唱字星光：位置铺满整个字框、随机旋转、随机生命周期与峰值大小。
            u1 = fx_unit_hash(seed + i, 1)
            u2 = fx_unit_hash(seed + i, 2)
            u3 = fx_unit_hash(seed + i, 3)
            u4 = fx_unit_hash(seed + i, 4)
            life_i = 300.0 + 150.0 * u4
            spread = max(life - life_i, 0.0)
            delay = u3 * spread * 0.8
            p = min(max((tau - delay) / life_i, 0.0), 1.0)
            if p <= 0.0 or p >= 1.0:
                continue
            envelope = math.sin(math.pi * p)
            peak = size * (0.65 + 0.70 * u4)
            out.append(ParticleDraw(
                origin_x + (u1 - 0.5) * box_w * 0.95,
                origin_y + (u2 - 0.5) * box_h * 0.85,
                u3 * 120.0 - 30.0,
                peak * (0.30 + 0.70 * envelope),
                envelope,
            ))
        elif kind == "assemble":
            # 粒子拼接：每颗粒子自随机方向的远端飞向字形内随机落点，
            # 平滑抵达后缩小熄灭（Trapcode 汇聚语言）。
            u1 = fx_unit_hash(seed + i, 1)
            u2 = fx_unit_hash(seed + i, 2)
            u3 = fx_unit_hash(seed + i, 3)
            u4 = fx_unit_hash(seed + i, 4)
            delay = u3 * 90.0
            p = min(max((tau - delay) / max(life - delay, 1.0), 0.0), 1.0)
            if p <= 0.0 or p >= 1.0:
                continue
            eased = p * p * (3.0 - 2.0 * p)
            land_x = (u1 - 0.5) * box_w * 0.9
            land_y = (u4 - 0.5) * box_h * 0.8
            theta = u2 * 2.0 * math.pi
            speed = 0.6 + 0.5 * u4
            start_x = land_x + math.cos(theta) * travel * speed
            start_y = land_y + math.sin(theta) * travel * speed * 0.7
            out.append(ParticleDraw(
                origin_x + start_x + (land_x - start_x) * eased,
                origin_y + start_y + (land_y - start_y) * eased,
                (u3 - 0.5) * 240.0 * (1.0 - eased),
                size * (0.9 - 0.35 * eased),
                min(p * 6.0, 1.0) * (1.0 - p * p),
            ))
        elif kind == "dissolve":
            # 粒子消散：粒子自字形内随机起点飞向随机方向远端并淡出。
            u1 = fx_unit_hash(seed + i, 1)
            u2 = fx_unit_hash(seed + i, 2)
            u3 = fx_unit_hash(seed + i, 3)
            u4 = fx_unit_hash(seed + i, 4)
            delay = u3 * 70.0
            p = min(max((tau - delay) / max(life - delay, 1.0), 0.0), 1.0)
            if p <= 0.0:
                continue
            eased = p * p * (3.0 - 2.0 * p)
            start_x = (u1 - 0.5) * box_w * 0.9
            start_y = (u4 - 0.5) * box_h * 0.8
            theta = u2 * 2.0 * math.pi
            speed = 0.6 + 0.5 * u4
            out.append(ParticleDraw(
                origin_x + start_x + math.cos(theta) * travel * speed * eased,
                origin_y + start_y + math.sin(theta) * travel * speed * 0.7 * eased,
                (u3 - 0.5) * 240.0 * eased,
                size * (0.75 + 0.25 * (1.0 - eased)),
                (1.0 - p) * (1.0 - p * 0.5),
            ))
        elif kind == "note":
            # 音符：出生点在字框顶部随机横移，上升带随机摆动，随机大小。
            u1 = fx_unit_hash(seed + i, 1)
            u2 = fx_unit_hash(seed + i, 2)
            u3 = fx_unit_hash(seed + i, 3)
            delay = u3 * life * 0.35
            p = min(max((tau - delay) / max(life - delay, 1.0), 0.0), 1.0)
            if p <= 0.0:
                continue
            rise = travel * (1.0 - (1.0 - p) * (1.0 - p))
            sway = math.sin((u2 + p * 1.5) * 2.0 * math.pi) * size * 0.4
            out.append(ParticleDraw(
                origin_x + (u1 - 0.5) * box_w * 0.8 + sway,
                origin_y - box_h * 0.35 - rise,
                (u2 - 0.5) * 28.0,
                size * (0.70 + 0.60 * u1) * (0.85 + 0.15 * math.sin(math.pi * p)),
                1.0 - p * math.sqrt(p),
            ))
    return out
