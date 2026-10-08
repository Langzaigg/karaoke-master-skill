"""窗口尺寸与多显示器定位辅助。

为什么需要这个模块
------------------
项目里大量窗口/对话框尺寸最初是在开发者的 2K 屏(且 100% 缩放)上硬编码的。
那种环境下逻辑像素(DIP)≈物理像素，``resize(1400, 900)`` 看着刚好。但 Qt6 的
``resize()`` / ``setMinimumSize()`` / ``availableGeometry()`` 全部是 **逻辑像素**，
在不同分辨率 / 缩放比例的屏幕上会出问题：

- 1920×1080 + 150% 缩放：可用逻辑区域只有约 1280×720，硬编码的 1400×900 直接溢出屏幕；
- ``setMinimumSize(1200, 800)`` 在这种屏幕上比可用高度还大，用户连缩小到屏幕内都做不到。

解决办法是不要硬编码绝对像素，而是按 **当前屏幕可用区域** 裁剪。由于期望尺寸和
``availableGeometry()`` 同为逻辑像素坐标系，二者直接比较即可，DPI 缩放被自动消化掉。

多显示器
--------
定位/居中一律基于「窗口所在屏」或「光标所在屏」，而不是永远 ``primaryScreen()``，
否则在副屏操作时窗口会跳回主屏。
"""

from __future__ import annotations

from PyQt6.QtCore import QRect
from PyQt6.QtGui import QCursor, QGuiApplication, QScreen
from PyQt6.QtWidgets import QWidget


def current_screen(widget: QWidget | None = None) -> QScreen:
    """返回最相关的屏幕。

    已显示的窗口用它自己所在的屏；尚未显示(如 ``__init__`` 阶段)时退回到光标
    所在屏，最后兜底主屏。这样启动期窗口会出现在用户正在操作的那块屏上。
    """
    if widget is not None and widget.isVisible():
        scr = widget.screen()
        if scr is not None:
            return scr
    return QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()


def available_rect(widget: QWidget | None = None) -> QRect:
    """当前屏幕去掉任务栏等保留区后的可用几何(逻辑像素)。"""
    return current_screen(widget).availableGeometry()


# 给窗口边框/标题栏 + 少量视觉留白预留的逻辑像素余量。
# 裁剪时用「可用区域 − 该余量」作为上限，而不是乘一个比例(如 0.9)：
# 这样只在窗口确实放不下时才收缩，否则保持设计尺寸——避免在「设计尺寸恰好
# 接近可用高度」的开发环境(如 2K@150%，可用高 ~928，主窗口高 900)上把窗口
# 平白缩小。
_FRAME_MARGIN = 24


def clamp_size(
    widget: QWidget, width: int, height: int, margin: int = _FRAME_MARGIN
) -> tuple[int, int]:
    """把期望尺寸裁剪到 ``可用区域 − margin`` 以内。

    返回裁剪后的 ``(width, height)``。窗口本就放得下时原样返回(裁剪是无操作)，
    因此对设计尺寸与大屏用户都无副作用；只有真正会溢出屏幕时才收缩。
    """
    avail = available_rect(widget)
    cw = min(width, max(320, avail.width() - margin))
    ch = min(height, max(240, avail.height() - margin))
    return cw, ch


def fit_to_screen(
    widget: QWidget,
    width: int,
    height: int,
    *,
    margin: int = _FRAME_MARGIN,
    center: bool = False,
) -> None:
    """裁剪到屏幕可用区域后 ``resize``，可选居中到所在屏。

    替代裸 ``widget.resize(w, h)``：在小屏 / 高缩放下不会溢出，放得下时保持原尺寸。
    """
    cw, ch = clamp_size(widget, width, height, margin)
    widget.resize(cw, ch)
    if center:
        center_on_screen(widget)


def fit_min_size(
    widget: QWidget, width: int, height: int, *, margin: int = _FRAME_MARGIN
) -> None:
    """设置最小尺寸，但绝不超过当前屏幕可用区域。

    替代裸 ``setMinimumSize(w, h)``：避免最小尺寸大于屏幕导致窗口无法缩进可视范围。
    """
    cw, ch = clamp_size(widget, width, height, margin)
    widget.setMinimumSize(cw, ch)


def center_on_screen(widget: QWidget) -> None:
    """把窗口居中到其所在(或光标所在)屏幕的可用区域。多显示器友好。"""
    avail = available_rect(widget)
    frame = widget.frameGeometry()
    frame.moveCenter(avail.center())
    widget.move(frame.topLeft())
