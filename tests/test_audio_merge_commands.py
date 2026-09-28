"""音频合成命令构建测试（纯函数断言，不跑 ffmpeg）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from krok_helper.audio_processing.merge import commands as merge_commands
from krok_helper.audio_processing.merge.commands import (
    build_merge_command,
    unique_output_path,
)
from krok_helper.audio_processing.merge.model import MergeClip
from krok_helper.errors import ProcessingError


def _clip(
    name: str,
    *,
    duration: float = 10.0,
    sample_rate: int = 44100,
    channels: int = 2,
    trim_start: float = 0.0,
    trim_end: float | None = None,
) -> MergeClip:
    return MergeClip(
        path=Path(f"/fake/{name}.wav"),
        duration=duration,
        sample_rate=sample_rate,
        channels=channels,
        trim_start=trim_start,
        trim_end=trim_end,
    )


def _filter_graph(command: list[str]) -> str:
    return command[command.index("-filter_complex") + 1]


class TestBuildMergeCommand:
    def test_two_clips_baseline(self, tmp_path) -> None:
        clips = [
            _clip("a", trim_start=1.0, trim_end=9.0),
            _clip("b", trim_start=0.0),
        ]
        command = build_merge_command(
            ffmpeg_path="ffmpeg.exe",
            clips=clips,
            output_path=tmp_path / "out.wav",
            joint_fade_seconds=0.03,
        )
        # 输入：第一条 -ss 在 -i 之前；第二条不裁头就没有 -ss。
        first_i = command.index("-i")
        assert command[first_i - 2] == "-ss"
        assert command[first_i - 1] == "1.000"
        assert command[first_i + 1] == str(Path("/fake/a.wav"))
        second_i = command.index("-i", first_i + 1)
        assert command[second_i - 1] != "-ss"

        graph = _filter_graph(command)
        # 接缝淡化：首条只淡出不淡入、次条只淡入不淡出，曲线 qsin。
        first_chain, second_chain = graph.split(";")[0], graph.split(";")[1]
        assert "afade=t=in" not in first_chain
        assert "afade=t=out:st=7.970:d=0.030:curve=qsin" in first_chain
        assert "afade=t=in:st=0:d=0.030:curve=qsin" in second_chain
        assert "afade=t=out" not in second_chain
        # 裁剪时长进入 atrim（8.0 = 9.0 - 1.0），asetpts 归零。
        assert "atrim=end=8.000" in first_chain
        assert "asetpts=PTS-STARTPTS" in first_chain
        # concat 收尾。
        assert graph.endswith("concat=n=2:v=0:a=1[out]")
        # WAV 输出用 PCM。
        codec = command[command.index("-c:a") + 1]
        assert codec == "pcm_s24le"  # 24bit：质量不下滑

    def test_head_and_tail_fade_flags(self, tmp_path) -> None:
        command = build_merge_command(
            ffmpeg_path="ffmpeg.exe",
            clips=[_clip("solo")],
            output_path=tmp_path / "out.wav",
            joint_fade_seconds=0.03,
            head_fade=True,
            tail_fade=True,
        )
        graph = _filter_graph(command)
        assert "afade=t=in:st=0:d=0.030:curve=qsin" in graph
        assert "afade=t=out" in graph
        assert "concat=n=1:v=0:a=1[out]" in graph

    def test_gap_apads_all_but_last(self, tmp_path) -> None:
        clips = [_clip("a"), _clip("b"), _clip("c")]
        command = build_merge_command(
            ffmpeg_path="ffmpeg.exe",
            clips=clips,
            output_path=tmp_path / "out.wav",
            gap_seconds=0.5,
        )
        chains = _filter_graph(command).split(";")
        assert chains[0].endswith("apad=pad_dur=0.500[a0]")
        assert chains[1].endswith("apad=pad_dur=0.500[a1]")
        assert "apad" not in chains[2]

    def test_mixed_formats_normalize_to_max_rate_and_stereo(self, tmp_path) -> None:
        clips = [_clip("a", sample_rate=44100, channels=1), _clip("b", sample_rate=48000, channels=2)]
        command = build_merge_command(
            ffmpeg_path="ffmpeg.exe",
            clips=clips,
            output_path=tmp_path / "out.flac",
        )
        graph = _filter_graph(command)
        assert graph.count("aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo") == 2
        codec = command[command.index("-c:a") + 1]
        assert codec == "flac"

    def test_fade_clamped_to_half_duration(self, tmp_path) -> None:
        command = build_merge_command(
            ffmpeg_path="ffmpeg.exe",
            clips=[_clip("short", duration=0.06, trim_end=0.06), _clip("b")],
            output_path=tmp_path / "out.wav",
            joint_fade_seconds=0.05,
        )
        chains = _filter_graph(command).split(";")
        # 淡化 0.05s 超过片段一半（0.03s），按半时长钳制。
        assert "afade=t=out:st=0.030:d=0.030:curve=qsin" in chains[0]

    def test_invalid_trim_raises(self, tmp_path) -> None:
        with pytest.raises(ProcessingError):
            build_merge_command(
                ffmpeg_path="ffmpeg.exe",
                clips=[_clip("bad", trim_start=9.96, trim_end=None)],
                output_path=tmp_path / "out.wav",
            )

    def test_unanalyzed_clip_raises(self, tmp_path) -> None:
        with pytest.raises(ProcessingError):
            build_merge_command(
                ffmpeg_path="ffmpeg.exe",
                clips=[MergeClip(path=Path("/fake/none.wav"))],
                output_path=tmp_path / "out.wav",
            )

    def test_no_clips_raises(self, tmp_path) -> None:
        with pytest.raises(ProcessingError):
            build_merge_command(
                ffmpeg_path="ffmpeg.exe",
                clips=[],
                output_path=tmp_path / "out.wav",
            )

    def test_unsupported_extension_raises(self, tmp_path) -> None:
        with pytest.raises(ProcessingError):
            build_merge_command(
                ffmpeg_path="ffmpeg.exe",
                clips=[_clip("a")],
                output_path=tmp_path / "out.mp3",
            )


class TestCrossfadeSplice:
    def test_crossfade_chains_acrossfade(self, tmp_path) -> None:
        from krok_helper.audio_processing.merge.commands import SPLICE_CROSSFADE

        clips = [_clip("a", trim_start=1.0, trim_end=9.0), _clip("b")]
        command = build_merge_command(
            ffmpeg_path="ffmpeg.exe",
            clips=clips,
            output_path=tmp_path / "out.wav",
            joint_fade_seconds=0.5,
            splice_mode=SPLICE_CROSSFADE,
        )
        graph = _filter_graph(command)
        chains = graph.split(";")
        # 每条链不再有接缝淡化（交给 acrossfade），也没有 apad。
        assert all("afade=t=in" not in c for c in chains[:2])
        assert all("afade=t=out" not in c for c in chains[:2])
        assert "apad" not in graph
        assert chains[2] == "[a0][a1]acrossfade=d=0.500:c1=qsin:c2=qsin[out]"

    def test_crossfade_head_tail_fades_still_apply(self, tmp_path) -> None:
        from krok_helper.audio_processing.merge.commands import SPLICE_CROSSFADE

        command = build_merge_command(
            ffmpeg_path="ffmpeg.exe",
            clips=[_clip("a"), _clip("b"), _clip("c")],
            output_path=tmp_path / "out.wav",
            joint_fade_seconds=0.2,
            head_fade=True,
            tail_fade=True,
            splice_mode=SPLICE_CROSSFADE,
        )
        graph = _filter_graph(command)
        chains = graph.split(";")
        assert "afade=t=in:st=0:d=0.200:curve=qsin" in chains[0]
        assert "afade=t=out:st=9.800:d=0.200:curve=qsin" in chains[2]
        assert chains[3] == "[a0][a1]acrossfade=d=0.200:c1=qsin:c2=qsin[x1]"
        assert chains[4] == "[x1][a2]acrossfade=d=0.200:c1=qsin:c2=qsin[out]"

    def test_crossfade_overlap_clamped_to_half_durations(self, tmp_path) -> None:
        from krok_helper.audio_processing.merge.commands import SPLICE_CROSSFADE

        command = build_merge_command(
            ffmpeg_path="ffmpeg.exe",
            clips=[_clip("shorty", duration=0.4, trim_end=0.4), _clip("b")],
            output_path=tmp_path / "out.wav",
            joint_fade_seconds=1.0,
            splice_mode=SPLICE_CROSSFADE,
        )
        graph = _filter_graph(command)
        assert "acrossfade=d=0.200:" in graph  # min(1.0, 0.4/2, 10/2)

    def test_crossfade_single_clip_uses_concat(self, tmp_path) -> None:
        from krok_helper.audio_processing.merge.commands import SPLICE_CROSSFADE

        command = build_merge_command(
            ffmpeg_path="ffmpeg.exe",
            clips=[_clip("solo")],
            output_path=tmp_path / "out.wav",
            splice_mode=SPLICE_CROSSFADE,
        )
        assert "concat=n=1:v=0:a=1[out]" in _filter_graph(command)

    def test_merged_duration_matches_mode(self) -> None:
        from krok_helper.audio_processing.merge.commands import (
            SPLICE_BUTT,
            SPLICE_CROSSFADE,
            merged_duration,
        )

        clips = [_clip("a", trim_end=10.0), _clip("b", trim_end=10.0)]
        assert merged_duration(clips, joint_fade_seconds=0.5, gap_seconds=0.2) == pytest.approx(20.2)
        assert merged_duration(
            clips, joint_fade_seconds=0.5, gap_seconds=0.2, splice_mode=SPLICE_BUTT
        ) == pytest.approx(20.2)
        assert merged_duration(
            clips, joint_fade_seconds=0.5, gap_seconds=0.2, splice_mode=SPLICE_CROSSFADE
        ) == pytest.approx(19.5)
        assert merged_duration(
            clips, joint_fade_seconds=99.0, splice_mode=SPLICE_CROSSFADE
        ) == pytest.approx(15.0)  # 单接缝重叠钳到 10/2=5


class TestUniqueOutputPath:
    def test_dedup_appends_counter(self, tmp_path) -> None:
        first = unique_output_path(tmp_path, "合成.wav")
        first.write_bytes(b"x")
        second = unique_output_path(tmp_path, "合成.wav")
        assert second.name == "合成 (2).wav"
        second.write_bytes(b"x")
        third = unique_output_path(tmp_path, "合成.wav")
        assert third.name == "合成 (3).wav"


class TestRunMerge:
    def test_success_returns_path_and_validates_output(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(merge_commands, "find_tool", lambda _name, _dir=None: "ffmpeg.exe")
        commands_seen: list[list[str]] = []

        def _fake_run(command, logger, *, should_cancel=None, on_process_started=None):
            commands_seen.append(command)
            Path(command[-1]).write_bytes(b"pretend-audio")

        monkeypatch.setattr(merge_commands, "run_command", _fake_run)
        output = tmp_path / "out.wav"
        result = merge_commands.run_merge([_clip("a")], output, None, lambda _m: None)
        assert result == output
        assert output.read_bytes() == b"pretend-audio"
        assert len(commands_seen) == 1

    def test_failure_cleans_partial_output(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(merge_commands, "find_tool", lambda _name, _dir=None: "ffmpeg.exe")

        def _failing_run(command, logger, *, should_cancel=None, on_process_started=None):
            Path(command[-1]).write_bytes(b"partial")
            raise ProcessingError("ffmpeg 执行失败，退出码: 1")

        monkeypatch.setattr(merge_commands, "run_command", _failing_run)
        output = tmp_path / "out.wav"
        with pytest.raises(ProcessingError):
            merge_commands.run_merge([_clip("a")], output, None, lambda _m: None)
        assert not output.exists()
