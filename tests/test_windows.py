"""``krok_helper.windows.open_in_explorer`` 的跨平台行为。

回归背景（PR #11 引入）：函数内 ``mkdir(exist_ok=True)`` 对已存在的
**文件**路径会抛 ``FileExistsError``，而音频合成页「打开输出目录」传入的
正是合成输出的文件路径——旧实现导致按钮点击后静默无响应。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from krok_helper import windows


def _install_fakes(monkeypatch):
    """屏蔽真实弹窗：记录 Popen 与 QDesktopServices.openUrl 的调用。"""

    popen_calls: list = []
    opened: list[Path] = []

    def fake_popen(*args, **kwargs):
        popen_calls.append(args)

    def fake_open_url(url):
        opened.append(Path(url.toLocalFile()))

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr("PyQt6.QtGui.QDesktopServices.openUrl", fake_open_url)
    return popen_calls, opened


def test_open_in_explorer_creates_missing_directory(monkeypatch, tmp_path):
    popen_calls, opened = _install_fakes(monkeypatch)
    target = tmp_path / "output"

    windows.open_in_explorer(target)

    assert target.is_dir()
    assert opened == [target]
    assert popen_calls == []


def test_open_in_explorer_opens_existing_directory(monkeypatch, tmp_path):
    popen_calls, opened = _install_fakes(monkeypatch)
    target = tmp_path / "output"
    target.mkdir()

    windows.open_in_explorer(target)

    assert opened == [target]
    assert popen_calls == []


def test_open_in_explorer_selects_existing_file_on_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(windows.sys, "platform", "win32")
    popen_calls, opened = _install_fakes(monkeypatch)
    target = tmp_path / "merged.wav"
    target.write_bytes(b"x")

    # 回归点：文件路径不得按目录 mkdir 抛 FileExistsError。
    windows.open_in_explorer(target)

    assert popen_calls and popen_calls[0][0][:2] == ["explorer", "/select,"]
    assert opened == []


def test_open_in_explorer_opens_parent_for_file_on_macos(monkeypatch, tmp_path):
    monkeypatch.setattr(windows.sys, "platform", "darwin")
    popen_calls, opened = _install_fakes(monkeypatch)
    target = tmp_path / "merged.wav"
    target.write_bytes(b"x")

    windows.open_in_explorer(target)

    assert opened == [target.parent]
    assert popen_calls == []
