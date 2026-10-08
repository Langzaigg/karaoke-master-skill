"""BASS 会话级 sample 失效登记表。

BASS 是进程全局单例：任一 BASS 引擎恢复设备时 ``BASS_Free()`` 会回收进程内
全部 sample 句柄——不止该引擎自己的流，还包括 KeySoundPlayer / MetronomePlayer
持有的按键音/节拍音样本。为让这一失效自闭合（不依赖 UI 层接线，无 Qt 依赖），
播放器实例在此登记（弱引用，不阻碍释放），引擎恢复流程结束后（无论成败——
BASS_Free 一经执行句柄即已失效）调用 invalidate-all，各实例归零句柄并在
下一次播放时对新 BASS 会话惰性重载样本。
"""

from __future__ import annotations

import threading
import weakref

_lock = threading.Lock()
_owners: weakref.WeakSet = weakref.WeakSet()


def register_bass_sample_owner(owner) -> None:
    """登记持有 BASS sample 句柄的播放器实例（弱引用，不阻碍实例释放）。"""
    with _lock:
        _owners.add(owner)


def invalidate_all_bass_samples() -> None:
    """BASS_Free 后调用：登记表内全部实例的 sample 句柄失效。

    无论恢复成败都必须调用——BASS_Free 执行过，进程内旧句柄即已全部失效，
    继续持有只可能误操作新会话中复用了同一句柄值的资源。单个实例失效失败
    不拖垮整体（各播放器的播放路径本身也全兜底）。
    """
    with _lock:
        owners = list(_owners)
    for owner in owners:
        try:
            owner.invalidate()
        except Exception:
            pass
