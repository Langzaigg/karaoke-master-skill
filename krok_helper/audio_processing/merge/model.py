"""音频合成页的数据模型：一条待合成的音频剪辑及其裁剪状态。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from krok_helper.audio_alignment import WaveformData

#: 裁剪值来源：还没分析过 / 静音检测自动应用 / 用户手动改过。
TRIM_SOURCE_FULL = "full"
TRIM_SOURCE_AUTO = "auto"
TRIM_SOURCE_MANUAL = "manual"


@dataclass
class MergeClip:
    """一条待合成的音频剪辑。

    ``trim_start`` / ``trim_end`` 是当前生效的裁剪区间（秒）；``trim_end`` 为
    ``None`` 表示裁到文件末尾。``suggested_*`` 是最近一次静音检测给出的建议值，
    「一键处理静音段」用它覆盖当前值，用户也可以随时手动改回去。
    """

    path: Path
    duration: float = 0.0
    sample_rate: int = 0
    channels: int = 0
    waveform: WaveformData | None = None
    trim_start: float = 0.0
    trim_end: float | None = None
    suggested_start: float = 0.0
    suggested_end: float | None = None
    trim_source: str = TRIM_SOURCE_FULL
    #: 最近一次分析失败的错误文案；空串表示正常。
    error: str = ""

    def effective_end(self) -> float:
        return self.duration if self.trim_end is None else self.trim_end

    def suggested_effective_end(self) -> float:
        return self.duration if self.suggested_end is None else self.suggested_end

    def trimmed_duration(self) -> float:
        return max(0.0, self.effective_end() - self.trim_start)

    def analyzed(self) -> bool:
        return self.waveform is not None and self.duration > 0 and not self.error

    def apply_suggested(self, start: float, end: float | None) -> None:
        self.suggested_start = max(0.0, start)
        self.suggested_end = end
        self.trim_start = self.suggested_start
        self.trim_end = self.suggested_end
        self.trim_source = TRIM_SOURCE_AUTO

    def apply_manual(self, start: float, end: float | None) -> None:
        self.trim_start = max(0.0, start)
        self.trim_end = end
        self.trim_source = TRIM_SOURCE_MANUAL

    def reset_to_full(self) -> None:
        self.trim_start = 0.0
        self.trim_end = None
        self.trim_source = TRIM_SOURCE_FULL

    def status_text(self) -> str:
        if self.error:
            return "分析失败"
        if not self.analyzed():
            return "未分析"
        if self.trim_source == TRIM_SOURCE_MANUAL:
            return "手动裁剪"
        if self.trim_source == TRIM_SOURCE_AUTO:
            return "已去静音"
        return "未处理"


__all__ = [
    "MergeClip",
    "TRIM_SOURCE_AUTO",
    "TRIM_SOURCE_FULL",
    "TRIM_SOURCE_MANUAL",
]
