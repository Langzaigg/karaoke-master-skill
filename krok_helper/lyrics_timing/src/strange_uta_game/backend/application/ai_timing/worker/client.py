"""AI 打轴宿主侧 worker 客户端（阶段 C）。

职责：

- 以子进程方式启动一次性 worker（``python -m ...ai_timing.worker``）；
- 发送版本化 align 消息，接收 progress/result/cancelled/error；
- 协作取消：发送 cancel 消息并等待宽限期，超时 terminate（§8.3）；
- 崩溃隔离：worker 进程死亡/超时转换为中文异常，不拖垮宿主；
- 任务结束回收进程与管道。

注意：当前以 ``sys.executable + -m`` 启动，适用于开发与打包后的
standalone Python 环境；PyInstaller onedir 场景的启动器适配在阶段 H
统一处理（见计划文档 §11 阶段 H）。
"""

import os
import subprocess
import sys
import threading
import time
from typing import Callable, Dict, List, Optional

from strange_uta_game.backend.application.ai_timing.alignment import (
    AlignmentRequest,
    AlignmentResult,
)
from strange_uta_game.backend.application.ai_timing.worker.protocol import (
    PROTOCOL_VERSION,
    WorkerProtocolError,
    decode_message,
    deserialize_result,
    encode_message,
    serialize_request,
)

ProgressCallback = Callable[[str, int, str], None]
"""on_progress(stage, percent, message)。"""


class AlignmentWorkerError(RuntimeError):
    """worker 启动/协议/执行失败（中文消息）。"""


class AlignmentWorkerCancelled(RuntimeError):
    """任务已取消。"""

    def __init__(self, message: str = "已取消 AI 打轴"):
        super().__init__(message)


class AlignmentWorkerTimeout(AlignmentWorkerError):
    """worker 超时被终止。"""


class AlignmentWorkerClient:
    """一次性 worker 进程的宿主侧驱动。

    典型用法（宿主线程）::

        client = AlignmentWorkerClient()
        try:
            result = client.run(request, audio_path, model_spec, on_progress)
        finally:
            client.close()
    """

    WORKER_MODULE = "strange_uta_game.backend.application.ai_timing.worker"

    def __init__(
        self,
        python_exe: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        cancel_grace_s: float = 5.0,
    ):
        if not python_exe and getattr(sys, "frozen", False):
            # 打包应用的 sys.executable 是应用 exe，不是解释器：
            # 空解释器直接报错（否则会把整个应用再启动一遍）
            self._python_exe = ""
        else:
            self._python_exe = python_exe or sys.executable
        self._env_override = env
        self._cancel_grace_s = cancel_grace_s
        self._proc: Optional[subprocess.Popen] = None
        self._stdin_lock = threading.Lock()
        self._cancel_requested = threading.Event()
        self._finished = threading.Event()

    # ── 生命周期 ──

    @staticmethod
    def _package_root() -> "Path":
        """SUG 包根目录（含 ``strange_uta_game/`` 的目录，即 src 布局的 src）。

        frozen（PyInstaller onedir）：spec 的 datas 已把 ``src/strange_uta_game``
        整树原样收进 ``_internal``（运行时即 ``sys._MEIPASS``），外部解释器
        直接从那里 import——无需任何额外的源码落盘步骤。
        """
        from pathlib import Path

        if getattr(sys, "frozen", False):
            base = getattr(sys, "_MEIPASS", "")
            return Path(base) if base else Path(sys.executable).resolve().parent

        import strange_uta_game

        return Path(strange_uta_game.__file__).resolve().parent.parent

    def _build_env(self, *, propagate_sys_path: bool) -> Dict[str, str]:
        env = dict(self._env_override or os.environ)
        if propagate_sys_path:
            # 仅当 worker 与宿主用同一解释器时传播完整 import 路径（src
            # 布局/开发模式需要）。外部 Runtime（专用 venv，自带 torch）
            # 绝不继承宿主 site-packages，否则其包版本会被宿主环境遮蔽。
            path_sep = os.pathsep
            existing = env.get("PYTHONPATH", "")
            entries: List[str] = [
                p for p in sys.path if p and p not in existing.split(path_sep)
            ]
            env["PYTHONPATH"] = path_sep.join(entries + ([existing] if existing else []))
        else:
            # 外部解释器：包根通过 -c 引导脚本的 argv 注入（见
            # _ensure_started）。绝不把包根设进 PYTHONPATH——对非嵌入式
            # Python 它排在 stdlib 之前，frozen 包根（_internal）里
            # PyInstaller 收集的本宿主版本 stdlib 扩展会遮蔽运行环境
            # 自带的版本（python313.dll vs 3.12 runtime 的
            # "Module use of pythonXXX.dll conflicts" 崩溃）
            env.pop("PYTHONPATH", None)
        # 强制子进程 stdout/stderr 为文本协议通道友好的环境
        env.setdefault("PYTHONIOENCODING", "utf-8")
        # 模型走受控本地目录：杜绝 from_pretrained 的网络探测/遥测卡顿
        env.setdefault("HF_HUB_OFFLINE", "1")
        env.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
        env.setdefault("TOKENIZERS_PARALLELISM", "false")
        # 统一日志路径按宿主口径传给 worker：外部解释器对 logs_dir 的
        # 解析（frozen 包根在 _MEIPASS）与宿主不同，不传会写错位置
        from strange_uta_game.backend.application.ai_timing.ailog import (
            ai_log_path,
        )

        env["SUG_AI_TIMING_LOG"] = str(ai_log_path())
        return env

    def _is_same_interpreter(self) -> bool:
        if getattr(sys, "frozen", False):
            # frozen 宿主的 sys.executable 是应用 exe，不是 Python 解释器：
            # 永远走外部解释器路径（runpy 引导 + 落盘源码树）
            return False
        if not self._python_exe or self._python_exe == sys.executable:
            return True
        try:
            from pathlib import Path

            return Path(self._python_exe).resolve() == Path(sys.executable).resolve()
        except OSError:
            return False

    def _ensure_started(self) -> subprocess.Popen:
        if self._proc is not None and self._proc.poll() is None:
            return self._proc
        if not self._python_exe:
            raise AlignmentWorkerError(
                "未配置对齐运行环境：请先在弹窗中「安装 / 修复」后再执行"
            )
        # 注意：这里绝不 clear 取消标志（G10）——启动窗口期的 cancel()
        #（proc 尚为 None，cancel() 只置标志不发送）若在此被 clear 会
        # 整体丢失；run() 会在 align 消息后据此补发 cancel。client 为
        # 一次性对象，标志不存在跨任务复用语义。
        self._finished.clear()
        # 重启前先回收上一轮的 stderr 临时文件（覆盖式打开会让旧句柄
        # 永久泄漏在 %TEMP%）
        self._discard_stderr_file()
        try:
            # stderr 落临时文件而非 DEVNULL：worker 崩溃时把 traceback 尾部
            # 并入错误消息，彻底告别「返回码 1」式静默失败
            import tempfile as _tf

            self._stderr_file = open(
                _tf.NamedTemporaryFile(
                    prefix="krok-aitiming-stderr-", delete=False
                ).name,
                "w",
                encoding="utf-8",
                errors="replace",
            )
            if self._is_same_interpreter():
                cmd = [self._python_exe, "-m", self.WORKER_MODULE]
                env = self._build_env(propagate_sys_path=True)
            else:
                # 外部解释器用 runpy 引导而不是 PYTHONPATH：嵌入式 Python
                # 发行版（托管 PyMSS runtime 就是）带 python312._pth，
                # 该文件存在时解释器完全忽略 PYTHONPATH，-m 会直接
                # ModuleNotFoundError。包根经 argv 注入 sys.path 后
                # runpy 等价于 -m。
                # 注意是 append 而不是 insert(0)：frozen 包根（_internal）
                # 里混着 PyInstaller 为宿主 Python（如 3.13）收集的
                # stdlib 扩展与 pythonXXX.dll——排在运行环境（3.12）
                # 自带 stdlib 之前会让 import unicodedata 等加载到版本
                # 不符的 .pyd，直接 "Module use of python313.dll
                # conflicts with this version of Python"（打包版实测）
                root = str(self._package_root())
                bootstrap = (
                    "import sys, runpy; sys.path.append(sys.argv.pop(1));"
                    f" runpy.run_module({self.WORKER_MODULE!r},"
                    " run_name='__main__')"
                )
                cmd = [self._python_exe, "-c", bootstrap, root]
                env = self._build_env(propagate_sys_path=False)
            from strange_uta_game.backend.infrastructure.windows import (
                hidden_subprocess_kwargs,
            )

            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self._stderr_file,
                env=env,
                text=True,
                encoding="utf-8",
                cwd=None,
                **hidden_subprocess_kwargs(),
            )
        except OSError as exc:
            # 启动失败同样回收刚创建的 stderr 临时文件
            self._discard_stderr_file()
            raise AlignmentWorkerError(f"无法启动对齐进程：{exc}") from exc
        return self._proc

    def _send(self, obj: dict) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise AlignmentWorkerError("对齐进程未启动")
        try:
            with self._stdin_lock:
                proc.stdin.write(encode_message(obj) + "\n")
                proc.stdin.flush()
        except (BrokenPipeError, OSError):
            # worker 已退出：由读取循环统一转换为错误
            pass

    # ── 对外接口 ──

    def run(
        self,
        request: AlignmentRequest,
        audio_path: str,
        model_spec: Dict[str, object],
        on_progress: Optional[ProgressCallback] = None,
        timeout_s: Optional[float] = None,
    ) -> AlignmentResult:
        """阻塞执行一次对齐；取消/停滞超时/崩溃分别抛对应异常。

        timeout_s 为**停滞阈值**而非总时长上限：worker 的 progress
        （stage/percent/message）有变化即重置看门狗，仅当连续 timeout_s
        无任何变化时击杀——慢机器上的长任务只要在推进就不会被超时。
        """
        proc = self._ensure_started()

        # 停滞看门狗（progress 探针）：worker 的 progress（stage, percent,
        # message）发生变化即视为有进展并重置时钟，超过 timeout_s 无任何
        # 变化才击杀——慢机器上任务总时长可以超过阈值，只要还在推进就不
        # 超时；CUDA 死锁等真挂死在阈值内无 progress 变化，仍会被终止。
        # stall_state 的跨线程读写依赖 CPython 字节码原子性，读侧拿到
        # 略旧值只会让检查晚一轮，无害。
        stall_state = {"last_change": time.monotonic(), "last_sig": None}
        timed_out = threading.Event()
        stop_watchdog = threading.Event()
        watchdog: Optional[threading.Thread] = None

        def _stall_watchdog() -> None:
            check_interval = min(30.0, max(0.2, timeout_s / 20.0))
            while not stop_watchdog.wait(check_interval):
                if self._finished.is_set():
                    return
                if time.monotonic() - stall_state["last_change"] > timeout_s:
                    if not self._finished.is_set():
                        timed_out.set()
                        # 进程树击杀：torch/CUDA 可能派生辅助子进程，只杀父
                        # 进程会把子进程孤儿化（显存占用不释放）
                        self._kill_tree(proc)
                    return

        if timeout_s is not None:
            watchdog = threading.Thread(
                target=_stall_watchdog, name="aitiming-stall-watchdog", daemon=True
            )
            watchdog.start()

        self._send(
            {
                "type": "align",
                "protocol": PROTOCOL_VERSION,
                "payload": {
                    "audio_path": str(audio_path),
                    "model": dict(model_spec),
                    "request": serialize_request(request),
                },
            }
        )
        if self._cancel_requested.is_set():
            # 启动窗口期的 cancel()（G10）：进程尚未启动时 cancel() 只能
            # 置标志、无法送达——_ensure_started 不会 clear 已置位的标志，
            # 这里在 align 消息之后补发 cancel（worker 的取消读线程在
            # provider.load 后启动，会在首个阶段边界命中）
            self._send({"type": "cancel"})

        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                try:
                    message = decode_message(line)
                except WorkerProtocolError:
                    continue  # 跳过非协议输出，保持通道健壮
                mtype = message.get("type")
                if mtype == "progress":
                    sig = (
                        str(message.get("stage", "")),
                        int(message.get("percent", 0)),
                        str(message.get("message", "")),
                    )
                    if sig != stall_state["last_sig"]:
                        # progress 有变化：重置停滞时钟
                        stall_state["last_sig"] = sig
                        stall_state["last_change"] = time.monotonic()
                    if on_progress is not None:
                        on_progress(sig[0], sig[1], sig[2])
                elif mtype == "result":
                    self._finished.set()
                    return deserialize_result(message.get("payload") or {})
                elif mtype == "cancelled":
                    self._finished.set()
                    raise AlignmentWorkerCancelled()
                elif mtype == "error":
                    self._finished.set()
                    raise AlignmentWorkerError(str(message.get("message", "对齐失败")))
            # stdout 关闭（进程退出）而无结果
            self._finished.set()
            returncode = proc.wait()
            if self._cancel_requested.is_set():
                raise AlignmentWorkerCancelled()
            if timed_out.is_set():
                raise AlignmentWorkerTimeout(
                    f"对齐无进展超时（{int(timeout_s or 0) // 60} 分钟无进度"
                    "更新），进程已终止"
                )
            detail = ""
            try:
                self._stderr_file.flush()
                with open(
                    self._stderr_file.name, encoding="utf-8", errors="replace"
                ) as fh:
                    tail = fh.read().strip().splitlines()[-6:]
                if tail:
                    detail = "；详情：" + " | ".join(tail)
            except Exception:
                pass
            raise AlignmentWorkerError(
                f"对齐进程异常退出（返回码 {returncode}），未返回结果{detail}"
            )
        finally:
            if watchdog is not None:
                stop_watchdog.set()
                watchdog.join(timeout=2)
            if not self._finished.is_set():
                self._finished.set()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._kill_tree(proc)

    def cancel(self) -> None:
        """请求协作取消；宽限期后强制终止进程。

        可从其他线程调用；run() 会以 AlignmentWorkerCancelled 返回。
        """
        self._cancel_requested.set()
        proc = self._proc
        if proc is None:
            return
        self._send({"type": "cancel"})
        # 等待协作退出
        try:
            proc.wait(timeout=self._cancel_grace_s)
            return
        except subprocess.TimeoutExpired:
            pass
        # 宽限期超时：强制终止（§8.3 必要时终止本任务拥有的 worker）。
        # 进程树击杀：只杀父进程会孤儿化 torch/CUDA 子进程
        try:
            self._kill_tree(proc)
            proc.wait(timeout=2)
        except OSError:
            pass

    def _discard_stderr_file(self) -> None:
        """关闭并删除 stderr 临时文件（幂等，防 %TEMP% 无限累积）。"""
        stderr_file = getattr(self, "_stderr_file", None)
        if stderr_file is None:
            return
        self._stderr_file = None
        name = getattr(stderr_file, "name", "")
        try:
            stderr_file.close()
        except Exception:
            pass
        try:
            os.unlink(name)
        except OSError:
            pass

    def close(self) -> None:
        """回收进程与管道（幂等）。"""
        proc = self._proc
        self._proc = None
        self._kill_tree(proc)
        self._discard_stderr_file()
        if proc is None:
            return
        for stream in (proc.stdin, proc.stdout):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        if proc.poll() is None:
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._kill_tree(proc)

    @staticmethod
    def _kill_tree(proc) -> None:
        """回收进程树（torch 导入在某些环境会派生辅助子进程，不能只等父进程）。"""
        if proc is None or proc.poll() is not None:
            return
        import sys as _sys

        try:
            if _sys.platform == "win32":
                import subprocess as _sp

                _sp.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    capture_output=True,
                    timeout=10,
                )
            else:
                proc.kill()
        except Exception:
            pass

    def __enter__(self) -> "AlignmentWorkerClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


__all__ = [
    "AlignmentWorkerClient",
    "AlignmentWorkerError",
    "AlignmentWorkerCancelled",
    "AlignmentWorkerTimeout",
]
