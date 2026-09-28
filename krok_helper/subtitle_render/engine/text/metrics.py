"""Qt-backed font selection and N3-compatible text geometry measurements."""

from __future__ import annotations

import math
from collections.abc import Callable

from PyQt6.QtGui import QFont, QFontMetrics, QPainterPath

from krok_helper.subtitle_render.engine.layout.layout_context import _LAYOUT_PASS
from krok_helper.subtitle_render.domain.models import Style
from krok_helper.subtitle_render.n3.font_catalog import resolve_qt_font_family


FontSelector = Callable[[str], QFont]


def clamp_weight(weight: int) -> QFont.Weight:
    if weight <= 250:
        return QFont.Weight.Thin
    if weight <= 350:
        return QFont.Weight.Light
    if weight <= 450:
        return QFont.Weight.Normal
    if weight <= 550:
        return QFont.Weight.Medium
    if weight <= 650:
        return QFont.Weight.DemiBold
    if weight <= 750:
        return QFont.Weight.Bold
    if weight <= 850:
        return QFont.Weight.ExtraBold
    return QFont.Weight.Black


def build_font(style: Style) -> QFont:
    font = QFont(
        resolve_qt_font_family(style.font_family),
        max(style.font_size_px, 1),
    )
    font.setPixelSize(max(style.font_size_px, 1))
    font.setWeight(clamp_weight(style.font_weight))
    font.setItalic(style.italic)
    return font


def latin_font_size(style: Style) -> int:
    value = style.latin_font_size_px
    return int(value) if value is not None and int(value) > 0 else int(style.font_size_px)


def latin_font_weight(style: Style) -> int:
    value = style.latin_font_weight
    return int(value) if value is not None and int(value) > 0 else int(style.font_weight)


_TEXT_CLASS_CACHE: dict[str, tuple[bool, bool]] = {}
_TEXT_CLASS_CACHE_MAX = 8192


def _text_class(text: str) -> tuple[bool, bool]:
    """Memoized (is_n3_latin_text, is_emoji_text) classification.

    字符测量热路径对同一字符反复分类（字体选择、度量源选择各查一次），
    ``all()/any()`` 生成器遍历比一次 dict 查找贵得多；字符表有限，
    模块级缓存稳定后全命中。多字符串键按整串记录，语义与逐字符判定
    一致（is_n3_latin_text 本就要求全字符命中）。
    """

    cached = _TEXT_CLASS_CACHE.get(text)
    if cached is not None:
        return cached
    classification = (is_n3_latin_text(text), is_emoji_text(text))
    if len(_TEXT_CLASS_CACHE) >= _TEXT_CLASS_CACHE_MAX:
        _TEXT_CLASS_CACHE.clear()
    _TEXT_CLASS_CACHE[text] = classification
    return classification


def is_n3_latin_text(text: str) -> bool:
    return bool(text) and all(
        ("0" <= char <= "9" or "A" <= char <= "Z" or "a" <= char <= "z")
        or ("\u00c0" <= char <= "\u00d6"
            or "\u00d8" <= char <= "\u00f6"
            or "\u00f8" <= char <= "\u00ff")
        for char in text
    )


def is_emoji_text(text: str) -> bool:
    return any(
        0x1F000 <= ord(char) <= 0x1FAFF or 0x2600 <= ord(char) <= 0x27BF
        for char in text
    )


def _build_emoji_font(style: Style) -> QFont:
    font = QFont("Segoe UI Symbol", max(int(style.font_size_px), 1))
    font.setPixelSize(max(int(style.font_size_px), 1))
    font.setWeight(clamp_weight(style.font_weight))
    font.setItalic(style.italic)
    return font


def build_latin_font(style: Style) -> QFont:
    family = style.font_family_latin or style.font_family
    size = max(latin_font_size(style), 1)
    font = QFont(resolve_qt_font_family(family), size)
    font.setPixelSize(size)
    font.setWeight(clamp_weight(latin_font_weight(style)))
    font.setItalic(style.italic)
    if int(style.latin_font_stretch_pct) != 100:
        font.setStretch(max(50, min(200, int(style.latin_font_stretch_pct))))
    return font


def make_font_for(
    style: Style,
    jp_font: QFont,
    latin_font: QFont,
) -> FontSelector:
    emoji_font = _build_emoji_font(style)
    same_text_fonts = _font_signature(latin_font) == _font_signature(jp_font)

    def font_for(text: str) -> QFont:
        is_latin, is_emoji = _text_class(text)
        if is_emoji:
            return emoji_font
        return latin_font if not same_text_fonts and is_latin else jp_font

    return font_for


def char_advance(
    text: str,
    metrics: QFontMetrics,
    latin_metrics: QFontMetrics,
    font_for: FontSelector | None,
) -> int:
    cache = getattr(_LAYOUT_PASS, "char_advances", None)
    if cache is None:
        if font_for is not None and is_emoji_text(text):
            return QFontMetrics(font_for(text)).horizontalAdvance(text)
        if font_for is not None and is_n3_latin_text(text):
            return latin_metrics.horizontalAdvance(text)
        return metrics.horizontalAdvance(text)
    use_emoji = False
    use_latin = False
    if font_for is not None:
        use_latin, use_emoji = _text_class(text)
    if use_emoji:
        emoji_font = font_for(text)
        source = QFontMetrics(emoji_font)
        source_key = ("emoji", _font_signature(emoji_font))
    else:
        source = latin_metrics if use_latin else metrics
        source_key = id(source)
    cache_key = (text, source_key)
    hit = cache.get(cache_key)
    if hit is None:
        hit = source.horizontalAdvance(text)
        cache[cache_key] = hit
        _LAYOUT_PASS.metrics.append(source)
    return hit


def char_ink_width(
    text: str,
    font: QFont,
    metrics: QFontMetrics,
    latin_metrics: QFontMetrics,
    font_for: FontSelector | None,
) -> int:
    """Return the glyph's horizontal ink width (0 for blank characters).

    与 Painter 的 ``_char_ink_x_ranges`` 同口径（``QPainterPath.addText`` 的
    矢量包围盒）：多 checkpoint leader 共享块的保底分摊用它计权——走字时长
    正比于要扫过的墨水量，全角标点（顿号等 advance=1em 但墨水极窄）按 advance
    计权会把末段假名挤压到比标点还短。
    """
    if not text or text.isspace():
        return 0
    use_latin, use_emoji = (
        _text_class(text) if font_for is not None else (False, False)
    )
    source_font = font_for(text) if use_emoji else None
    cache = getattr(_LAYOUT_PASS, "char_ink_widths", None)
    source_key: object
    if source_font is not None:
        source_key = ("emoji", _font_signature(source_font))
    elif use_latin:
        source_font = font_for(text)
        source_key = ("latin", _font_signature(source_font))
    else:
        source_font = font
        source_key = _font_signature(font)
    cache_key = (text, source_key)
    if cache is not None and cache_key in cache:
        return cache[cache_key]
    path = QPainterPath()
    path.addText(0.0, 0.0, source_font, text)
    rect = path.boundingRect()
    width = 0 if rect.isEmpty() else max(int(math.ceil(rect.width())), 0)
    if cache is not None:
        cache[cache_key] = width
    return width


_CHAR_GLYPH_CACHE: dict[tuple, tuple] = {}
_CHAR_GLYPH_CACHE_MAX = 16384


def clear_char_metric_cache() -> None:
    _CHAR_GLYPH_CACHE.clear()


def n3_char_box_ascent(
    metrics: QFontMetrics,
    font_size_px: int,
    stroke_width: int,
) -> float:
    """Return the N3 character-box height above the baseline."""
    ascent = max(metrics.ascent(), 0)
    descent = max(metrics.descent(), 0)
    total = max(ascent + descent, 1)
    return max(font_size_px, 1) * ascent / total + max(stroke_width, 0) / 2.0


def n3_char_box_descent(
    metrics: QFontMetrics,
    font_size_px: int,
    stroke_width: int,
) -> float:
    """Return the N3 character-box height below the baseline."""
    ascent = max(metrics.ascent(), 0)
    descent = max(metrics.descent(), 0)
    total = max(ascent + descent, 1)
    return max(font_size_px, 1) * descent / total + max(stroke_width, 0) / 2.0


def _font_signature(font: QFont) -> tuple:
    return (font.family(), font.pixelSize(), int(font.weight()), font.italic(), font.stretch())


def _char_glyph_metrics(
    text: str,
    glyph_font: QFont,
    metrics: QFontMetrics,
    latin_metrics: QFontMetrics,
    font_for: FontSelector | None,
) -> tuple[int, bool, float, float, int, int]:
    """Font-determined glyph geometry: advance, ink box and bearings.

    只依赖 ``(text, glyph_font)``，与描边宽/字间距/空格宽等纯算术参数
    无关——底层按字体签名缓存后，调描边宽、改字间距不再重付
    ``QPainterPath.addText`` 的矢量测量。
    """

    key = (text, _font_signature(glyph_font))
    cached = _CHAR_GLYPH_CACHE.get(key)
    if cached is not None:
        return cached
    advance = char_advance(text, metrics, latin_metrics, font_for)
    path = QPainterPath()
    if text:
        path.addText(0.0, 0.0, glyph_font, text)
    bounds = path.boundingRect()
    bounds_empty = bounds.isEmpty()
    bounds_width = float(bounds.width())
    bounds_left = float(bounds.left())
    if bounds_empty:
        left_bearing = right_bearing = 0
    else:
        use_latin, use_emoji = _text_class(text)
        glyph_metrics = (
            QFontMetrics(glyph_font)
            if use_emoji
            else latin_metrics
            if font_for is not None and use_latin
            else metrics
        )
        try:
            left_bearing = glyph_metrics.leftBearing(text)
            right_bearing = glyph_metrics.rightBearing(text)
        except (TypeError, ValueError):
            left_bearing = int(bounds.left())
            right_bearing = int(advance - bounds.right())
    entry = (advance, bounds_empty, bounds_width, bounds_left, left_bearing, right_bearing)
    if len(_CHAR_GLYPH_CACHE) >= _CHAR_GLYPH_CACHE_MAX:
        _CHAR_GLYPH_CACHE.clear()
    _CHAR_GLYPH_CACHE[key] = entry
    return entry


def truncate_div(numerator: int, denominator: int) -> int:
    """Integer division truncated toward zero, matching C# arithmetic."""
    if denominator == 0:
        return 0
    sign = -1 if (numerator < 0) != (denominator < 0) else 1
    return sign * (abs(numerator) // abs(denominator))


def nicokara_layout_width(
    ink_width: int,
    advance: int,
    left_bearing: int,
    right_bearing: int,
    *,
    edge_size: int,
    allow_biting: bool,
) -> int:
    advance = max(int(advance), 1)
    left = int(left_bearing)
    right = int(right_bearing)
    if not allow_biting:
        left = max(left, 0)
        right = max(right, 0)
    body_width = truncate_div(
        max(int(ink_width), 0) * (left + advance + right),
        advance,
    )
    return max(body_width, 0) + max(int(edge_size), 0)


def nicokara_char_geometry_left_offset(
    ink_width: int,
    advance: int,
    left_bearing: int,
    *,
    allow_biting: bool,
) -> int:
    advance = max(int(advance), 1)
    left = int(left_bearing)
    if not allow_biting:
        left = max(left, 0)
    return truncate_div(max(int(ink_width), 0) * left, advance)


def _char_layout_metrics(
    text: str,
    font: QFont,
    metrics: QFontMetrics,
    latin_metrics: QFontMetrics,
    font_for: FontSelector | None,
    style: Style,
) -> tuple[int, float]:
    is_latin_glyph = font_for is not None and _text_class(text)[0]
    glyph_font = font_for(text) if font_for is not None else font
    font_size = glyph_font.pixelSize()
    if font_size <= 0:
        font_size = max(
            latin_font_size(style) if is_latin_glyph else int(style.font_size_px),
            1,
        )
    space_percent = max(10, min(int(style.space_width_percent), 100))
    edge_size = max(int(style.stroke_width_px), 0)

    if text == " ":
        return font_size * space_percent // 100, 0.0

    (
        advance,
        bounds_empty,
        bounds_width,
        bounds_left,
        left_bearing,
        right_bearing,
    ) = _char_glyph_metrics(text, glyph_font, metrics, latin_metrics, font_for)

    if bounds_empty:
        body_width = font_size * space_percent * 25 // 100 // 10
        return max(body_width, 0) + edge_size, 0.0

    allow_biting = bool(style.allow_biting)
    width = nicokara_layout_width(
        int(bounds_width),
        advance,
        left_bearing,
        right_bearing,
        edge_size=edge_size,
        allow_biting=allow_biting,
    )
    geometry_left = nicokara_char_geometry_left_offset(
        int(bounds_width),
        advance,
        left_bearing,
        allow_biting=allow_biting,
    )
    offset = (
        -bounds_left
        + float(geometry_left)
        + edge_size / 2.0
    )
    return width, offset


def char_path_left_offset(
    text: str,
    font: QFont,
    metrics: QFontMetrics,
    latin_metrics: QFontMetrics,
    font_for: FontSelector | None,
    style: Style,
) -> float:
    if not text or text.isspace():
        return 0.0
    return _cached_char_layout_metrics(
        text, font, metrics, latin_metrics, font_for, style
    )[1]


def char_layout_width(
    text: str,
    font: QFont,
    metrics: QFontMetrics,
    latin_metrics: QFontMetrics,
    font_for: FontSelector | None,
    style: Style,
) -> int:
    return _cached_char_layout_metrics(
        text, font, metrics, latin_metrics, font_for, style
    )[0]


def _cached_char_layout_metrics(
    text: str,
    font: QFont,
    metrics: QFontMetrics,
    latin_metrics: QFontMetrics,
    font_for: FontSelector | None,
    style: Style,
) -> tuple[int, float]:
    """Pass-scoped memo over :func:`_char_layout_metrics`.

    同一 ``(text, style)`` 的宽度/偏移在一次 :func:`layout_pass` 区间内
    会被行宽测量、行布局构建、区间解析等多个调用点重复请求；font 与
    metrics 都是 style 的纯函数，结果只取决于 ``(text, style)``。
    """

    cache = getattr(_LAYOUT_PASS, "char_layout_metrics", None)
    if cache is None:
        return _char_layout_metrics(text, font, metrics, latin_metrics, font_for, style)
    key = (text, id(style))
    hit = cache.get(key)
    if hit is None:
        hit = _char_layout_metrics(text, font, metrics, latin_metrics, font_for, style)
        cache[key] = hit
        # 键里有 id()：存住入参，避免回收后地址被复用。
        _LAYOUT_PASS.styles.append(style)
    return hit


def letter_spacing(style: Style) -> int:
    return int(style.letter_spacing_px)


def line_text_width(char_widths: list[int], style: Style) -> int:
    if not char_widths:
        return 0
    spacing = letter_spacing(style)
    total = int(char_widths[-1])
    for width in char_widths[:-1]:
        # 零宽单元格（负余白压瘪的 guide 占位）在布局里完全隐形，
        # 不占字间距——否则分色行比纯文本行凭空多出一份 spacing。
        if int(width) <= 0:
            continue
        total += (
            max(int(width) + spacing, 0)
            if style.layout_semantics == "n3_1074"
            else int(width) + spacing
        )
    return max(0, total)


__all__ = [
    "FontSelector",
    "build_font",
    "build_latin_font",
    "char_advance",
    "char_ink_width",
    "char_layout_width",
    "char_path_left_offset",
    "clamp_weight",
    "clear_char_metric_cache",
    "is_emoji_text",
    "is_n3_latin_text",
    "latin_font_size",
    "latin_font_weight",
    "letter_spacing",
    "line_text_width",
    "make_font_for",
    "n3_char_box_ascent",
    "n3_char_box_descent",
    "nicokara_char_geometry_left_offset",
    "nicokara_layout_width",
    "truncate_div",
]
