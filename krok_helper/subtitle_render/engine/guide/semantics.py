"""Frame-independent conversion of authored guide symbols into render glyphs."""

from __future__ import annotations

from dataclasses import replace

from krok_helper.subtitle_render.domain.timing import (
    GuideSymbol,
    TimingChar,
    TimingLine,
    guide_symbol_has_visual,
    guide_symbol_replacement_count,
    guide_symbol_role_labels,
)
from krok_helper.subtitle_render.engine.layout.layout_context import _LAYOUT_PASS


def render_line_with_guide_symbols(line: TimingLine) -> TimingLine:
    """Return a render-only line with prefix and inline guides materialized.

    同一 :func:`layout_pass` 区间内按行身份缓存：display 解析、行宽测量、
    区间解析与 IR 序列化会对同一行反复替换（每次都重算 guide 替换计数并
    replace 出新对象）；区间契约保证行不可变，身份键即语义键。
    """

    if not line.chars:
        return line
    cache = getattr(_LAYOUT_PASS, "render_lines", None)
    if cache is None:
        return _render_line_with_guide_symbols_uncached(line)
    key = id(line)
    hit = cache.get(key)
    if hit is None:
        hit = _render_line_with_guide_symbols_uncached(line)
        cache[key] = hit
        # 键里有 id()：存住入参，避免回收后地址被复用。
        _LAYOUT_PASS.lines.append(line)
    return hit


def _render_line_with_guide_symbols_uncached(line: TimingLine) -> TimingLine:
    symbol = line.guide_symbol
    replacement_count = guide_symbol_replacement_count(line, symbol)
    chars = list(line.chars)
    inline_changed = False
    for index, inline_symbol in line.inline_guide_symbols.items():
        if (
            isinstance(index, int)
            and 0 <= index < len(chars)
            and isinstance(inline_symbol, GuideSymbol)
            and guide_symbol_has_visual(inline_symbol)
        ):
            chars[index] = replace(
                chars[index], text="\uFFFC", vector_glyph=inline_symbol
            )
            inline_changed = True
    render_line = (
        replace(line, chars=chars, inline_guide_symbols={})
        if inline_changed
        else line
    )
    symbol = render_line.guide_symbol
    if symbol is None or not guide_symbol_has_visual(symbol):
        return render_line
    if symbol.replacement_prefix:
        if replacement_count == 0:
            return render_line
        labels = guide_symbol_role_labels(symbol)
        guides = [
            TimingChar(
                text="\uFFFC",
                start_ms=int(source.start_ms),
                pause_release_ms=source.pause_release_ms,
                explicit_start=source.explicit_start,
                explicit_end=source.explicit_end,
                role_label=(
                    labels[index] if index < len(labels) else source.role_label
                ),
                vector_glyph=symbol,
            )
            for index, source in enumerate(render_line.chars[:replacement_count])
        ]
        return replace(
            render_line,
            chars=[*guides, *render_line.chars[replacement_count:]],
            guide_symbol=None,
            inline_guide_symbols={},
        )
    first_start = int(render_line.chars[0].start_ms)
    interval = max(int(symbol.duration_ms), 0)
    labels = guide_symbol_role_labels(symbol)
    guides = [
        TimingChar(
            text="\uFFFC",
            start_ms=(
                first_start
                if symbol.prefix_timing == "anchored"
                else first_start - interval * (len(labels) - index)
            ),
            role_label=label,
            vector_glyph=symbol,
        )
        for index, label in enumerate(labels)
    ]
    return replace(
        render_line,
        chars=[*guides, *render_line.chars],
        guide_symbol=None,
        inline_guide_symbols={},
    )


def guide_symbol_is_bitmap(symbol: object | None) -> bool:
    """Return whether a guide renders through a bitmap before/after image."""
    return isinstance(symbol, GuideSymbol) and symbol.kind == "bitmap" and bool(
        symbol.bitmap_before_path or symbol.bitmap_after_path
    )


__all__ = ["guide_symbol_is_bitmap", "render_line_with_guide_symbols"]
