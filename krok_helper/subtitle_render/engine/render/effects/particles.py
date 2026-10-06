"""入场/退场/唱字装饰粒子：sprite 常量、burst 规划器与轨迹求值器。

设计约束（与 C++ ``d2d_backend_render.cpp`` 的粒子求值互为镜像）：

- 所有随机量经 :func:`transitions.fx_unit_hash` 的同源整数哈希派生，轨迹是
  时间的纯函数（无状态累积），QPainter 与 D2D 两条后端逐帧一致、导出可复现。
- burst 由 Python 侧规划（``plan_line_bursts`` 同一函数供渲染 IR 与 Painter
  调用），锚点坐标由各后端按自身布局解析（行中心 / 字符 pivot），布局一致性
  由既有双后端布局奇偶校验保证。
- sprite 轮廓是本模块定义的常量（M/L/C/Q/Z，1000 单位 em 空间、以 (0,0) 为
  中心），随场景 IR 下发（``fx_sprites``），C++ 不内置副本——单一事实源。
- 粒子颜色模式（默认颜色·樱花粉双色 / 单独颜色·双色槽 / 跟随字体·
  前后各一 / 跟随字体·走字前后 / 复用配色方案）在规划期解析成每 burst
  的实色（#RRGGBB）下发，两条后端按 burst 颜色实心绘制、天然同色；
  双色档由规划器拆成两条同轨迹、不同种子/半数量的 burst 实现「每颗粒
  子随机取一色」，渲染端无特殊分支；单色跟随档（走字前/后、role）另带
  完整 PaintFill 装饰规格（与音符「跟随字体」同源）。
"""

from __future__ import annotations

import math
from collections import OrderedDict
from dataclasses import dataclass

from krok_helper.subtitle_render.domain.models import (
    Style,
    normalize_glow_concentration_level,
)
from krok_helper.subtitle_render.domain.paint import KaraokeColorState, PaintFill
from krok_helper.subtitle_render.engine.style.style_semantics import (
    appearance_role_source,
    effective_karaoke_colors,
)
from krok_helper.subtitle_render.serialization.paint import paint_fill_to_dict
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
# 花瓣（樱花飘落，2026-10）：飘入/飘散/飘动三档共用一条「\moves4 四点飘移」
# 等效轨迹（sin 横向摇摆 + 单调竖直行程 + 连续翻转）。位置偏移全部取
# box_w/box_h 比例（星光/音符同约定），不带字号倍数行程（2026-10 用户
# 口径：初版 travel=1.6em 出生过高、飘进行间距）。
PETAL_LIFE_MS = 900
# 「默认颜色」档的两种樱花粉：规划器拆双 burst 随机混发（每颗粒子一色）。
SAKURA_PINK_A = "#FFB7C5"
SAKURA_PINK_B = "#FFD7E0"
# 雪花（2026-10，与花瓣同管线）：六角星 sprite + 慢速匀速飘落的运动签名
#（宽幅低频摇摆、慢自转、柔包络——AE CC Snowfall 等参考的共同观感）；
# 寿命比花瓣长（下落更慢）。
SNOW_LIFE_MS = 1100
# 「默认颜色」档的雪花双色（雪白 + 冰蓝）随机混发；固定默认档按 kind
# 取默认对（雪花=雪色，花瓣与其余 kind=樱花粉，见 _default_color_pair）。
SNOW_A = "#F2F8FF"
SNOW_B = "#BFDFFF"


def _default_color_pair(snow: bool) -> tuple[str, str]:
    """按 kind 的默认双色（固定默认档与「默认颜色」模式共用）：雪花=雪白
    +冰蓝；花瓣与其余 kind 维持樱花粉（b5a1b7fb 用户口径不回归）。"""

    return (SNOW_A, SNOW_B) if snow else (SAKURA_PINK_A, SAKURA_PINK_B)
# 出入场动画驱动的粒子（星光/涟漪/音符/花瓣/拼接/消散）默认用固定默认档：
# 樱花粉双色随机混发 + 40% 字号（2026-10 用户口径：出入场粒子用「默认
# 颜色」的樱花色，而不是单独颜色的默认白）；开启 ``fx_apply_to_entry_exit``
# 后颜色与尺寸改吃粒子旋钮（数量仍固定）。
ANIM_PARTICLE_SIZE_EM = 0.40
ANIM_PARTICLE_COUNT = 14
# sparkle 整句扫过的额外错峰时长（按粒子横向位置从一端排到另一端）。
SPARKLE_SWEEP_MS = 350
# 星光族（sparkle/twinkle）纵向锚点分布：以锚点中心为原点的行高比例。
# 上缘 -0.62（字形顶 = -0.5，只稍微溢出字上方 ≈12% 行高）、下缘 +0.30
#（都在字框内）；线性映射 → 约 2/3 概率在字上侧（2026-10 用户口径：
# 尽量多出现在主文字上侧）。u < STAR_Y_SPLIT_U 落上侧。
STAR_Y_TOP = -0.62
STAR_Y_SPAN = 0.92
STAR_Y_SPLIT_U = 0.62 / 0.92
# 连续星星禁止同象限：哈希通道 6/7 重采样仍撞象限时按上一颗的竖侧
# 强制换侧（上半段/下半段各留 ~1/3 余量，仍落在偏置分布内）。


def star_y_fraction(u: float) -> float:
    """u∈[0,1] → 纵向锚点比例 ∈ [STAR_Y_TOP, STAR_Y_TOP+SPAN]。"""

    return STAR_Y_TOP + STAR_Y_SPAN * u


def _star_quadrant(left: bool, top: bool) -> int:
    return (2 if left else 0) + (1 if top else 0)


def star_y_resample(u2: float, alt6: float, alt7: float, prev_q: int, left: bool) -> float:
    """象限去重：与上一颗同象限时重采样 y（镜像 C++ fxStarYResample）。"""

    if _star_quadrant(left, star_y_fraction(u2) < 0.0) != prev_q:
        return u2
    if _star_quadrant(left, star_y_fraction(alt6) < 0.0) != prev_q:
        return alt6
    if _star_quadrant(left, star_y_fraction(alt7) < 0.0) != prev_q:
        return alt7
    if prev_q & 1:  # 上一颗在上 → 取下半段
        return STAR_Y_SPLIT_U + (1.0 - STAR_Y_SPLIT_U) * alt6
    # 上一颗在下 → 取上半段
    return STAR_Y_SPLIT_U * alt7


# ---------------------------------------------------------------------------
# 颜色模式解析：仿扫字线的 color / follow_before / follow_after / role
# ---------------------------------------------------------------------------

def _parse_hex_color(value: str) -> tuple[int, int, int] | None:
    """Parse ``#RRGGBB`` / ``#AARRGGBB`` into RGB (alpha dropped)."""

    text = str(value or "").strip()
    if not text.startswith("#") or len(text) not in {7, 9}:
        return None
    digits = text[1:]
    if any(character not in "0123456789abcdefABCDEF" for character in digits):
        return None
    if len(digits) == 8:
        digits = digits[2:]
    return (
        int(digits[0:2], 16),
        int(digits[2:4], 16),
        int(digits[4:6], 16),
    )


def _average_hex_color(colors: list[str], fallback: str) -> str:
    """平均若干 #RRGGBB 得到一个实色（逐通道算术平均，非法色跳过）。"""

    parsed = [color for color in (_parse_hex_color(c) for c in colors) if color]
    if not parsed:
        return fallback
    red = sum(channel[0] for channel in parsed) // len(parsed)
    green = sum(channel[1] for channel in parsed) // len(parsed)
    blue = sum(channel[2] for channel in parsed) // len(parsed)
    return f"#{red:02X}{green:02X}{blue:02X}"


def fill_to_solid_color(fill: PaintFill, fallback: str) -> str:
    """把一份 PaintFill 折算成粒子可用的实色（#RRGGBB）。

    solid 直接取色；渐变/拼色取全部停止色的平均（sprite 是小尺寸实心
    剪影，平均色最接近整体观感）；图片填充取不到代表色，回退 fallback。
    """

    if fill.mode == "image":
        return fallback
    if fill.mode == "gradient_horizontal" or fill.mode == "gradient_vertical":
        stops = [color for _position, color in (fill.gradient_stops or [])]
        if not stops:
            stops = [fill.start_color, fill.end_color]
        return _average_hex_color(stops, fallback)
    if fill.mode == "split_vertical":
        stops = [color for _position, color in (fill.split_stops or [])]
        if not stops:
            stops = [fill.split_top_color, fill.split_bottom_color]
        return _average_hex_color(stops, fallback)
    return _average_hex_color([fill.color], fallback)


def _particle_color_state(
    style: Style,
    char_style: Style | None,
    mode: str,
) -> tuple[Style, KaraokeColorState] | None:
    """解析 (装饰来源样式, 走字前/后配色态)；``color`` 档与悬空引用为
    ``None``（调用方回退单独颜色）。

    ``follow_before`` / ``follow_after`` 用**当前字符**自己的角色方案
    （``char_style``，由调用方按 ``role_label`` 解析；整行锚点或未提供时
    回落行样式——歌手行的行样式已合并该歌手方案）；``role`` 用
    ``fx_particle_role_name`` 指定来源的走字后态（扫字线同口径）。
    """

    if mode == "follow_before":
        source = char_style if char_style is not None else style
        return source, effective_karaoke_colors(source).before
    if mode == "follow_after":
        source = char_style if char_style is not None else style
        return source, effective_karaoke_colors(source).after
    if mode == "role":
        resolved = appearance_role_source(
            style, getattr(style, "fx_particle_role_name", None)
        )
        if resolved is None:
            return None
        return resolved, effective_karaoke_colors(resolved).after
    return None


def particle_solid_color(style: Style, char_style: Style | None = None) -> str:
    """按 ``fx_particle_color_mode`` 解析粒子实色（行级口径，纯函数）。

    ``follow_before`` / ``follow_after`` 取**当前字符**角色方案（缺省回落
    行样式）有效配色的走字前/后「主文字」填充；``role`` 取
    ``fx_particle_role_name`` 指定来源（扫字线「复用配色方案」同口径）
    的「走字后-主文字」填充；填充折算见 :func:`fill_to_solid_color`。
    悬空引用 / 未知模式回退 ``color`` 档的 ``fx_particle_color``。
    """

    fallback = str(getattr(style, "fx_particle_color", "") or "#FFFFFF")
    mode = str(getattr(style, "fx_particle_color_mode", "color") or "color")
    resolved = _particle_color_state(style, char_style, mode)
    if resolved is None:
        return fallback
    _source, state = resolved
    return fill_to_solid_color(state.text, fallback)


# 取色层级（fx_particle_color_layers）：从来源配色态裁剪装饰层。
# solid=仅实色 / stroke=+描边（按方案的描边栈，不加装饰）/
# decor=+装饰（仅装饰层，不加描边）/ all=全有（描边栈 + 装饰层，完全按方案）。
PARTICLE_COLOR_LAYERS = ("solid", "stroke", "decor", "all")


def _state_paint_spec(
    source: Style,
    state: KaraokeColorState,
    size_px: float,
    *,
    layers: str,
    after: bool,
    include_strokes: bool,
    fallback: str,
) -> dict[str, object] | None:
    """把一个配色态按**取色层级**折成 burst 级装饰规格（IR dict）。

    ``layers``（``fx_particle_color_layers``，2026-10 用户口径）：

    - ``solid`` 仅实色——只有主文字色（返回 ``None``，调用方退纯色剪影）；
    - ``stroke`` +描边——**按角色方案的描边栈**（方案启用二重描边时一起
      加），**不加装饰**；
    - ``decor`` +装饰——**不加描边**、仅加装饰层；
    - ``all`` 全有——描边栈 + 装饰层，完全按角色方案。

    装饰层**不固定阴影**：按来源角色方案的 ``decoration_kind`` 二选一——

    - ``shadow``：行空间常量偏移的剪影（``offset_x/y_px`` 按 粒子尺寸/
      来源字号 同比缩放），与文字 ``paint_shadow_silhouette`` 同口径；
    - ``glow``：多级描边近似高斯弥散晕（``radius_px`` 同口径缩放 +
      ``concentration_level``，``after`` 选走字前/后半径），与 glow 转场
      的既有双端配方（``GLOW_HALO_STROKES``）同源；
    - ``none``：装饰层不产出（``decor`` 档退化为纯色，``all`` 档退化为
      +描边）。

    装饰色取该态的 ``shadow`` 槽（文字发光/阴影同用此槽）。描边宽度按
    粒子尺寸/该来源字号 同比缩放（上限半个粒子边长）。``include_strokes
    =False``（涟漪光环）描边/二重描边/装饰全部不取——环体是发丝线，叠
    装饰会显著变粗（2026-10 用户口径）。图片填充暂折为单独颜色（实色
    回退，两端一致）。
    """

    if layers not in PARTICLE_COLOR_LAYERS or layers == "solid":
        return None

    def _layer_fill(fill: PaintFill) -> dict[str, object]:
        if fill.mode == "image":
            # 图片填充没有跨端一致的笔刷映射（位图平移与粒子旋转变换
            # 复合两端不同），折成单独颜色实心。
            solid = PaintFill(
                mode="solid",
                color=fallback,
                start_color=fallback,
                end_color=fallback,
                gradient_stops=[(0, fallback), (100, fallback)],
                split_top_color=fallback,
                split_bottom_color=fallback,
                split_stops=[(0, fallback), (100, fallback)],
            )
            return paint_fill_to_dict(solid)
        return paint_fill_to_dict(fill)

    want_strokes = include_strokes and layers in {"stroke", "all"}
    want_decor = include_strokes and layers in {"decor", "all"}
    scale = float(size_px) / max(float(source.font_size_px or 0.0), 1.0)
    half = max(float(size_px) / 2.0, 1.0)
    if want_strokes:
        stroke_width = min(
            max(float(source.stroke_width_px or 0) * scale, 0.0), half
        )
    else:
        stroke_width = 0.0
    if want_strokes:
        stroke2_raw = (
            max(int(source.stroke2_width_px or 0), 0)
            if source.stroke2_enabled
            else 0
        )
        stroke2_width = min(max(stroke2_raw * scale, 0.0), half)
    else:
        stroke2_width = 0.0
    spec: dict[str, object] = {
        "fill": _layer_fill(state.text),
        "stroke": _layer_fill(state.stroke),
        "stroke2": _layer_fill(state.stroke2),
        "stroke_width_px": round(stroke_width, 3),
        "stroke2_width_px": round(stroke2_width, 3),
    }
    if want_decor:
        # 装饰层随来源角色方案的 decoration_kind 二选一（2026-10 用户口径：
        # 不是固定阴影）——shadow=偏移剪影；glow=多级描边弥散晕（半径按
        # 走字前/后取，跟随尺寸同比缩放）；none=不产出。
        kind = str(getattr(source, "decoration_kind", "none") or "none")
        if kind in {"shadow", "glow"}:
            decor: dict[str, object] = {
                "kind": kind,
                "fill": _layer_fill(state.shadow),
            }
            if kind == "shadow":
                decor["offset_x_px"] = round(
                    float(source.shadow_offset_x or 0) * scale, 3
                )
                decor["offset_y_px"] = round(
                    float(source.shadow_offset_y or 0) * scale, 3
                )
            else:
                concentration = normalize_glow_concentration_level(
                    getattr(source, "glow_concentration_level", 0)
                )
                radius = 0
                if concentration >= 0:
                    radius = int(
                        (
                            source.glow_after_radius_px
                            if after
                            else source.glow_before_radius_px
                        )
                        or 0
                    )
                decor["radius_px"] = round(max(radius, 0) * scale, 3)
                decor["concentration_level"] = concentration
            spec["decor"] = decor
    return spec


def particle_paint_spec(
    style: Style,
    char_style: Style | None,
    size_px: float,
    *,
    include_strokes: bool = True,
) -> dict[str, object] | None:
    """按颜色模式 + 取色层级解析**单态档**装饰规格（跟随前/后·复用方案）。

    ``color`` / ``sakura`` 档（无来源配色态）、悬空引用，以及
    ``fx_particle_color_layers == "solid"``（默认仅实色，2026-10 用户口径）
    返回 ``None``——burst 只带实色 ``color``，走纯色剪影路径。双态档
    （前后各一 / 花瓣复用）的规格见 :func:`particle_variant_paints`。
    """

    layers = str(
        getattr(style, "fx_particle_color_layers", "solid") or "solid"
    )
    if layers == "solid":
        return None
    mode = str(getattr(style, "fx_particle_color_mode", "color") or "color")
    resolved = _particle_color_state(style, char_style, mode)
    if resolved is None:
        return None
    source, state = resolved
    fallback = str(getattr(style, "fx_particle_color", "") or "#FFFFFF")
    return _state_paint_spec(
        source,
        state,
        size_px,
        layers=layers,
        # 走字前/后态：follow_after 与 role（走字后来源）用后侧发光半径。
        after=mode != "follow_before",
        include_strokes=include_strokes,
        fallback=fallback,
    )


def particle_variant_paints(
    style: Style,
    char_style: Style | None,
    size_px: float,
    *,
    petal: bool = False,
    snow: bool = False,
    include_strokes: bool = True,
) -> list[dict[str, object]] | None:
    """双色随机模式的**变体实色/规格列表**（2026-10 花瓣特效口径）：

    - ``color`` 单独颜色——颜色一/颜色二双色槽随机混发（颜色二默认白色，
      白色即颜色本身——2026-10 用户口径：必须设置双色，无「未设置」态）；
    - ``sakura`` 默认颜色——按 kind 的默认双色变体（花瓣=樱花粉、
      雪花=雪白+冰蓝，见 :func:`_default_color_pair`）；
    - ``follow_mix`` 跟随字体·前后各一——当前字符角色方案（缺省回落行
      样式）配色的走字前/后两个变体；装饰层按 ``fx_particle_color_layers``
      取色层级裁剪（默认仅实色，见 :func:`_state_paint_spec`）；
    - ``role`` 复用配色方案——**仅花瓣粒子**取指定来源方案走字前/后两个
      变体（其余粒子 kind 的 ``role`` 保持单态「走字后」路径）。

    其余模式（follow_before / follow_after / 悬空 role）返回 ``None``
    （调用方走单态 + 取色层级路径）。「随机混发」由
    :func:`plan_line_bursts` 拆成两条同轨迹、不同种子/半数量的 burst 实现
    ——每颗粒子属且属一条变体，与逐粒子取色分布等价，渲染端零改动。
    """

    fallback = str(getattr(style, "fx_particle_color", "") or "#FFFFFF")
    mode = str(getattr(style, "fx_particle_color_mode", "color") or "color")
    if mode == "color":
        # 单独颜色：恒双色（颜色二默认白色——白色即颜色，无「未设置」
        # 态，2026-10 用户口径：必须设置双色）。两槽同色时折单 burst
        #（观感等价，默认双白不翻倍 burst）。
        color2 = str(getattr(style, "fx_particle_color2", "") or "#FFFFFF")
        if color2 == fallback:
            return [{"color": fallback}]
        return [{"color": fallback}, {"color": color2}]
    if mode == "sakura":
        pair = _default_color_pair(snow)
        return [{"color": pair[0]}, {"color": pair[1]}]

    layers = str(
        getattr(style, "fx_particle_color_layers", "solid") or "solid"
    )

    def _state_variants(source: Style) -> list[dict[str, object]]:
        colors = effective_karaoke_colors(source)
        out: list[dict[str, object]] = []
        for state, after in ((colors.before, False), (colors.after, True)):
            entry: dict[str, object] = {
                "color": fill_to_solid_color(state.text, fallback)
            }
            spec = _state_paint_spec(
                source,
                state,
                size_px,
                layers=layers,
                after=after,
                include_strokes=include_strokes,
                fallback=fallback,
            )
            if spec is not None:
                entry["paint"] = spec
            out.append(entry)
        return out

    if mode == "follow_mix":
        source = char_style if char_style is not None else style
        return _state_variants(source)
    if mode == "role" and petal:
        resolved = appearance_role_source(
            style, getattr(style, "fx_particle_role_name", None)
        )
        if resolved is None:
            return None
        return _state_variants(resolved)
    return None


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


def _petal_commands() -> list[list[object]]:
    """樱花单瓣剪影（画法参考：圆润风筝形/泪滴形——底部收窄成尖、向上
    展宽，最宽处约 2/3 高度，顶端带 V 形缺刻（樱花区别于桃李杏梅的关键
    特征）；瓣长 ≈ 1.45 倍瓣宽，2026-10 用户口径：初版过宽）。单轮廓无
    自交叠（宽 0.48em × 高 0.70em）。"""
    return [
        ["M", 0.0, 370.0],
        # 左缘自底尖收窄处展宽上行
        ["C", -65.0, 335.0, -190.0, 175.0, -238.0, -25.0],
        # 左顶瓣绕向中线（最宽 ≈ ±242 在上部 2/3 处）
        ["C", -242.0, -225.0, -150.0, -355.0, -52.0, -295.0],
        # 顶端缺刻：左沿下沉到谷底再升起（缺刻深 ≈ 10% 瓣长）
        ["C", -26.0, -278.0, -11.0, -268.0, 0.0, -262.0],
        ["C", 11.0, -268.0, 26.0, -278.0, 52.0, -295.0],
        # 右顶瓣 + 右缘收窄下行回底尖
        ["C", 150.0, -355.0, 242.0, -225.0, 238.0, -25.0],
        ["C", 190.0, 175.0, 65.0, 335.0, 0.0, 370.0],
        ["Z"],
    ]


def _snow_commands() -> list[list[object]]:
    """雪花剪影：六角星——外径 500 / 内径 160 的 12 顶点直线轮廓。六重
    对称是雪花晶体的辨识特征；单轮廓无自交叠（M + 11×L + Z），粒子
    小尺寸下读作 ❄ 星形枝晶。"""
    commands: list[list[object]] = []
    for k in range(12):
        radius = 500.0 if k % 2 == 0 else 160.0
        angle = math.pi * k / 6.0 - math.pi / 2.0
        commands.append([
            "M" if k == 0 else "L",
            radius * math.cos(angle),
            radius * math.sin(angle),
        ])
    commands.append(["Z"])
    return commands


FX_SPRITES: dict[str, dict[str, object]] = {
    "star4": _sprite_ir(_star4_commands()),
    "ring": _sprite_ir(_ring_commands()),
    "note": _sprite_ir(_note_commands()),
    "pixel": _sprite_ir(_pixel_commands()),
    "petal": _sprite_ir(_petal_commands()),
    "snow": _sprite_ir(_snow_commands()),
}

_SPRITE_FOR_KIND = {
    "sparkle": "star4",
    "ripple": "ring",
    "twinkle": "star4",
    "twinkle_classic": "star4",
    "note": "note",
    "assemble": "pixel",
    "dissolve": "pixel",
    "petal": "petal",
    "snow": "snow",
}


def sprite_for_kind(kind: str) -> str:
    return _SPRITE_FOR_KIND.get(kind, "star4")


# ---------------------------------------------------------------------------
# burst 规划：同一函数供渲染 IR（native/protocol）与 QPainter 调用
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 装饰规格缓存：同一「样式 × 角色方案 × 粒子尺寸」组合只解析一次，
# 之后所有 burst / 所有帧复用同一份规格 dict（含涟漪径向采样的输入）。
# 2026-10 用户口径：用过的粒子+角色组合只烘焙一次。强引用持有键里的
# Style，防 id() 复用串缓存；LRU 上限覆盖「多角色行 × 数档尺寸」。
# ---------------------------------------------------------------------------
_PAINT_CACHE_MAX = 64
_PAINT_CACHE: "OrderedDict[tuple, tuple[tuple[Style, Style | None], dict[str, object] | None]]" = OrderedDict()
# 实色回退同理缓存（渐变平均色逐 burst 重算同样昂贵）。
_SOLID_CACHE_MAX = 64
_SOLID_CACHE: "OrderedDict[tuple, tuple[tuple[Style, Style | None], str]]" = OrderedDict()
# 双色档（sakura / follow_mix / role+花瓣）的变体规格同样缓存
#（含每个态的完整装饰规格——渐变停止色/描边宽折算逐变体重算同样昂贵）。
_VARIANT_CACHE_MAX = 64
_VARIANT_CACHE: "OrderedDict[tuple, tuple[tuple[Style, Style | None], list[dict[str, object]] | None]]" = OrderedDict()


def _cached_particle_solid(style: Style, char_style: Style | None) -> str:
    key = (id(style), id(char_style) if char_style is not None else 0)
    entry = _SOLID_CACHE.get(key)
    if entry is not None and entry[0][0] is style and (
        entry[0][1] is char_style
        if char_style is not None
        else entry[0][1] is None
    ):
        _SOLID_CACHE.move_to_end(key)
        return entry[1]
    color = particle_solid_color(style, char_style)
    if len(_SOLID_CACHE) >= _SOLID_CACHE_MAX:
        _SOLID_CACHE.popitem(last=False)
    _SOLID_CACHE[key] = ((style, char_style), color)
    return color


def _cached_particle_paint(
    style: Style,
    char_style: Style | None,
    size_px: float,
    *,
    include_strokes: bool,
) -> dict[str, object] | None:
    key = (
        id(style),
        id(char_style) if char_style is not None else 0,
        round(float(size_px), 3),
        bool(include_strokes),
    )
    entry = _PAINT_CACHE.get(key)
    if entry is not None:
        # 键对象仍被条目强引用：id 未被复用，命中即安全。
        if entry[0][0] is style and (
            entry[0][1] is char_style
            if char_style is not None
            else entry[0][1] is None
        ):
            _PAINT_CACHE.move_to_end(key)
            return entry[1]
    spec = particle_paint_spec(
        style, char_style, size_px, include_strokes=include_strokes
    )
    if len(_PAINT_CACHE) >= _PAINT_CACHE_MAX:
        _PAINT_CACHE.popitem(last=False)
    _PAINT_CACHE[key] = ((style, char_style), spec)
    return spec


def _cached_particle_variants(
    style: Style,
    char_style: Style | None,
    size_px: float,
    *,
    petal: bool,
    snow: bool = False,
    include_strokes: bool,
) -> list[dict[str, object]] | None:
    key = (
        id(style),
        id(char_style) if char_style is not None else 0,
        round(float(size_px), 3),
        bool(petal),
        bool(snow),
        bool(include_strokes),
    )
    entry = _VARIANT_CACHE.get(key)
    if entry is not None and entry[0][0] is style and (
        entry[0][1] is char_style
        if char_style is not None
        else entry[0][1] is None
    ):
        _VARIANT_CACHE.move_to_end(key)
        return entry[1]
    variants = particle_variant_paints(
        style,
        char_style,
        size_px,
        petal=petal,
        snow=snow,
        include_strokes=include_strokes,
    )
    if len(_VARIANT_CACHE) >= _VARIANT_CACHE_MAX:
        _VARIANT_CACHE.popitem(last=False)
    _VARIANT_CACHE[key] = ((style, char_style), variants)
    return variants


def clear_particle_paint_cache() -> None:
    """清空装饰规格缓存（测试隔离用；常规渲染无需失效——键含完整签名）。"""

    _PAINT_CACHE.clear()
    _SOLID_CACHE.clear()
    _VARIANT_CACHE.clear()


def plan_line_bursts(
    style: Style,
    line_index: int,
    display_start_ms: int | None,
    display_end_ms: int | None,
    line_end_ms: int | None,
    char_windows: list[tuple[int, int]],
    *,
    char_visible: list[bool] | None = None,
    char_styles: list[Style | None] | None = None,
) -> list[dict[str, object]]:
    """把 style 的粒子档位规划成逐行 burst 列表（IR-ready dict）。

    时间锚与入退场动画同源：入场自显示窗口起点，退场自
    ``max(行末, 显示末-600)`` 回溯；涟漪光环逐字跟随（每字一枚同心水波
    环，按 char_fade 的 350ms 错峰节奏）；唱字自各字符唱字窗起点发射、
    但窗口延伸到轨迹自然播完（不受唱字窗约束）。
    尺寸语义：``fx_particle_size_em`` 为相对主字号的 em 比例（0.40 =
    40% 字号），此处按全局字号折算成像素下发，两后端同值。
    颜色语义：``color`` 恒为实色回退（旧 sidecar 兼容）；颜色模式非
    ``color`` 时另附 ``paint`` 完整装饰规格（填充/描边/二重描边 PaintFill
    + 已按粒子尺寸缩放的描边宽，见 :func:`particle_paint_spec`），两条
    后端优先按 ``paint`` 绘制；双色档（单独颜色双槽 / ``sakura`` /
    ``follow_mix`` 前后各一 / ``role``+花瓣）改拆两条实色变体 burst
    （数量对半、种子错开）。入退场动画粒子默认固定樱花粉双色档，开启
    ``fx_apply_to_entry_exit`` 后改吃粒子旋钮的颜色与尺寸（数量恒固定）。
    ``line_index`` 参与种子，保证同曲目每行轨迹不同且重开可复现。
    ``char_visible``（与 char_windows 等长）：False = 空白字符（空格等
    无字形内容）——唱字粒子跳过（无走字内容），出入场仍整行参与。
    ``char_styles``（可选，与 char_windows 等长）：逐字符的角色样式
    （调用方按 ``role_label`` 解析后传入）——「跟随字体」按**当前字符**
    自己的角色配色取色；未提供时回落行样式（歌手行已合并歌手方案）。
    """
    bursts: list[dict[str, object]] = []
    count = max(2, min(64, int(style.fx_particle_count)))
    font_px = max(float(getattr(style, "font_size_px", 0.0) or 0.0), 1.0)
    size = font_px * max(0.05, min(2.0, float(getattr(style, "fx_particle_size_em", 0.40))))
    seed_base = ((int(line_index) & 0xFFFF) * 0x9E37) & 0xFFFFFFFF
    char_count = len(char_windows)
    stagger = (
        RIPPLE_CHAR_STAGGER_MS // (char_count - 1) if char_count > 1 else 0
    )
    # 出入场动画粒子：默认固定档（数量恒固定）；开启「联动入退场」后
    # 颜色与尺寸改吃粒子旋钮（颜色模式同样生效）。编排窗口仍随「入场/
    # 退场动画时长」旋钮等比缩放（0 = 默认 600ms 基准）。
    apply_to_anim = bool(getattr(style, "fx_apply_to_entry_exit", False))
    anim_size = size if apply_to_anim else font_px * ANIM_PARTICLE_SIZE_EM
    anim_count = ANIM_PARTICLE_COUNT

    def _char_style(char_index: int | None) -> Style | None:
        # 行锚点 burst（入退场星光）取**首字符**的角色样式：逐字角色不进
        # 行样式，直接回落行样式会退成全局默认（2026-10 用户实测——主文
        # 字挂角色 A、唱字跟随正确、入退场却跟了全局默认）。无角色的首字
        # 符解析结果即行样式，语义不变；与指示灯/音量柱「段首行第一个角
        # 色」同款先例。
        if char_styles:
            if char_index is None:
                char_index = 0
            if 0 <= char_index < len(char_styles):
                return char_styles[char_index]
        return None

    def _burst_paint(
        char_index: int | None,
        burst_size: float,
        *,
        anim: bool,
        ring: bool = False,
        line_anchor: bool = False,
        petal: bool = False,
        snow: bool = False,
    ) -> list[dict[str, object]]:
        """burst 的颜色规格**列表**：固定默认档（按 kind 的默认双色）/ 实色
        回退 (+ 取色层级的装饰规格) / 双色档的变体规格列表
        （见 :func:`particle_variant_paints`）。

        规格按「样式 × 角色方案 × 尺寸 × 是否涟漪」缓存——同一组合全帧
        复用一份（见 :func:`_cached_particle_paint`）。涟漪光环不带描边
        （``include_strokes=False``）：环体是发丝线，叠描边显著变粗。
        取色层级（``fx_particle_color_layers``，仅来源配色四档生效）：默认
        仅实色 = 纯色剪影；+描边 = 按方案描边栈（不加装饰）；+装饰 = 仅
        装饰层（不加描边，阴影/发光按方案）；全有 = 描边栈 + 装饰层（见
        :func:`_state_paint_spec`）。

        ``line_anchor``（入退场星光）：跟随模式下附 ``char_colors`` 逐字
        颜色表——动画仍是整行一条 burst（扫过轨迹不变），绘制端按每颗粒
        子落点所在字符取该字角色的颜色（2026-10 用户口径：颜色逐字、动
        画不动）。此时不带 paint 规格（逐粒子换色无法烘焙/静态笔刷，
        取色层级装饰在逐字取色档不适用），``color`` 回退取首字符颜色
        （旧 sidecar 兼容）。
        """

        if anim and not apply_to_anim:
            # 固定默认档 = 按 kind 的默认双色随机混发（2026-10 用户口径：
            # 出入场粒子用「默认颜色」而非单独颜色的默认白；樱花粉为既有
            # 口径，雪花按 kind 取雪白+冰蓝）。
            pair = _default_color_pair(snow)
            return [{"color": pair[0]}, {"color": pair[1]}]
        mode = str(getattr(style, "fx_particle_color_mode", "color") or "color")
        if (
            line_anchor
            and mode in {"follow_before", "follow_after"}
            and char_styles
        ):
            char_colors = [
                _cached_particle_solid(style, _char_style(index))
                for index in range(char_count)
            ]
            return [{
                "color": char_colors[0],
                "char_colors": char_colors,
            }]
        char_style = _char_style(char_index)
        variants = _cached_particle_variants(
            style,
            char_style,
            burst_size,
            petal=petal,
            snow=snow,
            include_strokes=not ring,
        )
        if variants is not None:
            return variants
        fields: dict[str, object] = {
            "color": _cached_particle_solid(style, char_style)
        }
        spec = _cached_particle_paint(
            style, char_style, burst_size, include_strokes=not ring
        )
        if spec is not None:
            fields["paint"] = spec
        return [fields]

    def _variant_bursts(
        fields: dict[str, object],
        paints: list[dict[str, object]],
    ) -> None:
        """按颜色变体发射 burst：单色一条原样下发；双色档拆两条同轨迹
        变体——数量对半（余数归第一条）、种子按变体序号偏移，保证两条
        变体的粒子伪随机位置互相独立（同 index 撞位）。「每颗粒子随机
        取一色」由此实现，渲染端零特殊分支。"""

        total = int(fields["count"])
        for variant, paint in enumerate(paints):
            burst = {**fields, **paint}
            if len(paints) > 1:
                burst["count"] = (
                    total - total // 2 if variant == 0 else total // 2
                )
                burst["seed"] = (
                    int(fields["seed"]) + variant * 0x9E37
                ) & 0xFFFFFFFF
            bursts.append(burst)

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
        scale: float = 1.0,
    ) -> None:
        px = anim_size * 3.6 if ring_size is None else ring_size
        for char_index in range(char_count):
            start = int(base_ms) + int(stagger_ms * scale) * char_index
            _variant_bursts({
                "kind": "ripple", "anchor": "char",
                "char_index": int(char_index),
                "start_ms": start,
                "end_ms": start + int(RIPPLE_LIFE_MS * scale),
                "count": RIPPLE_RING_COUNT,
                "seed": (seed_base + seed_offset + char_index) & 0xFFFFFFFF,
                # 水波纹基准直径 ≈ 1.4× 字高，扩散峰值约 1.6× 字高。
                "size_px": px, "travel_px": 0.0, "front": False,
                "sweep": 0,
            }, _burst_paint(char_index, px, anim=True, ring=True))

    entry_anim = str(getattr(style, "entry_anim", "none") or "none")
    if entry_anim == "sparkle" and entry_active and display_start_ms is not None:
        _variant_bursts({
            "kind": "sparkle", "anchor": "line", "char_index": -1,
            "start_ms": int(display_start_ms),
            "end_ms": int(display_start_ms)
            + int(SPARKLE_ENTRY_LIFE_MS * entry_scale),
            "count": anim_count, "seed": (seed_base + 1) & 0xFFFFFFFF,
            # 星光画在主文字前（背后看不清——2026-10 用户复调；纵向锚点
            # 已偏置到字上侧为主，遮挡有限）。
            "size_px": anim_size, "travel_px": anim_size * 3.2, "front": True,
            "sweep": 1,
        }, _burst_paint(None, anim_size, anim=True, line_anchor=True))
    elif entry_anim == "ripple" and entry_active and display_start_ms is not None:
        _char_ripples(
            int(display_start_ms), 2, stagger_ms=stagger, scale=entry_scale
        )
    elif entry_anim == "note" and entry_active and display_start_ms is not None:
        for char_index in range(char_count):
            start = int(display_start_ms) + int(stagger * entry_scale) * char_index
            _variant_bursts({
                "kind": "note", "anchor": "char",
                "char_index": int(char_index),
                "start_ms": start,
                "end_ms": start + int(SING_NOTE_TOTAL_MS * entry_scale),
                "count": 3, "seed": (seed_base + 6 + char_index) & 0xFFFFFFFF,
                "size_px": anim_size, "travel_px": anim_size * 1.8,
                "front": True, "sweep": 0,
            }, _burst_paint(char_index, anim_size, anim=True))
    elif entry_anim == "petal" and entry_active and display_start_ms is not None:
        # 花瓣飘入：每字自字形顶上方错峰飘落汇拢到字框（ease-out 减速
        # 抵达后熄灭），随机摇摆 + 连续翻转（\moves4 四点飘移等效观感）。
        for char_index in range(char_count):
            start = int(display_start_ms) + int(stagger * entry_scale) * char_index
            _variant_bursts({
                "kind": "petal", "anchor": "char",
                "char_index": int(char_index),
                "start_ms": start,
                "end_ms": start + int(PETAL_LIFE_MS * entry_scale),
                "count": 3, "seed": (seed_base + 10 + char_index) & 0xFFFFFFFF,
                "size_px": anim_size, "travel_px": 0.0,
                "front": True, "sweep": 1,
            }, _burst_paint(char_index, anim_size, anim=True, petal=True))
    elif entry_anim == "snow" and entry_active and display_start_ms is not None:
        # 雪花飘入：每字自字形顶上方错峰匀速缓降进字框（宽幅低频摇摆 +
        # 慢自转——AE CC Snowfall 同款运动签名）。
        for char_index in range(char_count):
            start = int(display_start_ms) + int(stagger * entry_scale) * char_index
            _variant_bursts({
                "kind": "snow", "anchor": "char",
                "char_index": int(char_index),
                "start_ms": start,
                "end_ms": start + int(SNOW_LIFE_MS * entry_scale),
                "count": 3, "seed": (seed_base + 12 + char_index) & 0xFFFFFFFF,
                "size_px": anim_size, "travel_px": 0.0,
                "front": True, "sweep": 1,
            }, _burst_paint(char_index, anim_size, anim=True, snow=True))

    exit_anim = str(getattr(style, "exit_anim", "none") or "none")
    if exit_anim == "sparkle" and exit_active and display_end_ms is not None:
        exit_start = max(
            int(line_end_ms) if line_end_ms is not None else 0,
            int(display_end_ms) - int(SPARKLE_EXIT_LIFE_MS * exit_scale),
        )
        if int(display_end_ms) - exit_start < 120:
            exit_start = int(display_end_ms) - 120
        _variant_bursts({
            "kind": "sparkle", "anchor": "line", "char_index": -1,
            "start_ms": exit_start, "end_ms": int(display_end_ms),
            "count": anim_count, "seed": (seed_base + 3) & 0xFFFFFFFF,
            "size_px": anim_size, "travel_px": anim_size * 3.2, "front": True,
            "sweep": -1,
        }, _burst_paint(None, anim_size, anim=True, line_anchor=True))
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
            _variant_bursts({
                "kind": "note", "anchor": "char",
                "char_index": int(char_index),
                "start_ms": start,
                "end_ms": start + int(SING_NOTE_TOTAL_MS * exit_scale),
                "count": 3, "seed": (seed_base + 8 + char_index) & 0xFFFFFFFF,
                "size_px": anim_size, "travel_px": anim_size * 1.8,
                "front": True, "sweep": 0,
            }, _burst_paint(char_index, anim_size, anim=True))
    elif exit_anim == "petal" and exit_active and display_end_ms is not None:
        exit_start_petal = max(
            int(line_end_ms) if line_end_ms is not None else 0,
            int(display_end_ms)
            - int((RIPPLE_CHAR_STAGGER_MS + PETAL_LIFE_MS) * exit_scale),
        )
        if int(display_end_ms) - exit_start_petal < 120:
            exit_start_petal = int(display_end_ms) - 120
        tail_petal = (
            int(display_end_ms) - exit_start_petal
            - int(PETAL_LIFE_MS * exit_scale)
        )
        stagger_petal = min(
            int(stagger * exit_scale),
            max(0, tail_petal) // max(1, char_count - 1),
        )
        # 花瓣飘散：每字自字框内错峰剥落、边翻转边飘落到字底下方淡出。
        for char_index in range(char_count):
            start = exit_start_petal + stagger_petal * char_index
            _variant_bursts({
                "kind": "petal", "anchor": "char",
                "char_index": int(char_index),
                "start_ms": start,
                "end_ms": start + int(PETAL_LIFE_MS * exit_scale),
                "count": 3, "seed": (seed_base + 11 + char_index) & 0xFFFFFFFF,
                "size_px": anim_size, "travel_px": 0.0,
                "front": True, "sweep": -1,
            }, _burst_paint(char_index, anim_size, anim=True, petal=True))
    elif exit_anim == "snow" and exit_active and display_end_ms is not None:
        exit_start_snow = max(
            int(line_end_ms) if line_end_ms is not None else 0,
            int(display_end_ms)
            - int((RIPPLE_CHAR_STAGGER_MS + SNOW_LIFE_MS) * exit_scale),
        )
        if int(display_end_ms) - exit_start_snow < 120:
            exit_start_snow = int(display_end_ms) - 120
        tail_snow = (
            int(display_end_ms) - exit_start_snow
            - int(SNOW_LIFE_MS * exit_scale)
        )
        stagger_snow = min(
            int(stagger * exit_scale),
            max(0, tail_snow) // max(1, char_count - 1),
        )
        # 雪花飘散：每字自字框内错峰剥落、随机摇摆、终点向右缓漂、下探
        # 行间隙淡出（与花瓣同款编排，行程更缓）。
        for char_index in range(char_count):
            start = exit_start_snow + stagger_snow * char_index
            _variant_bursts({
                "kind": "snow", "anchor": "char",
                "char_index": int(char_index),
                "start_ms": start,
                "end_ms": start + int(SNOW_LIFE_MS * exit_scale),
                "count": 3, "seed": (seed_base + 13 + char_index) & 0xFFFFFFFF,
                "size_px": anim_size, "travel_px": 0.0,
                "front": True, "sweep": -1,
            }, _burst_paint(char_index, anim_size, anim=True, snow=True))

    # 粒子拼接/消散：像素风方块粒子，同出入场动画档（默认固定档）。
    per_char_assemble = max(4, ANIM_PARTICLE_COUNT // 2)
    if entry_anim == "assemble_in" and entry_active and display_start_ms is not None:
        for char_index in range(char_count):
            start = int(display_start_ms) + int(stagger * entry_scale) * char_index
            _variant_bursts({
                "kind": "assemble", "anchor": "char",
                "char_index": int(char_index),
                "start_ms": start,
                "end_ms": start + int(ASSEMBLE_LIFE_MS * entry_scale),
                "count": per_char_assemble,
                "seed": (seed_base + 7 + char_index) & 0xFFFFFFFF,
                "size_px": anim_size * 0.75,
                "travel_px": font_px * ASSEMBLE_TRAVEL_EM,
                "front": True, "sweep": 0,
            }, _burst_paint(char_index, anim_size * 0.75, anim=True))
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
            _variant_bursts({
                "kind": "dissolve", "anchor": "char",
                "char_index": int(char_index),
                "start_ms": start,
                "end_ms": start + int(ASSEMBLE_LIFE_MS * exit_scale),
                "count": per_char_assemble,
                "seed": (seed_base + 9 + char_index) & 0xFFFFFFFF,
                "size_px": anim_size * 0.75,
                "travel_px": font_px * ASSEMBLE_TRAVEL_EM,
                "front": True, "sweep": 0,
            }, _burst_paint(char_index, anim_size * 0.75, anim=True))

    # 唱字装饰粒子：默认唯一吃粒子旋钮（尺寸/数量/颜色）的档位。
    if style.sing_fx == "ripple":
        for char_index, (start_ms, end_ms) in enumerate(char_windows):
            duration = int(end_ms) - int(start_ms)
            if duration <= 0:
                continue
            if char_visible is not None and not char_visible[char_index]:
                continue
            ring_px = size * 3.6
            _variant_bursts({
                "kind": "ripple", "anchor": "char",
                "char_index": int(char_index),
                "start_ms": int(start_ms),
                "end_ms": int(start_ms) + RIPPLE_LIFE_MS,
                "count": RIPPLE_RING_COUNT,
                "seed": (seed_base + 5 + char_index) & 0xFFFFFFFF,
                "size_px": ring_px, "travel_px": 0.0, "front": False,
                "sweep": 0,
            }, _burst_paint(char_index, ring_px, anim=False, ring=True))
    elif style.sing_fx == "petal":
        # 花瓣飘动：唱到的字上花瓣轻摆缓沉（数量吃粒子旋钮，默认 4/字），
        # 窗口不受唱字窗约束、自然播完（与星光/音符同口径）。
        per_char = max(3, count // 3)
        for char_index, (start_ms, end_ms) in enumerate(char_windows):
            duration = int(end_ms) - int(start_ms)
            if duration <= 0:
                continue
            if char_visible is not None and not char_visible[char_index]:
                continue
            _variant_bursts({
                "kind": "petal", "anchor": "char",
                "char_index": int(char_index),
                "start_ms": int(start_ms),
                "end_ms": int(start_ms) + PETAL_LIFE_MS,
                "count": per_char,
                "seed": (seed_base + 5 + char_index) & 0xFFFFFFFF,
                "size_px": size, "travel_px": 0.0,
                "front": True, "sweep": 0,
            }, _burst_paint(char_index, size, anim=False, petal=True))
    elif style.sing_fx == "snow":
        # 雪花飘动：唱到的字上雪花轻摆缓沉（数量吃粒子旋钮，默认 4/字），
        # 窗口不受唱字窗约束、自然播完（与花瓣同口径，摇摆更慢更宽）。
        per_char = max(3, count // 3)
        for char_index, (start_ms, end_ms) in enumerate(char_windows):
            duration = int(end_ms) - int(start_ms)
            if duration <= 0:
                continue
            if char_visible is not None and not char_visible[char_index]:
                continue
            _variant_bursts({
                "kind": "snow", "anchor": "char",
                "char_index": int(char_index),
                "start_ms": int(start_ms),
                "end_ms": int(start_ms) + SNOW_LIFE_MS,
                "count": per_char,
                "seed": (seed_base + 5 + char_index) & 0xFFFFFFFF,
                "size_px": size, "travel_px": 0.0,
                "front": True, "sweep": 0,
            }, _burst_paint(char_index, size, anim=False, snow=True))
    elif style.sing_fx in ("twinkle", "twinkle_classic", "note"):
        # 唱字星光并入出入场星光运动学后，密度对齐出入场观感（默认 7/字）；
        # 「旧版」档（twinkle_classic）保留 2026-10 运动学改造前的原地闪烁
        # 观感，颗数沿用旧口径（默认 3/字）。
        if style.sing_fx == "twinkle":
            per_char = max(5, count // 2)
        elif style.sing_fx == "twinkle_classic":
            per_char = max(3, count // 4)
        else:
            per_char = 3
        total = SING_NOTE_TOTAL_MS if style.sing_fx == "note" else SING_TWINKLE_TOTAL_MS
        for char_index, (start_ms, end_ms) in enumerate(char_windows):
            duration = int(end_ms) - int(start_ms)
            if duration <= 0:
                continue
            if char_visible is not None and not char_visible[char_index]:
                continue
            seed = (seed_base + 5 + char_index) & 0xFFFFFFFF
            _variant_bursts({
                # 叠加在正在唱的那个字上（用户口径）：锚点=该字符自身
                # 中心；发射时刻=该字符唱字窗起点，窗口延伸到轨迹自然
                # 播完（换字后余韵继续，不硬截断）。
                "kind": style.sing_fx, "anchor": "char",
                "char_index": int(char_index),
                "start_ms": int(start_ms),
                "end_ms": int(start_ms) + total,
                "count": per_char,
                "seed": seed,
                # 星光族画主文字前（背后看不清——2026-10 用户复调）；
                # 唱字星光自字左向右小扫过（出入场同款波向运动学）；
                # 旧版档原地闪烁（sweep=0，无扫过）。
                "size_px": size, "travel_px": size * 1.8, "front": True,
                "sweep": 1 if style.sing_fx == "twinkle" else 0,
            }, _burst_paint(char_index, size, anim=False))
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

    star_prev_q = -1  # 星光族象限链：-1 = 尚无上一颗
    for i in range(int(burst["count"])):
        if kind in ("sparkle", "twinkle"):
            # 整句扫过：粒子伪随机铺满整行宽度（box_w），入场从左到右、退场
            # 反向，按横向位置错峰出生 → 连续顺滑地跟随扫过整句；每颗随机
            # 初始/末了大小、随机向上漂移方向。唱字星光（twinkle）2026-10
            # 起并入同一运动学（原地 sin 缩放的旧模式僵硬——用户口径），
            # 在字框内做同款小扫过 + 漂移。纵向锚点按 star_y_fraction
            # 偏置到主文字上侧（只稍微溢出字形顶），且连续两颗不落同象限
            #（链式比较出生位置，与存活过滤无关、重放可复现）。
            u1 = fx_unit_hash(seed + i, 1)
            u2 = fx_unit_hash(seed + i, 2)
            u3 = fx_unit_hash(seed + i, 3)
            u4 = fx_unit_hash(seed + i, 4)
            u5 = fx_unit_hash(seed + i, 5)
            left = (u1 - 0.5) < 0.0
            u2 = star_y_resample(
                u2,
                fx_unit_hash(seed + i, 6),
                fx_unit_hash(seed + i, 7),
                star_prev_q,
                left,
            )
            y_off = star_y_fraction(u2) * box_h
            star_prev_q = _star_quadrant(left, y_off < 0.0)
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
                origin_y + y_off + drift_y,
                u3 * 90.0 + 60.0 * p,
                scale,
                1.0 - p,
            ))
        elif kind == "twinkle_classic":
            # 旧版唱字星光（2026-10 运动学改造前的形态，逐字复刻自被
            # 2775dc99 删除的 twinkle 分支）：位置对称铺满整个字框、随机
            # 固定旋转、随机寿命（300–450ms）与峰值大小，sin 包络「出生
            # →放大→熄灭」的原地闪烁。镜像 C++ d2d_fx.cpp 同名分支。
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
        elif kind == "petal":
            # 花瓣飘落（Aegisub「\moves4 四点飘移」等效运动学）：sin 横向
            # 摇摆 + 单调竖直行程 + 全程连续翻转。位置寻路与星光/音符同
            # 约定——偏移全部取 box_w/box_h 比例（星光 star_y_fraction 的
            # 「只稍微溢出字形顶」口径；音符出生在 -0.35 box_h、行程按
            # 粒子尺寸折算），不用字号倍数，避免飘进行间距/上一行
            #（2026-10 用户口径：初版 travel=1.6em 出生过高）。
            # sweep>0 飘入 ease-out 汇拢、sweep<0 飘散 smoothstep 离场、
            # sweep==0 唱字飘动小行程缓起缓落。随机出生延迟错峰
            # （快进慢出包络，\fad 同款）。镜像 d2d_fx.cpp petal 分支。
            u1 = fx_unit_hash(seed + i, 1)
            u2 = fx_unit_hash(seed + i, 2)
            u3 = fx_unit_hash(seed + i, 3)
            u4 = fx_unit_hash(seed + i, 4)
            u5 = fx_unit_hash(seed + i, 5)
            delay = u3 * 110.0
            p = min(max((tau - delay) / max(life - delay, 1.0), 0.0), 1.0)
            if p <= 0.0:
                continue
            sway = (
                math.sin((u2 + p * (0.9 + 0.9 * u5)) * 2.0 * math.pi)
                * size * (0.8 + 0.7 * u4)
            )
            size_i = size * (0.72 + 0.55 * u4)
            spin = (
                u3 * 360.0
                + p * (160.0 + 240.0 * u5) * (1.0 if u4 >= 0.5 else -1.0)
            )
            if sweep > 0:
                # 花瓣飘入：自字形顶上方（溢出 0.10~0.40 box_h，星光同
                # 量级）ease-out 减速飘落进字框，抵达后熄灭。
                eased = 1.0 - (1.0 - p) * (1.0 - p)
                land_x = (u1 - 0.5) * box_w * 0.85
                land_y = (u4 - 0.5) * box_h * 0.55
                start_x = land_x + (u5 - 0.5) * box_w * 0.7
                start_y = -(0.60 + 0.30 * u2) * box_h
                out.append(ParticleDraw(
                    origin_x + start_x + (land_x - start_x) * eased + sway,
                    origin_y + start_y + (land_y - start_y) * eased,
                    spin,
                    size_i * (0.85 + 0.15 * math.sin(math.pi * p)),
                    min(p * 5.0, 1.0) * (1.0 - p) * (1.0 - p * 0.4),
                ))
            elif sweep < 0:
                # 花瓣飘散：横向飘逸保持随机（sin 摇摆），只有**结束位置**
                # 落在起点右侧（幅度随机，2026-10 用户口径：飘逸随机、
                # 终点向右）；纵向落到字底下方（0.05~0.65 box_h）淡出。
                eased = p * p * (3.0 - 2.0 * p)
                start_x = (u1 - 0.5) * box_w * 0.85
                end_x = start_x + (0.5 + 0.9 * u5) * box_w
                start_y = (u4 - 0.5) * box_h * 0.55
                out.append(ParticleDraw(
                    origin_x + start_x + (end_x - start_x) * eased + sway,
                    origin_y + start_y + (0.55 + 0.30 * u2) * box_h * eased,
                    spin,
                    size_i * (0.90 + 0.10 * (1.0 - p)),
                    (1.0 - p) * (1.0 - p * 0.5),
                ))
            else:
                # 花瓣飘动：字框上半出生（星光的偏上口径），小行程缓沉，
                # sin 包络淡入淡出。
                eased = p * p * (3.0 - 2.0 * p)
                out.append(ParticleDraw(
                    origin_x + (u1 - 0.5) * box_w * 0.7 + sway,
                    origin_y - (0.25 + 0.30 * u4) * box_h
                    + (0.40 + 0.30 * u2) * box_h * eased,
                    spin,
                    size_i * (0.85 + 0.15 * math.sin(math.pi * p)),
                    min(p * 5.0, 1.0) * math.sin(math.pi * p),
                ))
        elif kind == "snow":
            # 雪花飘落（AE CC Snowfall 运动签名，2026-10）：匀速下沉
            #（终端速度感，ease=线性）+ 宽幅**低频**摇摆（不足一个整周期
            # 的大漂移——雪的漂浮感，与花瓣的高频小摆相区分）+ 慢自转 +
            # 柔包络。位置偏移全部 box 比例（星光/音符/花瓣同约定）。
            # sweep>0 飘入、sweep<0 飘散（终点向右，同花瓣口径）、
            # sweep==0 唱字飘动。随机出生延迟错峰。镜像 d2d_fx.cpp。
            u1 = fx_unit_hash(seed + i, 1)
            u2 = fx_unit_hash(seed + i, 2)
            u3 = fx_unit_hash(seed + i, 3)
            u4 = fx_unit_hash(seed + i, 4)
            u5 = fx_unit_hash(seed + i, 5)
            delay = u3 * 160.0
            p = min(max((tau - delay) / max(life - delay, 1.0), 0.0), 1.0)
            if p <= 0.0:
                continue
            sway = (
                math.sin((u2 + p * (0.35 + 0.45 * u5)) * 2.0 * math.pi)
                * size * (0.75 + 0.65 * u4)
            )
            size_i = size * (0.70 + 0.50 * u4)
            spin = (
                u3 * 360.0
                + p * (80.0 + 140.0 * u5) * (1.0 if u4 >= 0.5 else -1.0)
            )
            eased = p  # 匀速下沉（雪的终端速度感）
            if sweep > 0:
                # 雪花飘入：自字形顶上方匀速缓降进字框。
                land_x = (u1 - 0.5) * box_w * 0.85
                land_y = (u4 - 0.5) * box_h * 0.55
                start_x = land_x + (u5 - 0.5) * box_w * 0.6
                start_y = -(0.60 + 0.30 * u2) * box_h
                out.append(ParticleDraw(
                    origin_x + start_x + (land_x - start_x) * eased + sway,
                    origin_y + start_y + (land_y - start_y) * eased,
                    spin,
                    size_i * (0.85 + 0.15 * math.sin(math.pi * p)),
                    min(p * 4.0, 1.0) * (1.0 - p) * (1.0 - p * 0.4),
                ))
            elif sweep < 0:
                # 雪花飘散：随机摇摆、终点向右缓漂（幅度小于花瓣），
                # 下探行间隙淡出。
                start_x = (u1 - 0.5) * box_w * 0.85
                end_x = start_x + (0.35 + 0.6 * u5) * box_w
                start_y = (u4 - 0.5) * box_h * 0.55
                out.append(ParticleDraw(
                    origin_x + start_x + (end_x - start_x) * eased + sway,
                    origin_y + start_y + (0.50 + 0.30 * u2) * box_h * eased,
                    spin,
                    size_i * (0.90 + 0.10 * (1.0 - p)),
                    (1.0 - p) * (1.0 - p * 0.5),
                ))
            else:
                # 雪花飘动：字框上半出生、轻摆缓沉，sin 包络淡入淡出。
                out.append(ParticleDraw(
                    origin_x + (u1 - 0.5) * box_w * 0.7 + sway,
                    origin_y - (0.25 + 0.30 * u4) * box_h
                    + (0.40 + 0.30 * u2) * box_h * eased,
                    spin,
                    size_i * (0.85 + 0.15 * math.sin(math.pi * p)),
                    min(p * 4.0, 1.0) * math.sin(math.pi * p),
                ))
    return out
