"""音频合成导出/预听的 FFmpeg 命令构建与执行（纯函数为主，便于测试）。

拼接策略是「短淡化直拼」：每条剪辑按裁剪区间取出后统一采样率/声道
（``aformat``，concat filter 的硬性要求，也是混合素材爆音的主因），接缝处
前一条结尾淡出、后一条开头淡入，``afade`` 显式用 ``curve=qsin``（零斜率
端点曲线，短淡化防爆音比默认线性更好）。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Callable

from krok_helper.audio_processing.merge.analysis import MIN_TRIM_SECONDS
from krok_helper.audio_processing.merge.model import MergeClip
from krok_helper.errors import ExportCancelled, ProcessingError
from krok_helper.ffmpeg import find_tool, run_command
from krok_helper.types import Logger

#: 接缝淡化默认时长。论坛共识 10–30ms 即可消除爆音且听感不可闻。
DEFAULT_JOINT_FADE_SECONDS = 0.03
SUPPORTED_OUTPUT_FORMATS = ("wav", "flac")
_FALLBACK_SAMPLE_RATE = 44100


def _seconds(value: float) -> str:
    return f"{max(0.0, value):.3f}"


def _resolve_format(output_path: Path) -> str:
    suffix = output_path.suffix.lower().lstrip(".")
    if suffix not in SUPPORTED_OUTPUT_FORMATS:
        raise ProcessingError(f"音频合成只支持输出 WAV 或 FLAC，无法输出: {output_path.name}")
    return suffix


def _clip_duration(clip: MergeClip) -> float:
    end = clip.duration if clip.trim_end is None else clip.trim_end
    duration = end - clip.trim_start
    if clip.duration <= 0:
        raise ProcessingError(f"素材还没分析完成，无法确定时长: {clip.path.name}")
    if clip.trim_start < -1e-6 or end > clip.duration + 0.05 or duration < MIN_TRIM_SECONDS:
        raise ProcessingError(
            f"{clip.path.name} 的裁剪区间无效（{clip.trim_start:.3f}s ~ {end:.3f}s，"
            f"素材时长 {clip.duration:.3f}s），请调整后再合成。"
        )
    return duration


#: 拼接方式：直拼（截断后拼接 + 接缝短淡化） / 交叉淡化（保留双轨，接缝重叠切歌）。
SPLICE_BUTT = "butt"
SPLICE_CROSSFADE = "crossfade"
SUPPORTED_SPLICE_MODES = (SPLICE_BUTT, SPLICE_CROSSFADE)


def _build_inputs_and_filter(
    clips: list[MergeClip],
    *,
    joint_fade_seconds: float,
    gap_seconds: float,
    head_fade: bool,
    tail_fade: bool,
    splice_mode: str = SPLICE_BUTT,
) -> tuple[list[str], str]:
    """构建输入参数与 filter_complex，供导出/预览共用。

    - ``butt``：每条截断取裁剪区间后首尾相接，接缝处前后短淡化防爆音，
      ``gap_seconds`` 为条目间的静音间隔；
    - ``crossfade``：每条仍按裁剪区间取音频，但相邻两条在接缝处重叠
      ``joint_fade_seconds``（前后两轨同时保留，前轨淡出、后轨淡入，
      DJ 切歌式过渡）；此时 ``gap_seconds`` 不参与，全局首/尾淡化仍生效。
    """
    input_args: list[str] = []
    chains: list[str] = []
    labels: list[str] = []

    sample_rate = max((clip.sample_rate or _FALLBACK_SAMPLE_RATE) for clip in clips)
    channel_layout = "stereo" if any(clip.channels >= 2 for clip in clips) else "mono"
    fade = max(0.0, joint_fade_seconds)
    durations = [_clip_duration(clip) for clip in clips]

    for index, clip in enumerate(clips):
        duration = durations[index]
        if clip.trim_start > 0.001:
            input_args.extend(["-ss", _seconds(clip.trim_start)])
        input_args.extend(["-i", str(clip.path)])

        parts = [
            f"[{index}:a:0]asetpts=PTS-STARTPTS",
            f"atrim=end={_seconds(duration)}",
            "asetpts=PTS-STARTPTS",
            (
                "aformat=sample_fmts=fltp"
                f":sample_rates={sample_rate}"
                f":channel_layouts={channel_layout}"
            ),
        ]
        if splice_mode == SPLICE_CROSSFADE:
            # 交叉淡化的接缝淡化交给 acrossfade，这里只保留全局首/尾淡化。
            fade_in = min(fade, duration / 2.0) if index == 0 and head_fade else 0.0
            fade_out = min(fade, duration / 2.0) if index == len(clips) - 1 and tail_fade else 0.0
        else:
            fade_in = fade if (index > 0 or head_fade) else 0.0
            fade_out = fade if (index < len(clips) - 1 or tail_fade) else 0.0
            fade_in = min(fade_in, duration / 2.0)
            fade_out = min(fade_out, duration / 2.0)
        if fade_in > 0:
            parts.append(f"afade=t=in:st=0:d={_seconds(fade_in)}:curve=qsin")
        if fade_out > 0:
            parts.append(
                f"afade=t=out:st={_seconds(duration - fade_out)}:d={_seconds(fade_out)}:curve=qsin"
            )
        if splice_mode == SPLICE_BUTT and gap_seconds > 0 and index < len(clips) - 1:
            parts.append(f"apad=pad_dur={_seconds(gap_seconds)}")

        label = f"[a{index}]"
        chains.append(",".join(parts) + label)
        labels.append(label)

    if splice_mode == SPLICE_CROSSFADE and len(clips) > 1:
        # 相邻两条链式 acrossfade；重叠时长按相邻两条时长各半钳制。
        current = labels[0]
        for index in range(1, len(clips)):
            overlap = min(
                fade,
                durations[index - 1] / 2.0,
                durations[index] / 2.0,
            )
            output_label = f"[x{index}]" if index < len(clips) - 1 else "[out]"
            if overlap > 0:
                chains.append(
                    f"{current}{labels[index]}acrossfade="
                    f"d={_seconds(overlap)}:c1=qsin:c2=qsin{output_label}"
                )
            else:
                # 退化（某条过短）：该接缝退化为直拼。
                chains.append(
                    f"{current}{labels[index]}concat=n=2:v=0:a=1{output_label}"
                )
            current = output_label
    else:
        chains.append(f"{''.join(labels)}concat=n={len(clips)}:v=0:a=1[out]")
    return input_args, ";".join(chains)


def merged_duration(
    clips: list[MergeClip],
    *,
    joint_fade_seconds: float,
    gap_seconds: float = 0.0,
    splice_mode: str = SPLICE_BUTT,
) -> float:
    """按拼接方式估算成品总时长（与时间轴视图共口径）。"""
    if not clips:
        return 0.0
    total = sum(_clip_duration(clip) for clip in clips)
    if splice_mode == SPLICE_CROSSFADE:
        durations = [_clip_duration(clip) for clip in clips]
        overlaps = 0.0
        for index in range(1, len(clips)):
            overlaps += min(
                joint_fade_seconds, durations[index - 1] / 2.0, durations[index] / 2.0
            )
        return max(0.0, total - overlaps)
    return total + gap_seconds * (len(clips) - 1)


def build_merge_command(
    *,
    ffmpeg_path: str,
    clips: list[MergeClip],
    output_path: Path,
    joint_fade_seconds: float = DEFAULT_JOINT_FADE_SECONDS,
    gap_seconds: float = 0.0,
    head_fade: bool = False,
    tail_fade: bool = False,
    splice_mode: str = SPLICE_BUTT,
) -> list[str]:
    """构建合成导出命令（纯函数，不碰文件系统）。"""
    if not clips:
        raise ProcessingError("还没有可合成的音频素材。")
    output_format = _resolve_format(output_path)
    input_args, filter_graph = _build_inputs_and_filter(
        clips,
        joint_fade_seconds=joint_fade_seconds,
        gap_seconds=gap_seconds,
        head_fade=head_fade,
        tail_fade=tail_fade,
        splice_mode=splice_mode,
    )
    command = [ffmpeg_path, "-hide_banner", "-v", "error", "-nostdin", "-y"]
    command.extend(input_args)
    command.extend(["-filter_complex", filter_graph, "-map", "[out]"])
    if output_format == "wav":
        # 24bit PCM：16bit 源无损、24bit 源不降位（fltp 中转实测逐样本无损）。
        command.extend(["-c:a", "pcm_s24le"])
    else:
        # FLAC 默认即 s32 容器/24bit 有效位深，实测对 24bit 源逐样本零差异。
        command.extend(["-c:a", "flac"])
    command.append(str(output_path))
    return command


def unique_output_path(root: Path, filename: str) -> Path:
    """输出路径去重：同名时追加 `` (2)``、`` (3)``……"""
    candidate = root / filename
    if not candidate.exists():
        return candidate
    stem, suffix = candidate.stem, candidate.suffix
    for index in range(2, 10000):
        candidate = root / f"{stem} ({index}){suffix}"
        if not candidate.exists():
            return candidate
    raise ProcessingError("输出目录中存在过多同名文件，请换个文件名。")


def run_merge(
    clips: list[MergeClip],
    output_path: Path,
    ffmpeg_dir: Path | None,
    logger: Logger,
    *,
    joint_fade_seconds: float = DEFAULT_JOINT_FADE_SECONDS,
    gap_seconds: float = 0.0,
    head_fade: bool = False,
    tail_fade: bool = False,
    splice_mode: str = SPLICE_BUTT,
    should_cancel: Callable[[], bool] | None = None,
    on_process_started: Callable[[subprocess.Popen | None], None] | None = None,
    command_builder=build_merge_command,
) -> Path:
    """执行合成导出，返回输出文件路径。失败/取消时清理半成品。"""
    ffmpeg_path = find_tool("ffmpeg.exe", ffmpeg_dir)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    mode_label = "交叉淡化切歌" if splice_mode == SPLICE_CROSSFADE else "直拼"
    estimated = merged_duration(
        clips,
        joint_fade_seconds=joint_fade_seconds,
        gap_seconds=gap_seconds,
        splice_mode=splice_mode,
    )
    logger(
        f"开始合成 {len(clips)} 条音频（{mode_label}），"
        f"接缝淡化 {_seconds(joint_fade_seconds)}s、间隔 {_seconds(gap_seconds)}s，"
        f"预计成品约 {estimated:.3f}s"
    )
    command = command_builder(
        ffmpeg_path=ffmpeg_path,
        clips=clips,
        output_path=output_path,
        joint_fade_seconds=joint_fade_seconds,
        gap_seconds=gap_seconds,
        head_fade=head_fade,
        tail_fade=tail_fade,
        splice_mode=splice_mode,
    )
    try:
        run_command(
            command,
            logger,
            should_cancel=should_cancel,
            on_process_started=on_process_started,
        )
    except ExportCancelled:
        _remove_incomplete_output(output_path, logger)
        raise
    except ProcessingError as exc:
        _remove_incomplete_output(output_path, logger)
        raise ProcessingError(f"音频合成失败: {output_path.name}\n{exc}") from exc

    if not output_path.is_file() or os.path.getsize(output_path) == 0:
        _remove_incomplete_output(output_path, logger)
        raise ProcessingError(f"合成失败，未生成有效文件: {output_path}")
    logger(f"音频合成完成: {output_path}")
    return output_path


def _remove_incomplete_output(output_path: Path, logger: Logger) -> None:
    if not output_path.exists():
        return
    try:
        output_path.unlink()
        logger(f"已清理未完成的输出文件: {output_path}")
    except OSError as exc:
        logger(f"清理未完成的输出文件失败: {output_path} ({exc})")


__all__ = [
    "DEFAULT_JOINT_FADE_SECONDS",
    "SPLICE_BUTT",
    "SPLICE_CROSSFADE",
    "SUPPORTED_OUTPUT_FORMATS",
    "SUPPORTED_SPLICE_MODES",
    "build_merge_command",
    "merged_duration",
    "run_merge",
    "unique_output_path",
]
