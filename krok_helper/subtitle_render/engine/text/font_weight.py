"""统一「请求字重 → 字体实例」解析——Qt/CPU 与 Direct2D/GPU 的共同口径。

背景（2026-10 可变字体排障）：可变字体的命名实例字重往往不是标准整百
（如 Iwata UD Gothic VF 的 300/425/460/540/700/790/900）。此前 CPU 侧
用 ``clamp_weight`` 桶化后交给 Qt 自行匹配、GPU 侧把原始字重交给
DirectWrite ``GetFirstMatchingFont``，两条管线对同一 (family, weight)
会选出不同实例；Qt 对缺失字重还会落进 GDI 伪家族的合成粗体（'L Bold'），
导致两条后端的字形 / advance / 行宽全部分叉。

本模块是唯一的决策点，规则与 native 侧 ``d2d_font_fallback.cpp`` 的
unified weight resolution 逐条对应（改动任一侧必须同步另一侧）：

1. **可变字体**（fvar 含 wght 轴）：按 ``clamp(W, axis.min, axis.max)``
   渲染**真实轴值插值实例**，永不模拟——轴内任意值都是"真实字重"
   （``QFont.setVariableAxis`` 优先级高于 setWeight/setStyleName，实测
   越界值自动钳制到轴端点）。
2. **静态字体**：``E = bucket_weight(W)``（标准整百桶化）；
   a. E 命中真实 face 字重 → ``setStyleName`` 钉住该 face，不设 weight；
   b. 族内仅一个 face 且 E ≥ 600 且 E > face 字重 → 钉住该 face +
      ``setWeight(E)`` 触发合成粗体（唯一能确定性地模拟的场景：基 face
      唯一，Qt 无其他 face 可被匹配器劫持）；
   c. 其余缺失档 → 就近吸附到真实 face（平局取较轻）并钉扎，不模拟
      （Qt 对多 face 族缺失字重的原生匹配跨族不可预测——Yu Gothic@600
      给 Regular+假粗体、Yu Gothic UI@800 给真 Bold——必须钉扎绕开）。
3. **拿不到 face 元数据**（字体缺失 / headless 枚举为空）：退回旧行为
   （仅按桶化值 setWeight），不钉扎。

可变轴信息来自 ``QRawFont.fontTable('fvar')`` 的最小解析（tag/min/
default/max），进程级缓存；face 清单来自 ``QFontDatabase.styles``。
"""

from __future__ import annotations

import struct
import threading
from dataclasses import dataclass

from PyQt6.QtGui import QFont, QFontDatabase, QFontInfo, QFontMetrics, QPainterPath, QRawFont

_AXIS_TAG_WEIGHT = b"wght"

# 与 metrics.clamp_weight / native 侧 weightBucket 同表的整百桶化。
_WEIGHT_BUCKETS: tuple[tuple[int, int], ...] = (
    (250, 100),
    (350, 300),
    (450, 400),
    (550, 500),
    (650, 600),
    (750, 700),
    (850, 800),
    (1000, 900),
)


def bucket_weight(weight: int) -> int:
    """把任意请求字重映射到标准整百（100..900），与 clamp_weight 同表。"""
    value = int(weight)
    for upper, bucket in _WEIGHT_BUCKETS:
        if value <= upper:
            return bucket
    return 900


@dataclass(frozen=True)
class WeightAxis:
    minimum: float
    default: float
    maximum: float


@dataclass(frozen=True)
class FontWeightPlan:
    """(family, 请求字重) 的权威解析结果。

    ``axis_value`` 非 None 时走可变字体真实轴值渲染，其余字段仅静态
    路径有效；``mark`` 供 UI 标注：None=真实，"模拟"/"就近"/"越界"。
    """

    family: str
    requested_weight: int
    axis_value: float | None = None
    style_name: str | None = None
    base_weight: int = 0
    synthetic_bold: bool = False
    enum_weight: int = 0
    italic: bool = False
    mark: str | None = None


_AXIS_CACHE: dict[str, dict[bytes, WeightAxis] | None] = {}
_FACE_CACHE: dict[str, tuple[tuple[int, str, bool], ...]] = {}
_PLAN_CACHE: dict[tuple[str, int, bool], FontWeightPlan] = {}
_LOCK = threading.Lock()


def clear_font_weight_cache() -> None:
    """字体安装/卸载后清空进程级缓存（调用方：宿主字体库刷新）。"""
    with _LOCK:
        _AXIS_CACHE.clear()
        _FACE_CACHE.clear()
        _PLAN_CACHE.clear()


def _parse_fvar(raw: bytes) -> dict[bytes, WeightAxis] | None:
    if len(raw) < 16:
        return None
    major, _minor, axes_offset, _reserved, axis_count, axis_size = struct.unpack_from(
        ">6H", raw, 0
    )
    _instance_count, _instance_size = struct.unpack_from(">2H", raw, 12)
    if major != 1 or axis_size < 20 or axes_offset < 16:
        return None
    axes: dict[bytes, WeightAxis] = {}
    for index in range(axis_count):
        offset = axes_offset + index * axis_size
        if offset + 20 > len(raw):
            break
        tag = bytes(raw[offset : offset + 4])
        minimum, default, maximum = struct.unpack_from(">3l", raw, offset + 4)
        axes[tag] = WeightAxis(minimum / 65536.0, default / 65536.0, maximum / 65536.0)
    return axes


def family_weight_axis(family: str) -> WeightAxis | None:
    """返回族解析结果里的 wght 轴；静态字体 / 解析失败返回 None。"""
    key = str(family)
    with _LOCK:
        cached = _AXIS_CACHE.get(key, _MISSING)
    if cached is not _MISSING:
        return None if cached is None else cached.get(_AXIS_TAG_WEIGHT)
    try:
        raw_font = QRawFont.fromFont(QFont(key))
        table = bytes(raw_font.fontTable(b"fvar"))
        axes = _parse_fvar(table) if table else None
    except (RuntimeError, TypeError, ValueError):
        axes = None
    with _LOCK:
        _AXIS_CACHE[key] = axes
    return None if axes is None else axes.get(_AXIS_TAG_WEIGHT)


class _Missing:
    pass


_MISSING = _Missing()


def face_inventory(family: str) -> tuple[tuple[int, str, bool], ...]:
    """族内全部真实 face 的 ``(字重, styleName, 是否斜体)``，按字重升序。

    同字重保留全部 face（ upright 与斜体是不同 face，去重会钉错）；
    headless / 字体缺失返回空 tuple。进程级缓存。
    """
    key = str(family)
    with _LOCK:
        cached = _FACE_CACHE.get(key)
    if cached is not None:
        return cached
    faces: list[tuple[int, str, bool]] = []
    try:
        for style in QFontDatabase.styles(key):
            weight = int(QFontDatabase.weight(key, style))
            name = str(style)
            italic = bool(QFontDatabase.italic(key, style))
            if 1 <= weight <= 1000:
                faces.append((weight, name, italic))
    except (RuntimeError, TypeError, ValueError):
        faces = []
    faces.sort(key=lambda entry: (entry[0], entry[2], entry[1]))
    inventory = tuple(faces)
    with _LOCK:
        _FACE_CACHE[key] = inventory
    return inventory


def physical_weight_styles(family: str) -> tuple[tuple[int, str], ...]:
    """族内直立 face 的 ``(字重, styleName)`` 清单，按字重升序。

    同字重既有直立又有斜体 face 时只保留直立（斜体 face 由解析器按
    italic 请求单独选择）；仅有斜体 face 的族退回斜体清单。
    """
    inventory = face_inventory(family)
    upright = tuple(
        (weight, name) for weight, name, italic in inventory if not italic
    )
    if upright:
        seen: dict[int, str] = {}
        for weight, name in upright:
            if weight not in seen or name < seen[weight]:
                seen[weight] = name
        return tuple(sorted(seen.items()))
    seen = {}
    for weight, name, _italic in inventory:
        if weight not in seen or name < seen[weight]:
            seen[weight] = name
    return tuple(sorted(seen.items()))


# 指纹探测：多字号 + advance + 墨迹包围盒。双字号是为了打断单字号的
# 取整碰撞（Yu Gothic UI 的 Semibold/Bold 在 62px 的 advance 完全相同），
# 拉丁大小写/数字 + 假名/汉字的混合串保证不同字重实例必然分离。
_FINGERPRINT_TEXT = "Ag0Wg指あソ"
_FINGERPRINT_SIZES = (40, 41)


def _font_fingerprint(font: QFont) -> tuple:
    signature: list = []
    for size in _FINGERPRINT_SIZES:
        font.setPixelSize(size)
        metrics = QFontMetrics(font)
        signature.extend(metrics.horizontalAdvance(ch) for ch in _FINGERPRINT_TEXT)
        path = QPainterPath()
        path.addText(0.0, 0.0, font, _FINGERPRINT_TEXT)
        rect = path.boundingRect()
        signature.extend(
            (round(rect.left() * 8), round(rect.top() * 8),
             round(rect.right() * 8), round(rect.bottom() * 8))
        )
    font.setPixelSize(_FINGERPRINT_SIZES[0])
    return tuple(signature)


def _resolve_missing_static_weight(
    family: str,
    inventory: tuple[tuple[int, str, bool], ...],
    bucket: int,
    italic: bool,
) -> tuple[int, str, bool] | None:
    """实测 Qt 对缺失档的实际渲染目标：``(face字重, styleName, 是否合成)``。

    Qt 对静态族缺失字重的选择（就近吸附 vs 某个基 face + 合成粗体）由
    其内部匹配器打分决定，跨族结构不可预测也读不回来（QFontInfo/QRawFont
    只回显请求）。这里用公开 API 实测：把「交给 Qt 决定」的字体与每个
    候选 face 的「真实渲染 / 钉扎+加粗」构造做逐字 advance + 墨迹指纹
    比对，匹配者即 Qt 的实际选择。无匹配（字体被替换 / 度量异常）返回
    None，由调用方走就近吸附兜底。
    """
    plain = QFont(family)
    plain.setWeight(QFont.Weight(bucket))
    if italic:
        plain.setItalic(True)
    if QFontInfo(plain).family().casefold() != family.casefold():
        # 族名解析失败会静默替换默认字体，指纹毫无意义。
        return None
    target = _font_fingerprint(plain)

    candidates = [
        (weight, style) for weight, style, face_italic in inventory
        if face_italic == italic
    ] or [(weight, style) for weight, style, _face_italic in inventory]

    synthetic_hits: list[tuple[int, str]] = []
    real_hits: list[tuple[int, str]] = []
    for weight, style in candidates:
        font = QFont(family)
        font.setStyleName(style)
        if italic:
            font.setItalic(True)
        if _font_fingerprint(font) == target:
            real_hits.append((weight, style))
            continue
        if weight < bucket:
            font.setWeight(QFont.Weight(bucket))
            if _font_fingerprint(font) == target:
                synthetic_hits.append((weight, style))
    if real_hits:
        # 多个真实 face 指纹相同（理论上的同度量实例）：取字重最近者。
        best = min(real_hits, key=lambda item: (abs(item[0] - bucket), item[0]))
        return best[0], best[1], False
    if synthetic_hits:
        best = min(synthetic_hits, key=lambda item: (abs(item[0] - bucket), item[0]))
        return best[0], best[1], True
    return None


def resolve_weight_plan(
    family: str, weight: int, italic: bool = False
) -> FontWeightPlan:
    """(family, 请求字重, 请求斜体) → 权威渲染计划。"""
    requested = int(weight)
    cache_key = (str(family), requested, bool(italic))
    with _LOCK:
        cached_plan = _PLAN_CACHE.get(cache_key)
    if cached_plan is not None:
        return cached_plan
    plan = _compute_weight_plan(family, requested, bool(italic))
    with _LOCK:
        _PLAN_CACHE[cache_key] = plan
    return plan


def _compute_weight_plan(
    family: str, requested: int, italic: bool
) -> FontWeightPlan:
    axis = family_weight_axis(family)
    if axis is not None:
        value = min(max(float(requested), axis.minimum), axis.maximum)
        mark = None if value == float(requested) else "越界"
        return FontWeightPlan(
            family=family,
            requested_weight=requested,
            axis_value=value,
            base_weight=int(round(value)),
            enum_weight=bucket_weight(requested),
            italic=bool(italic),
            mark=mark,
        )

    inventory = face_inventory(family)
    # 斜体请求优先斜体 face；族内没有斜体 face 时回退直立 face 并由
    # 调用方的 setItalic 走合成斜体（与旧解析行为一致）。
    selected = [face for face in inventory if face[2] == italic]
    if not selected and italic:
        selected = list(inventory)
    if not selected:
        # 字体缺失 / headless：保持旧行为（仅桶化 setWeight）。
        bucket = bucket_weight(requested)
        return FontWeightPlan(
            family=family,
            requested_weight=requested,
            base_weight=bucket,
            enum_weight=bucket,
            italic=bool(italic),
        )

    bucket = bucket_weight(requested)
    weights = [face_weight for face_weight, _name, _face_italic in selected]
    if bucket in weights:
        base_weight, style_name, _face_italic = selected[weights.index(bucket)]
        return FontWeightPlan(
            family=family,
            requested_weight=requested,
            style_name=style_name,
            base_weight=base_weight,
            enum_weight=bucket,
            italic=bool(italic),
        )

    # 缺失档：以实测的 Qt 实际渲染目标为权威（含合成粗体——旧版模拟
    # 字重语义；Qt 选哪个基 face 跨族不可预测，必须指纹实测）。
    resolved = _resolve_missing_static_weight(family, inventory, bucket, italic)
    if resolved is not None:
        base_weight, style_name, synthetic = resolved
        return FontWeightPlan(
            family=family,
            requested_weight=requested,
            style_name=style_name,
            base_weight=base_weight,
            synthetic_bold=synthetic,
            enum_weight=bucket,
            italic=bool(italic),
            mark="模拟" if synthetic else "就近",
        )

    # 指纹无匹配（被替换字体 / 度量异常）：就近吸附兜底（平局取较轻）。
    base_weight = min(weights, key=lambda value: (abs(value - bucket), value))
    base_weight, style_name, _face_italic = selected[weights.index(base_weight)]
    return FontWeightPlan(
        family=family,
        requested_weight=requested,
        style_name=style_name,
        base_weight=base_weight,
        enum_weight=bucket,
        italic=bool(italic),
        mark="就近",
    )


def apply_weight_plan(font: QFont, plan: FontWeightPlan) -> None:
    """把权威解析结果应用到 QFont（调用方已设 family/pixelSize）。"""
    font.setItalic(bool(plan.italic))
    if plan.axis_value is not None:
        font.setVariableAxis(QFont.Tag(_AXIS_TAG_WEIGHT), float(plan.axis_value))
        return
    if plan.style_name is not None:
        font.setStyleName(plan.style_name)
    if plan.synthetic_bold:
        font.setWeight(QFont.Weight(plan.enum_weight))
    elif plan.style_name is None:
        # 元数据缺失的兜底：维持旧的桶化 setWeight 行为。
        font.setWeight(QFont.Weight(plan.enum_weight))


def build_weight_font(
    family: str, size_px: int, weight: int, italic: bool = False
) -> QFont:
    """按统一口径构造 QFont（family 已是 resolve_qt_font_family 的结果）。"""
    font = QFont(family, max(int(size_px), 1))
    font.setPixelSize(max(int(size_px), 1))
    apply_weight_plan(
        font, resolve_weight_plan(family, weight, italic=bool(italic))
    )
    return font


__all__ = [
    "FontWeightPlan",
    "WeightAxis",
    "apply_weight_plan",
    "bucket_weight",
    "build_weight_font",
    "clear_font_weight_cache",
    "face_inventory",
    "family_weight_axis",
    "physical_weight_styles",
    "resolve_weight_plan",
]
