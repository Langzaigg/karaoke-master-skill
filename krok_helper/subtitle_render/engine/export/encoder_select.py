"""Video encoder selection for subtitle MP4 export."""

from __future__ import annotations

import subprocess
from functools import lru_cache
from typing import Literal

EncoderMode = Literal["cpu", "auto", "nvenc", "qsv", "amf_qvbr", "amf_cqp"]
VideoCodec = Literal["h264", "hevc"]

ENCODER_CPU = "cpu"
ENCODER_AUTO = "auto"
ENCODER_NVENC = "nvenc"
ENCODER_QSV = "qsv"
ENCODER_AMF_QVBR = "amf_qvbr"
ENCODER_AMF_CQP = "amf_cqp"
# 4.3.x 及更早版本保存的旧值。QVBR 仅在用户显式选择时生效（含 legacy
# 归一与 auto 解析），因此该旧值归一到 CQP。
ENCODER_AMF = "amf"
ENCODER_MODES: set[str] = {
    ENCODER_CPU,
    ENCODER_AUTO,
    ENCODER_NVENC,
    ENCODER_QSV,
    ENCODER_AMF_QVBR,
    ENCODER_AMF_CQP,
}

CODEC_H264 = "h264"
CODEC_HEVC = "hevc"
VIDEO_CODECS: set[str] = {CODEC_H264, CODEC_HEVC}

# (codec, mode) → ffmpeg 编码器名。auto 解析成具体硬编模式后再查表。
_CODEC_ENCODER_NAMES: dict[str, dict[str, str]] = {
    CODEC_H264: {
        ENCODER_CPU: "libx264",
        ENCODER_NVENC: "h264_nvenc",
        ENCODER_QSV: "h264_qsv",
        ENCODER_AMF_QVBR: "h264_amf",
        ENCODER_AMF_CQP: "h264_amf",
    },
    CODEC_HEVC: {
        ENCODER_CPU: "libx265",
        ENCODER_NVENC: "hevc_nvenc",
        ENCODER_QSV: "hevc_qsv",
        ENCODER_AMF_QVBR: "hevc_amf",
        ENCODER_AMF_CQP: "hevc_amf",
    },
}

CPU_PRESETS: tuple[str, ...] = (
    "ultrafast",
    "superfast",
    "veryfast",
    "faster",
    "fast",
    "medium",
    "slow",
    "slower",
    "veryslow",
)


def normalize_encoder_mode(mode: str) -> str:
    """Return a supported encoder mode, migrating legacy values, falling back to CPU."""
    if mode == ENCODER_AMF:
        return ENCODER_AMF_CQP
    return mode if mode in ENCODER_MODES else ENCODER_CPU


def normalize_video_codec(codec: str) -> str:
    """Return a supported video codec, falling back to H.264."""
    return codec if codec in VIDEO_CODECS else CODEC_H264


def normalize_cpu_preset(preset: str) -> str:
    """Return a supported x264/x265 preset, falling back to ``medium``."""
    return preset if preset in CPU_PRESETS else "medium"


def amf_qvbr_quality_level(crf: int) -> int:
    """Map UI quality to the inverse AMF QVBR scale (UI 18 -> QVBR 26)."""
    normalized_crf = max(0, min(51, int(crf)))
    if normalized_crf <= 18:
        # Preserve the highest-quality endpoint while anchoring the app's
        # visually-near-lossless default in AMF's practical QVBR range.
        return round(51 - normalized_crf * 25 / 18)
    return round(26 - (normalized_crf - 18) * 25 / 33)


def video_encoder_options(
    ffmpeg_path: str,
    mode: str,
    *,
    crf: int,
    preset: str,
    codec: str = CODEC_H264,
) -> list[str]:
    """Build ffmpeg video encoder options for the selected mode/codec."""
    selected = normalize_encoder_mode(mode)
    codec = normalize_video_codec(codec)
    if selected == ENCODER_AUTO:
        selected = _best_available_hardware_encoder(ffmpeg_path, codec) or ENCODER_CPU

    names = _CODEC_ENCODER_NAMES[codec]
    crf = max(0, min(51, int(crf)))
    # MP4 里 HEVC 打 hvc1 tag（默认 hev1 在 Apple 系播放器上不识别）
    hevc_tag = ["-tag:v", "hvc1"] if codec == CODEC_HEVC else []
    if selected == ENCODER_NVENC:
        return ["-c:v", names[ENCODER_NVENC], "-preset", "p4", "-cq", str(crf), *hevc_tag]
    if selected == ENCODER_QSV:
        # QSV ICQ accepts 1-51. A zero value disables ICQ selection in FFmpeg,
        # so preserve the shared UI range while translating only this endpoint.
        return ["-c:v", names[ENCODER_QSV], "-global_quality", str(max(1, crf)), *hevc_tag]
    if selected == ENCODER_AMF_CQP:
        # CQP 与 N3 的 AMD/AMF 导出同构：质量值直通为固定量化参数，
        # 码率是编码结果而非输入，不传 -b:v/-maxrate。hevc_amf 无 B 帧，
        # 没有 -qp_b 选项。
        options = [
            "-c:v",
            names[ENCODER_AMF_CQP],
            "-quality",
            "balanced",
            "-rc",
            "cqp",
            "-qp_i",
            str(crf),
            "-qp_p",
            str(crf),
        ]
        if codec == CODEC_H264:
            options.extend(["-qp_b", str(crf)])
        return [*options, *hevc_tag]
    if selected == ENCODER_AMF_QVBR:
        return [
            "-c:v",
            names[ENCODER_AMF_QVBR],
            "-quality",
            "balanced",
            "-rc",
            "qvbr",
            "-qvbr_quality_level",
            str(amf_qvbr_quality_level(crf)),
            *hevc_tag,
        ]

    return [
        "-c:v",
        names[ENCODER_CPU],
        "-preset",
        normalize_cpu_preset(preset),
        "-crf",
        str(crf),
        *hevc_tag,
    ]


def resolved_encoder_label(ffmpeg_path: str, mode: str, codec: str = CODEC_H264) -> str:
    """Human-readable encoder label for status logs."""
    selected = normalize_encoder_mode(mode)
    codec = normalize_video_codec(codec)
    if selected == ENCODER_AUTO:
        selected = _best_available_hardware_encoder(ffmpeg_path, codec) or ENCODER_CPU
    names = _CODEC_ENCODER_NAMES[codec]
    encoder_name = names.get(selected, names[ENCODER_CPU])
    base = {
        ENCODER_NVENC: "NVIDIA NVENC",
        ENCODER_QSV: "Intel QSV",
        ENCODER_AMF_QVBR: "AMD AMF(QVBR)",
        ENCODER_AMF_CQP: "AMD AMF(CQP)",
    }.get(selected)
    if base is None:
        return f"CPU({encoder_name})"
    return f"{base}({encoder_name})"


def _best_available_hardware_encoder(ffmpeg_path: str, codec: str = CODEC_H264) -> str | None:
    encoders = _available_encoders(ffmpeg_path)
    names = _CODEC_ENCODER_NAMES[normalize_video_codec(codec)]
    # auto 模式解析到 A 卡时走 CQP；QVBR 必须用户显式选择。
    for mode in (ENCODER_NVENC, ENCODER_QSV, ENCODER_AMF_CQP):
        if names[mode] in encoders:
            return mode
    return None


@lru_cache(maxsize=8)
def _available_encoders(ffmpeg_path: str) -> frozenset[str]:
    try:
        completed = subprocess.run(
            [ffmpeg_path, "-hide_banner", "-encoders"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
    except Exception:
        return frozenset()
    return frozenset(
        token
        for line in completed.stdout.splitlines()
        for token in line.split()
        if token.startswith(("h264_", "hevc_")) or token in ("libx264", "libx265")
    )
