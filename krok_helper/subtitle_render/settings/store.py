"""Persistence adapter for the subtitle-render settings namespace."""

from __future__ import annotations

from krok_helper.settings import (
    load_app_settings,
    write_app_settings_namespace,
)
from krok_helper.subtitle_render.contracts import SubtitleRenderSettingsProvider


class SubtitleRenderSettingsStore:
    """Read and replace the module namespace through one stable boundary."""

    def __init__(
        self,
        provider: SubtitleRenderSettingsProvider | None = None,
    ) -> None:
        self._provider = provider

    def load(self) -> dict:
        if self._provider is not None and hasattr(self._provider, "load"):
            loaded = self._provider.load()
        else:
            loaded = load_app_settings().subtitle_render
        return dict(loaded) if isinstance(loaded, dict) else {}

    def save(self, data: dict) -> None:
        if self._provider is not None and hasattr(self._provider, "save"):
            self._provider.save(data)
            return
        # 命名空间级写：不再整读整写（旧链路 save 内部还要再 load 一次
        # AppSettings，是停手防抖后 GUI 卡顿的主体之一）。语义与旧的
        # 「load → 只改本命名空间 → save(merge_module_namespaces=False)」
        # 等价——那次 load 的基线就是盘上内容，合并后其余字段全部原样。
        write_app_settings_namespace("subtitle_render", data)
