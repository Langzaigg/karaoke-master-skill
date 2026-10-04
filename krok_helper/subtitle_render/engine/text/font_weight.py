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

from PyQt6.QtGui import QFont, QFontDatabase, QRawFont

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
_LOCK = threading.Lock()


def clear_font_weight_cache() -> None:
    """字体安装/卸载后清空进程级缓存（调用方：宿主字体库刷新）。"""
    with _LOCK:
        _AXIS_CACHE.clear()
        _FACE_CACHE.clear()


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


def resolve_weight_plan(
    family: str, weight: int, italic: bool = False
) -> FontWeightPlan:
    """(family, 请求字重, 请求斜体) → 权威渲染计划。"""
    requested = int(weight)
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
    selected = [face for face in inventory if face[2] == bool(italic)]
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

    single_weight, single_style, _single_italic = selected[0]
    if len(selected) == 1 and bucket >= 600 and bucket > single_weight:
        return FontWeightPlan(
            family=family,
            requested_weight=requested,
            style_name=single_style,
            base_weight=single_weight,
            synthetic_bold=True,
            enum_weight=bucket,
            italic=bool(italic),
            mark="模拟",
        )

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
