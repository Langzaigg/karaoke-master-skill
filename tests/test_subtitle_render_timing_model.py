from __future__ import annotations

from krok_helper.subtitle_render.domain.timing import (
    GuideSymbol,
    RubyAnnotation,
    TimingChar,
    TimingLine,
    TimingTrack,
    guide_symbol_replacement_count,
    line_visible_chars,
    normalize_combining_chars,
    normalize_ruby_annotation_text,
    timing_line_start_ms,
)
from krok_helper.subtitle_render.serialization.timing import (
    guide_symbol_from_dict,
    guide_symbol_to_dict,
    line_animation_override_from_dict,
    line_animation_override_to_dict,
    subtitle_loading_settings_from_dict,
    subtitle_loading_settings_to_dict,
    track_page_plan_from_dict,
    track_page_plan_to_dict,
)


def test_timing_model_preserves_guide_prefix_semantics() -> None:
    symbol = GuideSymbol(
        count=2,
        duration_ms=100,
        replacement_prefix=("●", "●"),
    )
    line = TimingLine(
        chars=[
            TimingChar("●", 1_000),
            TimingChar("●", 1_100),
            TimingChar("歌", 1_200),
        ],
        guide_symbol=symbol,
    )

    assert guide_symbol_replacement_count(line) == 2
    assert [char.text for char in line_visible_chars(line)] == ["歌"]
    assert timing_line_start_ms(line) == 1_000


def test_timing_track_options_preserve_first_seen_order() -> None:
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar("a", 0, role_label="主唱")],
                singer_id=2,
                singer_label="甲",
            ),
            TimingLine(
                chars=[TimingChar("b", 1, role_label="和声")],
                singer_id=1,
                singer_label="乙",
            ),
            TimingLine(
                chars=[TimingChar("c", 2, role_label="主唱")],
                singer_id=2,
                singer_label="甲",
            ),
        ]
    )

    assert track.singer_options == [(2, "甲"), (1, "乙")]
    assert track.role_options == ["主唱", "和声"]


def test_models_keeps_timing_compatibility_exports() -> None:
    from krok_helper.subtitle_render import models

    assert models.GuideSymbol is GuideSymbol
    assert models.TimingChar is TimingChar
    assert models.TimingLine is TimingLine
    assert models.TimingTrack is TimingTrack
    assert models.line_visible_chars is line_visible_chars
    assert models.timing_line_start_ms is timing_line_start_ms


def test_models_keeps_timing_codec_compatibility_exports() -> None:
    from krok_helper.subtitle_render import models

    assert models.guide_symbol_from_dict is guide_symbol_from_dict
    assert models.guide_symbol_to_dict is guide_symbol_to_dict
    assert models.line_animation_override_from_dict is line_animation_override_from_dict
    assert models.line_animation_override_to_dict is line_animation_override_to_dict
    assert models.subtitle_loading_settings_from_dict is subtitle_loading_settings_from_dict
    assert models.subtitle_loading_settings_to_dict is subtitle_loading_settings_to_dict
    assert models.track_page_plan_from_dict is track_page_plan_from_dict
    assert models.track_page_plan_to_dict is track_page_plan_to_dict


# ---------------------------------------------------------------------------
# 组合记号规范化（issue #14：分解形浊点 U+3099/U+309A）
# ---------------------------------------------------------------------------


def _guide_symbol_for_test() -> GuideSymbol:
    return GuideSymbol(
        name="测试符",
        path_commands=(("M", (0.0, 0.0)), ("L", (100.0, 100.0))),
    )


def test_normalize_combining_chars_merges_dakuten_into_base_cell() -> None:
    line = TimingLine(
        chars=[
            TimingChar(text="テ", start_ms=1000),
            TimingChar(text="\u3099", start_ms=1500),
            TimingChar(text="こ", start_ms=2000),
        ],
        end_ms=2500,
    )
    line.inline_guide_symbols[2] = _guide_symbol_for_test()
    ruby = RubyAnnotation(
        kanji="こ",
        reading="こ",
        pos_start_ms=2000,
        pos_end_ms=2500,
        target_line_index=0,
        target_char_start=2,
        target_char_end=3,
    )
    track = TimingTrack(lines=[line], rubies=[ruby])

    normalize_combining_chars(track)

    assert [c.text for c in line.chars] == ["デ", "こ"]
    assert [c.start_ms for c in line.chars] == [1000, 2000]
    assert line.end_ms == 2500
    assert list(line.inline_guide_symbols) == [1]
    assert (ruby.target_char_start, ruby.target_char_end) == (1, 2)


def test_normalize_combining_chars_absorbs_mark_line_end_semantics() -> None:
    line = TimingLine(
        chars=[
            TimingChar(text="は", start_ms=0),
            TimingChar(
                text="\u309A",
                start_ms=400,
                pause_release_ms=900,
                explicit_end=True,
            ),
        ],
        end_ms=1000,
    )
    track = TimingTrack(lines=[line])
    normalize_combining_chars(track)

    assert [c.text for c in line.chars] == ["ぱ"]
    assert line.chars[0].pause_release_ms == 900
    assert line.chars[0].explicit_end is True


def test_normalize_combining_chars_keeps_uncomposable_pair_in_one_cell() -> None:
    line = TimingLine(
        chars=[
            TimingChar(text="な", start_ms=0),
            TimingChar(text="\u3099", start_ms=100),
            TimingChar(text="に", start_ms=300),
        ],
        end_ms=500,
    )
    track = TimingTrack(lines=[line])
    normalize_combining_chars(track)

    assert [c.text for c in line.chars] == ["な\u3099", "に"]
    assert [c.start_ms for c in line.chars] == [0, 300]


def test_normalize_combining_chars_leaves_plain_lines_untouched() -> None:
    line = TimingLine(
        chars=[TimingChar(text="て", start_ms=0), TimingChar(text="こ", start_ms=100)],
        end_ms=200,
    )
    ruby = RubyAnnotation(
        kanji="て",
        reading="て",
        pos_start_ms=0,
        pos_end_ms=100,
        target_line_index=0,
        target_char_start=0,
        target_char_end=1,
    )
    track = TimingTrack(lines=[line], rubies=[ruby])
    normalize_combining_chars(track)

    assert [c.text for c in line.chars] == ["て", "こ"]
    assert (ruby.target_char_start, ruby.target_char_end) == (0, 1)


def test_normalize_combining_chars_shifts_shared_span_fields() -> None:
    line = TimingLine(
        chars=[
            TimingChar(
                text="テ",
                start_ms=0,
                source_span_start_ms=0,
                source_span_end_ms=900,
                source_span_index=0,
                source_span_count=3,
            ),
            TimingChar(
                text="\u3099",
                start_ms=300,
                source_span_start_ms=0,
                source_span_end_ms=900,
                source_span_index=1,
                source_span_count=3,
            ),
            TimingChar(
                text="こ",
                start_ms=600,
                source_span_start_ms=0,
                source_span_end_ms=900,
                source_span_index=2,
                source_span_count=3,
            ),
            TimingChar(text="ろ", start_ms=900),
        ],
        end_ms=1200,
    )
    track = TimingTrack(lines=[line])
    normalize_combining_chars(track)

    assert [c.text for c in line.chars] == ["デ", "こ", "ろ"]
    assert line.chars[0].source_span_count == 2
    assert line.chars[0].source_span_index == 0
    assert line.chars[1].source_span_count == 2
    assert line.chars[1].source_span_index == 1


def test_normalize_ruby_annotation_text_composes_and_keeps_part_boundaries() -> None:
    ruby = RubyAnnotation(
        kanji="テ\u3099",
        reading="て\u3099こ",
        reading_part_ms=[200],
        reading_parts=["て\u3099", "こ"],
    )
    normalize_ruby_annotation_text(ruby)
    assert ruby.kanji == "デ"
    assert ruby.reading_parts == ["で", "こ"]
    assert ruby.reading == "でこ"
    assert "".join(ruby.reading_parts) == ruby.reading
    assert len(ruby.reading_parts) == len(ruby.reading_part_ms) + 1

    crossing = RubyAnnotation(
        kanji="手",
        reading="て\u3099こ",
        reading_part_ms=[200],
        reading_parts=["て", "\u3099こ"],
    )
    normalize_ruby_annotation_text(crossing)
    # 时间戳边界把基字与浊点分在两个 part：跨边界不组合，拼接校验仍成立。
    assert crossing.reading_parts == ["て", "\u3099こ"]
    assert crossing.reading == "て\u3099こ"
    assert "".join(crossing.reading_parts) == crossing.reading
