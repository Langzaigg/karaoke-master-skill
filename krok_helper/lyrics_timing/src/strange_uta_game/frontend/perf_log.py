from __future__ import annotations

import functools
import os
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from strange_uta_game import app_dirs


_ENABLED = os.getenv("SUG_TIMING_PERF_LOG", "").lower() not in ("", "0", "false", "no", "off")
_DEFAULT_THRESHOLD_MS = float(os.getenv("SUG_TIMING_PERF_THRESHOLD_MS", "16"))


def _default_log_path() -> Path:
    """诊断日志默认路径。

    macOS 程序目录在只读 bundle 内，改用 ``~/Library/Logs``；其余平台沿用程序目录
    下的 ``logs``。目录在首次写入时（已被 ``_ENABLED`` 门控且 try/except 包裹）惰性
    创建，故此处只解析路径、不触碰文件系统。
    """
    try:
        if sys.platform == "darwin":
            base_dir = Path.home() / "Library" / "Logs" / app_dirs.APP_NAME
        else:
            base_dir = app_dirs.program_dir() / "logs"
    except Exception:
        base_dir = Path.cwd() / "logs"
    return base_dir / "sug-timing-perf.log"


_LOG_PATH = Path(
    os.getenv(
        "SUG_TIMING_PERF_LOG_PATH",
        str(_default_log_path()),
    )
)
_WATCHDOG_STARTED = False
_WATCHDOG_LOCK = threading.Lock()
_LAST_HEARTBEAT_S = time.perf_counter()
_STALL_ACTIVE = False


def perf_enabled() -> bool:
    return _ENABLED


def perf_log_path() -> Path:
    return _LOG_PATH


def _format_fields(fields: dict[str, Any]) -> str:
    parts = []
    for key, value in fields.items():
        text = str(value).replace("\n", "\\n").replace("\r", "\\r")
        parts.append(f"{key}={text}")
    return " ".join(parts)


def log_perf_event(event: str, **fields: Any) -> None:
    if not _ENABLED:
        return
    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        line = f"{stamp} event={event}"
        extra = _format_fields(fields)
        if extra:
            line += " " + extra
        with _LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def start_ui_watchdog(parent, interval_ms: int = 16) -> None:
    if not _ENABLED:
        return
    global _WATCHDOG_STARTED
    with _WATCHDOG_LOCK:
        if _WATCHDOG_STARTED:
            return
        _WATCHDOG_STARTED = True

    from PyQt6.QtCore import QTimer

    threshold_ms = float(os.getenv("SUG_TIMING_STALL_MS", "120"))
    main_thread_id = threading.main_thread().ident

    def heartbeat() -> None:
        global _LAST_HEARTBEAT_S, _STALL_ACTIVE
        _LAST_HEARTBEAT_S = time.perf_counter()
        if _STALL_ACTIVE:
            log_perf_event("ui.stall.end")
            _STALL_ACTIVE = False

    timer = QTimer(parent)
    timer.setInterval(interval_ms)
    timer.timeout.connect(heartbeat)
    timer.start()
    parent._sug_perf_watchdog_timer = timer
    heartbeat()

    def watchdog_loop() -> None:
        global _STALL_ACTIVE
        while True:
            time.sleep(max(0.025, threshold_ms / 3000.0))
            age_ms = (time.perf_counter() - _LAST_HEARTBEAT_S) * 1000.0
            if age_ms < threshold_ms or _STALL_ACTIVE:
                continue
            _STALL_ACTIVE = True
            stack_text = ""
            frame = sys._current_frames().get(main_thread_id)
            if frame is not None:
                stack_text = "".join(traceback.format_stack(frame, limit=18))
            log_perf_event(
                "ui.stall.start",
                gap_ms=f"{age_ms:.1f}",
                stack=stack_text,
            )

    thread = threading.Thread(
        target=watchdog_loop,
        name="SUGTimingPerfWatchdog",
        daemon=True,
    )
    thread.start()
    log_perf_event(
        "ui.watchdog.started",
        threshold_ms=f"{threshold_ms:.1f}",
        log_path=str(_LOG_PATH),
    )


def log_elapsed(
    event: str,
    start_s: float,
    threshold_ms: float | None = None,
    **fields: Any,
) -> None:
    if not _ENABLED:
        return
    elapsed_ms = (time.perf_counter() - start_s) * 1000.0
    threshold = _DEFAULT_THRESHOLD_MS if threshold_ms is None else threshold_ms
    if elapsed_ms >= threshold:
        log_perf_event(event, elapsed_ms=f"{elapsed_ms:.1f}", **fields)


FieldGetter = Callable[[Any, tuple[Any, ...], dict[str, Any]], dict[str, Any]]


def log_slow_method(
    event: str,
    threshold_ms: float | None = None,
    fields: FieldGetter | None = None,
):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(self, *args, **kwargs):
            if not _ENABLED:
                return func(self, *args, **kwargs)
            start_s = time.perf_counter()
            try:
                return func(self, *args, **kwargs)
            finally:
                extra: dict[str, Any] = {}
                if fields is not None:
                    try:
                        extra = fields(self, args, kwargs)
                    except Exception:
                        extra = {}
                log_elapsed(event, start_s, threshold_ms, **extra)

        return wrapper

    return decorator
