from __future__ import annotations

import ctypes
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path


if sys.platform == "win32":
    user32 = ctypes.windll.user32
    shell32 = ctypes.windll.shell32
    shcore = getattr(ctypes.windll, "shcore", None)
    user32.GetDpiForSystem.restype = wintypes.UINT


def enable_high_dpi_awareness() -> None:
    if sys.platform != "win32":
        return

    try:
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return
    except Exception:
        pass

    if shcore is not None:
        try:
            shcore.SetProcessDpiAwareness(2)
            return
        except Exception:
            pass

    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass


def set_explicit_app_user_model_id(app_id: str) -> None:
    if sys.platform != "win32":
        return

    try:
        shell32.SetCurrentProcessExplicitAppUserModelID(str(app_id))
    except Exception:
        pass


def hidden_subprocess_kwargs() -> dict[str, object]:
    """Return Windows process flags that prevent console-window flashes.

    ``CREATE_NO_WINDOW`` is the primary guard for console executables.  The
    hidden ``STARTUPINFO`` is retained as defense in depth for launchers that
    do not fully honor that creation flag when called from a frozen GUI app.
    """

    if sys.platform != "win32":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
    return {
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
        "startupinfo": startupinfo,
    }


def open_in_explorer(path) -> None:
    """在系统文件管理器中打开目录，目录不存在则先创建。

    传入已存在的文件时定位其所在目录：Windows 用资源管理器直接选中该文件
    （与 subtitle_render._open_export_folder 同一手法），其余平台打开所在
    目录。合成页「打开输出目录」传的就是输出文件路径，不能按目录建。
    """
    from PyQt6.QtCore import QUrl
    from PyQt6.QtGui import QDesktopServices

    target = Path(path).resolve()
    if target.exists() and not target.is_dir():
        if sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", str(target)])
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(target.parent)))
        return
    target.mkdir(parents=True, exist_ok=True)
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))
