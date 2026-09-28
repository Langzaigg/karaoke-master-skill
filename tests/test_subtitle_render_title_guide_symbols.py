"""标题条目图片导唱符（行前导唱符 + 逐字符替换）的回归测试。

标题永不走字：位图导唱符恒取「走字前」一侧图片；GPU sidecar 尚无对应
实现，检测到启用条目带导唱符时必须整帧回退 Painter。
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from PyQt6.QtGui import QColor, QImage
from PyQt6.QtWidgets import QApplication

from krok_helper.subtitle_render.domain.models import (
    Style,
    TitleOverlay,
    migrate_title_guide_symbols,
    normalize_title_guide_symbols,
    style_from_dict,
    style_to_dict,
    title_overlay_from_dict,
    title_overlay_to_dict,
)
from krok_helper.subtitle_render.domain.timing import (
    GuideSymbol,
    TimingChar,
    TimingLine,
    TimingTrack,
    TimingTrackMeta,
)
from krok_helper.subtitle_render.engine.painter import paint_frame
from krok_helper.subtitle_render.engine.render.elements.title import (
    layout_title_overlay,
)
from krok_helper.subtitle_render.engine.style.title_semantics import (
    resolve_title_overlay,
)
from krok_helper.subtitle_render.frontend.editor.lyrics_list import (
    LyricsPanel,
    _line_content_text,
)
from krok_helper.subtitle_render.native.protocol import gpu_unsupported_features
from krok_helper.subtitle_render.settings.property_controllers import (
    TitleOverlaysController,
)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _title_track() -> TimingTrack:
    line = TimingLine(
        chars=[TimingChar(text="あ", start_ms=2000), TimingChar(text="い", start_ms=2500)],
        end_ms=30000,
    )
    return TimingTrack(meta=TimingTrackMeta(title="曲名", artist="歌手"), lines=[line])


def _color_png(path, color: str, size: int = 10) -> str:
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QColor(color))
    assert image.save(str(path))
    return str(path)


def _bitmap_symbol(before: str | None, after: str | None = None, **kwargs) -> GuideSymbol:
    return GuideSymbol(
        kind="bitmap",
        bitmap_before_path=before,
        bitmap_after_path=after,
        **kwargs,
    )


def _plain_style(**title_kwargs) -> Style:
    """清空配色方案 + 悬空布局引用：让 TitleOverlay 自身字段生效。"""
    return Style(
        custom_style_schemes={},
        title_overlays=[TitleOverlay(enabled=True, layout_index=None, **title_kwargs)],
    )


# ---------------------------------------------------------------------------
# 域模型：归一化 / 迁移 / 序列化
# ---------------------------------------------------------------------------


def test_normalize_title_guide_symbols_drops_out_of_range_entries():
    symbol = _bitmap_symbol("a.png")
    rows, inline = normalize_title_guide_symbols(
        "AB\ncd",
        {1: symbol, 7: symbol, "x": symbol},
        {(0, 1): symbol, (0, 9): symbol, (5, 0): symbol, (1, 2): symbol},
    )
    assert rows == {1: symbol}
    assert inline == {(0, 1): symbol}


def test_migrate_title_guide_symbols_keeps_surviving_chars():
    row_symbol = _bitmap_symbol("row.png")
    inline_a = _bitmap_symbol("a.png")
    inline_b = _bitmap_symbol("b.png")
    rows, inline = migrate_title_guide_symbols(
        "AB\ncd",
        {1: row_symbol},
        {(0, 1): inline_a, (1, 0): inline_b},
        "AXB\ncd",
    )
    # "AB" → "AXB"：B 的图片跟到新下标 2；第二行原样保留，行前符号跟着行走。
    assert rows == {1: row_symbol}
    assert inline == {(0, 2): inline_a, (1, 0): inline_b}

    same_rows, same_inline = migrate_title_guide_symbols(
        "AB", {}, {(0, 0): inline_a}, "AB"
    )
    assert same_rows == {} and same_inline == {(0, 0): inline_a}


def test_title_guide_symbols_roundtrip_through_style_dict(tmp_path):
    before = _color_png(tmp_path / "before.png", "#FF0000")
    after = _color_png(tmp_path / "after.png", "#00FF00")
    row_symbol = replace(_bitmap_symbol(before, after), count=2)
    inline_symbol = _bitmap_symbol(before)
    title = TitleOverlay(
        text_template="AB\ncd",
        guide_symbols={1: row_symbol},
        inline_guide_symbols={(0, 1): inline_symbol, (1, 1): inline_symbol},
    )
    restored = title_overlay_from_dict(title_overlay_to_dict(title))
    assert restored.guide_symbols == {1: row_symbol}
    assert restored.inline_guide_symbols == {(0, 1): inline_symbol, (1, 1): inline_symbol}

    style = style_from_dict(style_to_dict(Style(title_overlays=[title])))
    assert style.title_overlays[0].guide_symbols == {1: row_symbol}
    assert style.title_overlays[0].inline_guide_symbols == {
        (0, 1): inline_symbol,
        (1, 1): inline_symbol,
    }


def test_title_controller_migrates_guide_symbols_on_text_edit(tmp_path):
    symbol = _bitmap_symbol(_color_png(tmp_path / "x.png", "#FF0000"))
    controller = TitleOverlaysController()
    style = Style(
        title_overlays=[
            TitleOverlay(
                text_template="AB",
                guide_symbols={0: symbol},
                inline_guide_symbols={(0, 1): symbol},
            )
        ]
    )
    updated = controller.update(style, 0, {"text_template": "A B"})
    title = updated.title_overlays[0]
    assert title.guide_symbols == {0: symbol}
    assert title.inline_guide_symbols == {(0, 2): symbol}


# ---------------------------------------------------------------------------
# GPU 回退
# ---------------------------------------------------------------------------


def test_gpu_renders_title_guide_symbols_natively(tmp_path):
    """标题导唱符由 GPU sidecar 原生渲染，不触发整帧 Painter 回退。"""
    symbol = _bitmap_symbol(_color_png(tmp_path / "x.png", "#FF0000"))
    track = _title_track()
    assert gpu_unsupported_features(track, Style()) == ()

    enabled = _plain_style(inline_guide_symbols={(0, 0): symbol})
    assert gpu_unsupported_features(track, enabled) == ()

    row_only = _plain_style(guide_symbols={0: symbol})
    assert gpu_unsupported_features(track, row_only) == ()


# ---------------------------------------------------------------------------
# 布局与绘制
# ---------------------------------------------------------------------------


def test_title_layout_materializes_inline_and_prefix_guide_glyphs(qapp, tmp_path):
    symbol = _bitmap_symbol(_color_png(tmp_path / "x.png", "#0000FF", size=10))
    prefix = replace(symbol, count=2)
    style = _plain_style(
        text_template="あい",
        font_size_px=48,
        guide_symbols={0: prefix},
        inline_guide_symbols={(0, 1): symbol},
    )
    resolved = resolve_title_overlay(style)
    layout = layout_title_overlay(1920, 1080, _title_track(), resolved, style=style)
    assert layout is not None
    row = layout.glyph_rows[0]
    # 行前导唱符 count=2 → 两个虚拟字形插在正文之前。
    assert [glyph.guide_symbol for glyph in row[:2]] == [prefix, prefix]
    assert row[0].text == "\uFFFC"
    # 正文第 1 字被行内替换；正方形图片按字号缩放 → 48×48，advance 同宽。
    assert row[3].guide_symbol is symbol
    assert row[2].guide_symbol is None
    assert row[3].advance == pytest.approx(48.0)
    assert row[0].advance == pytest.approx(48.0)
    # 布局宽度包含导唱符：两个行前 + 「あ」 + 48px 图片。
    plain = layout_title_overlay(
        1920,
        1080,
        _title_track(),
        resolve_title_overlay(_plain_style(text_template="あい", font_size_px=48)),
        style=_plain_style(text_template="あい", font_size_px=48),
    )
    assert plain is not None
    assert layout.widths[0] > plain.widths[0]


def _paint_title(qapp, style: Style, tmp_path) -> tuple[int, int]:
    """渲染一帧并数红 / 绿像素（走字前 / 走字后图片各自的命中数）。"""
    image = QImage(800, 450, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QColor("#101010"))
    paint_frame(image, _title_track(), 1000, style)
    red = green = 0
    for y in range(image.height()):
        for x in range(image.width()):
            color = QColor(image.pixel(x, y))
            if color.red() > 200 and color.green() < 60 and color.blue() < 60:
                red += 1
            elif color.green() > 200 and color.red() < 60 and color.blue() < 60:
                green += 1
    return red, green


def test_title_paint_uses_before_image_only(qapp, tmp_path):
    before = _color_png(tmp_path / "before.png", "#FF0000")
    after = _color_png(tmp_path / "after.png", "#00FF00")
    symbol = _bitmap_symbol(before, after)
    style = _plain_style(
        text_template="あい",
        font_size_px=48,
        inline_guide_symbols={(0, 0): symbol},
    )
    red, green = _paint_title(qapp, style, tmp_path)
    # 标题永不走字：只画「走字前」红图，绝不出现走字后绿图。
    assert red > 50
    assert green == 0

    plain_red, plain_green = _paint_title(
        qapp, _plain_style(text_template="あい", font_size_px=48), tmp_path
    )
    assert plain_red == 0 and plain_green == 0


def test_title_paint_row_prefix_guide_symbol(qapp, tmp_path):
    before = _color_png(tmp_path / "before.png", "#FF0000")
    symbol = replace(_bitmap_symbol(before), count=1)
    style = _plain_style(
        text_template="あい",
        font_size_px=48,
        guide_symbols={0: symbol},
    )
    red, _green = _paint_title(qapp, style, tmp_path)
    assert red > 50


def test_title_guide_symbol_layer_key_separates_symbols(qapp, tmp_path):
    from krok_helper.subtitle_render.engine.render.elements.title import (
        title_overlay_layer_key,
    )
    from krok_helper.subtitle_render.engine.render.effects import fill_signature

    before_a = _color_png(tmp_path / "a.png", "#FF0000")
    before_b = _color_png(tmp_path / "b.png", "#0000FF")
    track = _title_track()
    style_a = _plain_style(
        text_template="あい", inline_guide_symbols={(0, 0): _bitmap_symbol(before_a)}
    )
    style_b = _plain_style(
        text_template="あい", inline_guide_symbols={(0, 0): _bitmap_symbol(before_b)}
    )
    layout_a = layout_title_overlay(
        1920, 1080, track, resolve_title_overlay(style_a), style=style_a
    )
    layout_b = layout_title_overlay(
        1920, 1080, track, resolve_title_overlay(style_b), style=style_b
    )
    assert layout_a is not None and layout_b is not None
    key_a = title_overlay_layer_key(
        layout_a, resolve_title_overlay(style_a), fill_signature=fill_signature
    )
    key_b = title_overlay_layer_key(
        layout_b, resolve_title_overlay(style_b), fill_signature=fill_signature
    )
    assert key_a != key_b


# ---------------------------------------------------------------------------
# 歌词列表（标题模式）
# ---------------------------------------------------------------------------


def test_lyrics_list_title_mode_carries_guide_symbols(qapp, tmp_path):
    symbol = _bitmap_symbol(_color_png(tmp_path / "x.png", "#FF0000"))
    prefix = replace(symbol, count=1)
    panel = LyricsPanel()
    try:
        panel.set_title(
            TitleOverlay(
                text_template="AB",
                guide_symbols={0: prefix},
                inline_guide_symbols={(0, 1): symbol},
            )
        )
        line = panel._track.lines[0]
        assert line.guide_symbol == prefix
        assert line.inline_guide_symbols == {1: symbol}
        # 内容列预览与歌词同口径：被替换的字符显示 ◆ 占位。
        assert _line_content_text(line) == "A◆"
    finally:
        panel.deleteLater()
