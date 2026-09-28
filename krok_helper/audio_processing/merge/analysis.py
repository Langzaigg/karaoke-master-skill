"""音频合成的素材分析：单遍 FFmpeg 解码 + ``silencedetect`` 静音检测。

一遍 ffmpeg 同时产出两样东西：

- stdout：8kHz 单声道 s16le PCM，用于波形峰值（口径与波形对齐页的
  :func:`krok_helper.audio_alignment.extract_waveform` 一致）；
- stderr：``silencedetect`` 打印的 ``silence_start`` / ``silence_end`` 行，
  用于推导首尾静音裁剪点（社区标准做法，见 Stack Overflow #25697596）。

检测与裁剪的推导全部拆成纯函数（:func:`parse_silencedetect_output` /
:func:`derive_trim_bounds`），不碰 ffmpeg 就能测试。
"""

from __future__ import annotations

import re
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from krok_helper.audio_alignment import (
    WAVEFORM_PEAKS_PER_SECOND,
    WAVEFORM_SAMPLE_RATE,
    WaveformData,
    _build_peaks,
    _samples_from_pcm,
)
from krok_helper.errors import ExportCancelled, ProcessingError
from krok_helper.ffmpeg import (
    _build_subprocess_kwargs,
    find_tool,
    probe_media,
    terminate_process,
)
from krok_helper.types import Logger

#: silencedetect 噪声阈值（dBFS）。FFmpeg 默认 -60dB 过严，社区实用区间
#: -35 ~ -50dB，这里取通用默认 -40dB；UI 可调 -60 ~ -20。
DEFAULT_SILENCE_THRESHOLD_DB = -40.0
#: 少于该时长的静音不上报（避免把极短间隙当静音）。
DEFAULT_MIN_SILENCE_SECONDS = 0.1
#: 裁剪点向声音一侧回退的保留余量，避免切掉起振/收尾瞬态。
DEFAULT_MARGIN_SECONDS = 0.05
#: 裁剪后片段至少要保留的时长（与命令构建共用同一口径）。
MIN_TRIM_SECONDS = 0.05
#: probe 拿不到采样率/声道时的兜底值。
_FALLBACK_SAMPLE_RATE = 44100

_SILENCE_START_RE = re.compile(r"silence_start:\s*(-?\d+(?:\.\d+)?)")
_SILENCE_END_RE = re.compile(r"silence_end:\s*(\d+(?:\.\d+)?)")
#: 静音段起止离文件头/尾多近算「贴边」（首部/尾部静音）。
_EDGE_TOLERANCE_SECONDS = 0.05


@dataclass
class ClipAnalysis:
    """一条素材的分析结果：波形 + 探测信息 + 建议裁剪区间。"""

    waveform: WaveformData
    sample_rate: int
    channels: int
    trim_start: float
    trim_end: float | None


def build_analysis_command(
    ffmpeg_path: str,
    media_path: Path,
    *,
    threshold_db: float = DEFAULT_SILENCE_THRESHOLD_DB,
    min_silence_seconds: float = DEFAULT_MIN_SILENCE_SECONDS,
) -> list[str]:
    """一遍解码命令：stdout 出 PCM、stderr 出 silencedetect 行。

    注意不能加 ``-v error`` —— silencedetect 的输出走 info 级别。
    """
    return [
        ffmpeg_path,
        "-hide_banner",
        "-nostdin",
        "-i",
        str(media_path),
        "-map",
        "0:a:0",
        "-vn",
        "-af",
        f"silencedetect=noise={threshold_db:g}dB:d={min_silence_seconds:g}",
        "-ac",
        "1",
        "-ar",
        str(WAVEFORM_SAMPLE_RATE),
        "-f",
        "s16le",
        "pipe:1",
    ]


def parse_silencedetect_output(text: str) -> list[tuple[float, float | None]]:
    """解析 silencedetect 的 stderr，返回静音区间列表 ``(start, end)``。

    ``end`` 为 ``None`` 表示该段静音延伸到文件末尾（EOF 未闭合）。容错规则：

    - 一行里 start/end 可能同时出现（新版 ffmpeg 会合并打印 duration）；
    - 只有 end 没有 start 的孤儿段按 0 起点收下；
    - 起点为负（时间戳回退）钳到 0。
    """
    spans: list[tuple[float, float | None]] = []
    current_start: float | None = None

    def _close(start: float | None, end: float | None) -> None:
        spans.append((max(0.0, start if start is not None else 0.0), end))

    for line in text.splitlines():
        start_match = _SILENCE_START_RE.search(line)
        end_match = _SILENCE_END_RE.search(line)
        if start_match:
            if current_start is not None:
                _close(current_start, None)
            current_start = float(start_match.group(1))
        if end_match:
            end = float(end_match.group(1))
            _close(current_start, end)
            current_start = None
    if current_start is not None:
        _close(current_start, None)
    return spans


def derive_trim_bounds(
    silences: list[tuple[float, float | None]],
    duration: float,
    *,
    margin_seconds: float = DEFAULT_MARGIN_SECONDS,
) -> tuple[float, float | None]:
    """从静音区间推导首尾裁剪点，返回 ``(trim_start, trim_end)``。

    - 首部静音：第一段静音贴着文件头时，裁到该段结束（首个声音起点）再回退
      ``margin_seconds``；
    - 尾部静音：最后一段静音延伸到文件末尾（end 为 None 或贴着 duration）时，
      裁到该段开始再前推 ``margin_seconds``；
    - 整条都没检出有效声音（唯一一段静音覆盖全文件）→ 保留全长，由调用方提示；
    - 推导出的区间不足 :data:`MIN_TRIM_SECONDS` → 放弃裁剪，保留全长。
    """
    if duration <= 0 or not silences:
        return 0.0, None

    first_start, first_end = silences[0]
    last_start, last_end = silences[-1]
    covers_head = first_start <= _EDGE_TOLERANCE_SECONDS
    covers_tail = last_end is None or last_end >= duration - _EDGE_TOLERANCE_SECONDS

    if len(silences) == 1 and covers_head and covers_tail:
        return 0.0, None  # 全静音（或从头静到尾），没有有效声音可保

    start = 0.0
    if covers_head and first_end is not None:
        start = max(0.0, first_end - margin_seconds)

    end: float | None = None
    if covers_tail:
        end = min(duration, max(start, last_start + margin_seconds))

    if end is not None and end - start < MIN_TRIM_SECONDS:
        return 0.0, None
    if start >= duration - MIN_TRIM_SECONDS:
        return 0.0, None
    return start, end


def analyze_clip(
    media_path: Path,
    ffmpeg_dir: Path | None,
    logger: Logger,
    *,
    label: str,
    threshold_db: float = DEFAULT_SILENCE_THRESHOLD_DB,
    min_silence_seconds: float = DEFAULT_MIN_SILENCE_SECONDS,
    margin_seconds: float = DEFAULT_MARGIN_SECONDS,
    should_cancel: Callable[[], bool] | None = None,
) -> ClipAnalysis:
    """分析一条音频：波形峰值 + 采样率/声道 + 首尾静音建议裁剪点。

    取消时抛 :class:`ExportCancelled`，解码失败抛 :class:`ProcessingError`
    （中文文案）。
    """
    ffmpeg_path = find_tool("ffmpeg.exe", ffmpeg_dir)
    ffprobe_path = find_tool("ffprobe.exe", ffmpeg_dir)
    info = probe_media(ffprobe_path, media_path)
    if info.audio_streams == 0:
        raise ProcessingError(f"{label} 里没有检测到音频流。")

    logger(f"正在分析 {label}: {media_path.name}")
    process = subprocess.Popen(
        build_analysis_command(
            ffmpeg_path,
            media_path,
            threshold_db=threshold_db,
            min_silence_seconds=min_silence_seconds,
        ),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **_build_subprocess_kwargs(),
    )
    assert process.stdout is not None and process.stderr is not None

    # stderr 单独开线程收：silencedetect 行数多时先把 stderr 管道写满，
    # 会反过来卡住 ffmpeg 写 stdout，主线程读 stdout 就死锁了。
    stderr_chunks: list[bytes] = []

    def _drain_stderr() -> None:
        for line in iter(process.stderr.readline, b""):  # type: ignore[union-attr]
            stderr_chunks.append(line)

    drain_thread = threading.Thread(target=_drain_stderr, daemon=True)
    drain_thread.start()

    pcm_chunks: list[bytes] = []
    try:
        while True:
            chunk = process.stdout.read(65536)
            if not chunk:
                break
            pcm_chunks.append(chunk)
            if should_cancel is not None and should_cancel():
                terminate_process(process, timeout=1.0)
                raise ExportCancelled("已停止分析。")
        return_code = process.wait()
    finally:
        drain_thread.join(timeout=2.0)
        for stream in (process.stdout, process.stderr):
            try:
                stream.close()
            except OSError:
                pass

    if return_code != 0:
        stderr_text = b"".join(stderr_chunks).decode("utf-8", errors="replace").strip()
        raise ProcessingError(f"{label} 分析失败: {media_path.name}\n{stderr_text}")

    samples = _samples_from_pcm(b"".join(pcm_chunks))
    if not samples:
        raise ProcessingError(f"{label} 没有可用于绘制波形的音频采样。")

    duration = info.duration or (len(samples) / WAVEFORM_SAMPLE_RATE)
    window_size = max(1, WAVEFORM_SAMPLE_RATE // WAVEFORM_PEAKS_PER_SECOND)
    peaks = _build_peaks(samples, window_size)
    waveform = WaveformData(
        path=media_path,
        duration=duration,
        peaks_per_second=WAVEFORM_PEAKS_PER_SECOND,
        peaks=peaks,
    )

    stderr_text = b"".join(stderr_chunks).decode("utf-8", errors="replace")
    silences = parse_silencedetect_output(stderr_text)
    bounds = derive_trim_bounds(silences, duration, margin_seconds=margin_seconds)
    bounds_end = duration if bounds[1] is None else bounds[1]
    logger(
        f"{label} 分析完成: 时长 {duration:.3f}s，静音 {len(silences)} 处，"
        f"建议裁剪 {bounds[0]:.3f}s ~ {bounds_end:.3f}s"
    )
    return ClipAnalysis(
        waveform=waveform,
        sample_rate=info.sample_rate or _FALLBACK_SAMPLE_RATE,
        channels=info.channels or 2,
        trim_start=bounds[0],
        trim_end=bounds[1],
    )


__all__ = [
    "DEFAULT_MARGIN_SECONDS",
    "DEFAULT_MIN_SILENCE_SECONDS",
    "DEFAULT_SILENCE_THRESHOLD_DB",
    "MIN_TRIM_SECONDS",
    "ClipAnalysis",
    "analyze_clip",
    "build_analysis_command",
    "derive_trim_bounds",
    "parse_silencedetect_output",
]
