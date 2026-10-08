"""SRT 字幕格式导出器。

SRT 格式是通用字幕格式：
序号
HH:MM:SS,mmm --> HH:MM:SS,mmm
字幕文本
"""

from .base import BaseExporter, ExportError
from strange_uta_game.backend.domain import Project

# 无后续时间信息时的最小显示时长（毫秒），也用作结束时间的下限钳制
_MIN_DISPLAY_MS = 5000


class SRTExporter(BaseExporter):
    """SRT 字幕格式导出器

    导出标准 SRT 字幕格式。
    """

    @property
    def name(self) -> str:
        return "SRT"

    @property
    def description(self) -> str:
        return "通用字幕格式（SubRip）"

    @property
    def file_extension(self) -> str:
        return ".srt"

    @property
    def file_filter(self) -> str:
        return "SRT 字幕文件 (*.srt)"

    def export(self, project: Project, file_path: str) -> None:
        """导出为 SRT 格式"""
        self._validate_project(project)
        file_path = self._ensure_extension(file_path)

        blocks = []
        index = 1

        sentences = project.sentences
        for i, sentence in enumerate(sentences):
            if not sentence.text.strip():
                continue

            # 起始时间
            if sentence.has_timetags:
                start_ms = sentence.global_timing_start_ms or 0
            else:
                start_ms = 0

            # 结束时间：下一行的开始时间；末行用音频时长收尾（未知则退回
            # 当前行 + 5 秒）。仅当自然 End 不产生正时长（对唱行乱序，下一行
            # 先于本行开始）时才抬到最小显示时长——沿用下一行 Start 会导出
            # 05:00 --> 04:00 的负时长非法 SRT，但无条件下限会把正常短间隔
            # 歌词的 End 抬过下一行 Start，制造大面积重叠字幕。
            if i + 1 < len(sentences):
                next_sentence = sentences[i + 1]
                if next_sentence.has_timetags:
                    end_ms = (next_sentence.global_timing_start_ms or 0)
                else:
                    end_ms = start_ms + _MIN_DISPLAY_MS
            elif project.audio_duration_ms > start_ms:
                end_ms = project.audio_duration_ms
            else:
                end_ms = start_ms + _MIN_DISPLAY_MS
            if end_ms <= start_ms:
                end_ms = start_ms + _MIN_DISPLAY_MS

            start_str = self._format_srt_timestamp(start_ms)
            end_str = self._format_srt_timestamp(end_ms)

            block = f"{index}\n{start_str} --> {end_str}\n{sentence.text}"
            blocks.append(block)
            index += 1

        # 写入文件
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write("\n\n".join(blocks) + "\n")
        except Exception as e:
            raise ExportError(f"写入文件失败: {e}")

    def _format_srt_timestamp(self, timestamp_ms: int) -> str:
        """格式化 SRT 时间戳: HH:MM:SS,mmm

        调用方传入的 timestamp_ms 应已是全局时间戳
        （Sentence.global_timing_start_ms），本方法不再二次叠加偏移。
        """
        timestamp_ms = max(0, timestamp_ms)

        hours = timestamp_ms // 3600000
        remaining = timestamp_ms % 3600000
        minutes = remaining // 60000
        remaining = remaining % 60000
        seconds = remaining // 1000
        millis = remaining % 1000

        return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"
