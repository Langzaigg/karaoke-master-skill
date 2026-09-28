"""音频合成子包：多段音频拼接 + 首尾静音自动裁剪 + 接缝淡化。"""

from krok_helper.audio_processing.merge.page import MergeHost, MergePage

__all__ = ["MergeHost", "MergePage"]
