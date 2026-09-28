"""音频合成分析层测试：silencedetect 输出解析与首尾裁剪推导（纯函数，不依赖 ffmpeg）。"""

from __future__ import annotations

import pytest

from krok_helper.audio_processing.merge.analysis import (
    WAVEFORM_SAMPLE_RATE,
    build_analysis_command,
    derive_trim_bounds,
    parse_silencedetect_output,
)


class TestParseSilencedetectOutput:
    def test_typical_head_and_tail_lines(self) -> None:
        text = (
            "[silencedetect @ 0x1] silence_start: 0.016\n"
            "[silencedetect @ 0x1] silence_end: 1.234 | silence_duration: 1.218\n"
            "[silencedetect @ 0x1] silence_start: 8.5\n"
            "[silencedetect @ 0x1] silence_end: 10 | silence_duration: 1.5\n"
        )
        assert parse_silencedetect_output(text) == [(0.016, 1.234), (8.5, 10.0)]

    def test_trailing_silence_never_closes(self) -> None:
        text = "[silencedetect @ 0x1] silence_start: 7.5\n"
        assert parse_silencedetect_output(text) == [(7.5, None)]

    def test_negative_start_is_clamped_to_zero(self) -> None:
        text = "silence_start: -0.008\nsilence_end: 0.9\n"
        assert parse_silencedetect_output(text) == [(0.0, 0.9)]

    def test_orphan_end_is_kept_from_zero(self) -> None:
        assert parse_silencedetect_output("silence_end: 1.5\n") == [(0.0, 1.5)]

    def test_empty_and_noise_lines(self) -> None:
        assert parse_silencedetect_output("") == []
        assert parse_silencedetect_output("Input #0, wav, from 'a.wav':\nDuration: 00:00:10\n") == []


class TestDeriveTrimBounds:
    def test_head_silence_only(self) -> None:
        start, end = derive_trim_bounds([(0.0, 1.2)], 10.0, margin_seconds=0.05)
        assert start == pytest.approx(1.15)
        assert end is None

    def test_tail_silence_only(self) -> None:
        start, end = derive_trim_bounds([(8.5, 10.0)], 10.0, margin_seconds=0.05)
        assert start == 0.0
        assert end == pytest.approx(8.55)

    def test_head_and_tail(self) -> None:
        start, end = derive_trim_bounds([(0.0, 1.0), (8.0, 10.0)], 10.0, margin_seconds=0.05)
        assert start == pytest.approx(0.95)
        assert end == pytest.approx(8.05)

    def test_unclosed_tail_counts_as_tail_silence(self) -> None:
        _start, end = derive_trim_bounds([(0.0, 1.0), (8.0, None)], 10.0, margin_seconds=0.05)
        assert end == pytest.approx(8.05)

    def test_middle_silence_keeps_full_length(self) -> None:
        assert derive_trim_bounds([(4.0, 5.0)], 10.0, margin_seconds=0.05) == (0.0, None)

    def test_fully_silent_clip_keeps_full_length(self) -> None:
        assert derive_trim_bounds([(0.0, 10.0)], 10.0, margin_seconds=0.05) == (0.0, None)
        assert derive_trim_bounds([(0.0, None)], 10.0, margin_seconds=0.05) == (0.0, None)

    def test_short_head_margin_clamps_to_zero(self) -> None:
        # 头部静音只有 0.03s，回退 0.05s 余量后钳回 0（等于不裁）。
        start, _end = derive_trim_bounds([(0.0, 0.03)], 10.0, margin_seconds=0.05)
        assert start == 0.0

    def test_margin_cannot_push_end_past_duration(self) -> None:
        _start, end = derive_trim_bounds([(0.0, 1.0), (9.98, 10.0)], 10.0, margin_seconds=0.05)
        assert end == 10.0

    def test_empty_or_invalid_duration(self) -> None:
        assert derive_trim_bounds([], 10.0) == (0.0, None)
        assert derive_trim_bounds([(0.0, 1.0)], 0.0) == (0.0, None)

    def test_derived_range_too_short_falls_back_to_full(self) -> None:
        # 首尾静音裁完后只剩 0.02s 声音，不足最短保留时长，放弃裁剪。
        start, end = derive_trim_bounds([(0.0, 4.9), (4.92, 6.0)], 6.0, margin_seconds=0.0)
        assert (start, end) == (0.0, None)


class TestBuildAnalysisCommand:
    def test_command_shape(self, tmp_path) -> None:
        command = build_analysis_command(
            "ffmpeg.exe",
            tmp_path / "a.wav",
            threshold_db=-40.0,
            min_silence_seconds=0.1,
        )
        joined = " ".join(command)
        assert "silencedetect=noise=-40dB:d=0.1" in joined
        assert f"-ar {WAVEFORM_SAMPLE_RATE}" in joined
        assert "-f s16le" in joined
        assert command[-1] == "pipe:1"
        # silencedetect 走 info 级日志，命令不能带 -v error 把它压掉。
        assert "-v" not in command or command[command.index("-v") + 1] != "error"
