"""磁盘缓存 TSM 渲染缓存。

设计：
- 切换播放速度（≠ 1.0x）时，后台 worker 用 Pedalboard 的 time_stretch 渲染，
  结果保存到磁盘缓存文件，不占用大量内存。
- 播放时从磁盘缓存读取到内存。
- 缓存文件位于程序所在目录下的 .cache 文件夹，更换歌曲或退出时自动清理。
- 1.0x 特殊路径：直接返回原始 PCM 引用，零渲染开销。
- 缓存文件采用 MP3 格式压缩，节省磁盘空间。

分块并行渲染架构：
- 最多同时渲染 2 个不同速度（MAX_SPEEDS）
- 每个速度内部，音频按"低能量切点"分成多个块（音乐感知分块，块长 25~35s
  浮动），由多个 worker 并行处理
- 块之间有 10% 渲染重叠保护 PV 端点质量，拼接时提取 core 区域直接硬切
  （不交叉淡化）：切点选在 RMS 局部最低 + 零交叉处，听觉上不可闻；而
  crossfade 会糊化 sustain 尾音，反而不如硬切清晰

缓存文件命名：{歌曲名}_{speed}x.mp3
"""

from __future__ import annotations

import ctypes
import heapq
import os
import sys
import threading
import time
from collections import OrderedDict
from concurrent.futures import CancelledError, ThreadPoolExecutor, Future
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set

import numpy as np
import soundfile as sf
from pedalboard import time_stretch
from pedalboard.io import AudioFile

from strange_uta_game import app_dirs


ProgressCallback = Callable[[float, float], None]  # (speed, 0.0~1.0)
DoneCallback = Callable[[float], None]              # (speed,)
LoadProgressCallback = Callable[[str, float], None]  # (stage, 0.0~1.0)


# ---- Windows 线程优先级 ----
_THREAD_PRIORITY_BELOW_NORMAL = -1


def _set_worker_thread_priority() -> None:
    """将当前线程（TSMWorker）降到 BELOW_NORMAL，让音频线程优先获得 CPU。
    仅 Windows 生效，其他平台静默忽略。
    """
    if sys.platform != "win32":
        return
    try:
        handle = ctypes.windll.kernel32.GetCurrentThread()
        ctypes.windll.kernel32.SetThreadPriority(handle, _THREAD_PRIORITY_BELOW_NORMAL)
    except Exception:
        pass

_SPEED_QUANT = 2  # round(speed, 2)，0.01 精度
_CACHE_DIR_NAME = ".cache"
_MP3_QUALITY = 128  # MP3 比特率 (kbps)

# 分块渲染参数
_MAX_SPEEDS = 2         # 最多同时渲染的速度数
_CHUNK_SECONDS = 30     # 名义块秒数（实际块长会在 ±_SEARCH_RADIUS_SECONDS 内浮动）
_OVERLAP_RATIO = 0.1    # 渲染重叠比例 10%，保证 TSM 渲染质量
_CPU_USAGE_RATIO = 0.7  # CPU 使用比例上限

# 音乐感知切点（低能量 splice point）参数
_SEARCH_RADIUS_SECONDS = 5.0   # 名义边界 ±5s 内搜索 RMS 最低点
_MIN_CHUNK_SECONDS = 15.0      # 切点选完后的最短块尺寸（防 PV 启动延迟占比过大）
_RMS_FRAME_SIZE = 2048         # ~46ms @ 44.1kHz
_RMS_HOP_SIZE = 512            # ~12ms @ 44.1kHz
_ZERO_CROSS_SEARCH = 16        # 零交叉对齐搜索半径（样本），防 click

# 接缝处理参数（在渲染域，非源域）
_SEAM_ZC_SEARCH = 64           # 渲染域切点过零重对齐搜索半径（样本，~1.5ms @44.1k）
_SEAM_XFADE_SECONDS = 0.008    # 接缝等功率微淡化时长（8ms），用各块已渲染的 overlap


def _get_max_workers() -> int:
    """根据 CPU 核心数计算全局最大 worker 数，不超过 70%。"""
    import os
    cpu_count = os.cpu_count() or 4
    return max(1, int(cpu_count * _CPU_USAGE_RATIO))


def _quantize(speed: float) -> float:
    return round(float(speed), _SPEED_QUANT)


def _get_cache_dir() -> Path:
    """获取缓存目录（与 project_store / video_converter 同源）。

    解析逻辑见 :mod:`strange_uta_game.app_dirs`：``SUG_CACHE_DIR`` 最高优先，
    macOS 用 ``~/Library/Caches``，其余平台用程序目录下的 ``.cache``。
    """
    return app_dirs.cache_dir()


def _get_cache_path(song_name: str, speed: float) -> Path:
    """获取缓存文件路径"""
    cache_dir = _get_cache_dir()
    q = _quantize(speed)
    filename = f"{song_name}_{q}x.mp3"
    return cache_dir / filename


def _get_source_mp3_path(song_name: str) -> Path:
    """获取源 MP3 文件路径"""
    cache_dir = _get_cache_dir()
    return cache_dir / f"{song_name}_source.mp3"


def clear_cache() -> None:
    """清空所有缓存文件"""
    cache_dir = _get_cache_dir()
    for f in cache_dir.glob("*.mp3"):
        try:
            f.unlink()
        except Exception:
            pass
    print(f"[TSM缓存] 已清空全部缓存文件: {cache_dir}")


def clear_cache_for_song(song_name: str) -> None:
    """清空指定歌曲的所有缓存文件（包括源 MP3）"""
    cache_dir = _get_cache_dir()
    # 歌名可能含 glob 元字符（[]*?），拼进 glob 模式会导致永不匹配；
    # 改为遍历缓存目录后按前缀过滤。
    prefix = f"{song_name}_"
    for f in cache_dir.glob("*.mp3"):
        if not f.name.startswith(prefix):
            continue
        try:
            f.unlink()
        except Exception:
            pass
    source = cache_dir / f"{song_name}_source.mp3"
    if source.exists():
        try:
            source.unlink()
        except Exception:
            pass
    print(f"[TSM缓存] 已清空歌曲缓存: {song_name}")


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """原子写文件：先写同目录临时文件，再 ``os.replace`` 改名到目标路径。

    缓存 MP3 直接 ``open(final_path, "wb")`` 写会留下一段"文件已存在但内容
    未写完"的窗口；此时 UI 线程靠 ``path.exists()`` 判断渲染就绪并用 BASS
    打开，会读到 0 字节/截断的 MP3，导致变速音频偶发损坏。``os.replace``
    在同一卷上是原子的，读端要么看到旧文件、要么看到完整新文件。

    临时文件名带 pid + 线程 id，避免多个写入者撞名；扩展名 ``.tmp`` 不匹配
    ``clear_cache`` 的 ``*.mp3`` glob，不会被误当作缓存清理。
    """
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except Exception:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
        raise


@dataclass
class ChunkInfo:
    """分块信息"""
    index: int
    src_start: int       # 源 PCM 中的起始采样（含 overlap）
    src_end: int         # 源 PCM 中的结束采样（含 overlap）
    core_start: int      # 核心区域起始（不含 overlap）
    core_end: int        # 核心区域结束（不含 overlap）


def _frame_rms_db(mono: np.ndarray, frame_size: int, hop_size: int) -> np.ndarray:
    """逐帧 RMS（dB），O(n) 累积和实现，向量化无 Python 循环。"""
    n = len(mono)
    if n < frame_size:
        rms = float(np.sqrt(np.mean(mono * mono) + 1e-12))
        return np.array([20.0 * np.log10(rms + 1e-9)], dtype=np.float32)
    sq = mono.astype(np.float64)
    sq *= sq
    cs = np.concatenate(([0.0], np.cumsum(sq)))
    num_frames = (n - frame_size) // hop_size + 1
    starts = np.arange(num_frames, dtype=np.int64) * hop_size
    sums = cs[starts + frame_size] - cs[starts]
    rms = np.sqrt(np.maximum(sums / frame_size, 1e-12))
    return (20.0 * np.log10(rms + 1e-9)).astype(np.float32)


def _snap_zero_crossing(mono: np.ndarray, sample: int, search: int) -> int:
    """在 ±search 样本内找最近零交叉点，返回新的样本下标。

    硬切若不落在零交叉点会产生 click；选择 RMS 最低点后再做一次 16 样本级的
    零交叉对齐，可在不引入感知偏移的前提下消掉切点 click。
    """
    n = len(mono)
    if sample <= 1 or sample >= n - 1:
        return max(0, min(sample, n - 1))
    lo = max(1, sample - search)
    hi = min(n - 1, sample + search)
    seg = mono[lo - 1 : hi + 1]
    signs = np.sign(seg)
    signs[signs == 0] = 1.0
    crossings = np.where(signs[1:] != signs[:-1])[0]
    if len(crossings) == 0:
        return sample
    abs_pos = crossings.astype(np.int64) + lo
    best = int(abs_pos[np.argmin(np.abs(abs_pos - sample))])
    return best


def _plan_chunks(pcm: np.ndarray, sample_rate: int) -> List[ChunkInfo]:
    """音乐感知分块：在名义边界 ±5s 内寻找 RMS 最低点作为切点，零交叉对齐。

    设计要点：
    - 切点落在能量最低段（换气、间奏间隙、乐句末尾衰减），任何相位失配/样本
      对齐误差在听觉上被信号本身的静音掩盖，听不出"加速感"。
    - PV 仍按 10% overlap 渲染保护端点质量；拼接时硬切（不交叉淡化），避免
      crossfade 把 sustain 尾音糊化。
    - 块长在 [_MIN_CHUNK_SECONDS, _CHUNK_SECONDS + _SEARCH_RADIUS_SECONDS] 浮动，
      最大块长不平衡 ~40%，对多 worker 负载均衡基本无害。
    - 切点表与速度无关：set_source 时算一次，所有速度档（0.2~2.0x）复用同一表。

    短音频（<= _CHUNK_SECONDS）走单块快速路径，不切。
    """
    n = len(pcm)
    chunk_samples = _CHUNK_SECONDS * sample_rate
    overlap_samples = int(chunk_samples * _OVERLAP_RATIO)
    search_radius = int(_SEARCH_RADIUS_SECONDS * sample_rate)
    min_chunk_samples = int(_MIN_CHUNK_SECONDS * sample_rate)

    if n <= chunk_samples:
        return [ChunkInfo(index=0, src_start=0, src_end=n,
                          core_start=0, core_end=n)]

    # 切点搜索用 mono；多声道下取均值，避免立体声反相分量影响 RMS 判定
    if pcm.ndim > 1 and pcm.shape[1] > 1:
        mono = pcm.mean(axis=1, dtype=np.float32)
    else:
        mono = np.ascontiguousarray(pcm.reshape(-1), dtype=np.float32)
    rms_db = _frame_rms_db(mono, _RMS_FRAME_SIZE, _RMS_HOP_SIZE)

    boundaries: List[int] = [0]
    nominal = chunk_samples
    while nominal < n - chunk_samples // 2:
        lo = max(0, nominal - search_radius)
        hi = min(n, nominal + search_radius)
        f_lo = lo // _RMS_HOP_SIZE
        f_hi = min(len(rms_db), hi // _RMS_HOP_SIZE + 1)
        if f_hi > f_lo:
            local = rms_db[f_lo:f_hi]
            best_frame = f_lo + int(np.argmin(local))
            cut = best_frame * _RMS_HOP_SIZE + _RMS_HOP_SIZE // 2
            cut = max(lo, min(cut, hi))
            chosen_db = float(local[best_frame - f_lo])
        else:
            cut = nominal
            chosen_db = float("nan")
        cut = _snap_zero_crossing(mono, cut, _ZERO_CROSS_SEARCH)
        # 防止与上一切点过近导致小块
        cut = max(cut, boundaries[-1] + min_chunk_samples)
        # 防止最后一块过小：与末端的距离也得 >= 最小块尺寸
        if cut >= n - min_chunk_samples:
            break
        offset_s = (cut - nominal) / sample_rate
        print(
            f"[TSM切点] 名义 {nominal / sample_rate:.1f}s → 实际"
            f" {cut / sample_rate:.1f}s (偏移 {offset_s:+.2f}s, RMS"
            f" {chosen_db:.1f} dB)"
        )
        boundaries.append(cut)
        nominal = cut + chunk_samples
    boundaries.append(n)

    chunks: List[ChunkInfo] = []
    last_idx = len(boundaries) - 2
    for idx in range(len(boundaries) - 1):
        core_start = boundaries[idx]
        core_end = boundaries[idx + 1]
        src_start = max(0, core_start - overlap_samples) if idx > 0 else core_start
        src_end = min(n, core_end + overlap_samples) if idx < last_idx else core_end
        chunks.append(ChunkInfo(
            index=idx,
            src_start=src_start,
            src_end=src_end,
            core_start=core_start,
            core_end=core_end,
        ))
    return chunks


@dataclass
class SpeedTask:
    """单个速度的渲染任务"""
    speed: float
    priority: int
    progress_cb: Optional[ProgressCallback]
    done_cb: Optional[DoneCallback]
    version: int
    chunks: List[ChunkInfo] = field(default_factory=list)
    results: Dict[int, np.ndarray] = field(default_factory=dict)  # {chunk_index: pcm}
    pending_chunks: Set[int] = field(default_factory=set)  # 待处理的块 index
    futures: List[Future] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    completed: threading.Event = field(default_factory=threading.Event)
    cancelled: bool = False


# 全局线程池（所有速度任务共享，总并发数不超过 CPU 70%）
_executor: Optional[ThreadPoolExecutor] = None
_executor_lock = threading.Lock()

# 专用 finalizer 线程池：单线程，负责 merge + MP3 编码 + 磁盘写入。
# 与 TSMWorker 池隔离，确保这些重操作不占用渲染 worker 槽，
# 且 Worker 的 done_callback 只做检查和投递，立即返回。
_finalizer_executor: Optional[ThreadPoolExecutor] = None
_finalizer_lock = threading.Lock()


def _get_executor() -> ThreadPoolExecutor:
    """获取全局渲染线程池（懒初始化）。"""
    global _executor
    with _executor_lock:
        if _executor is None:
            max_workers = _get_max_workers()
            _executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="TSMWorker")
            print(f"[TSM渲染池] 已初始化，最大 worker 数: {max_workers}")
        return _executor


def _get_finalizer_executor() -> ThreadPoolExecutor:
    """获取专用 finalizer 线程池（单线程，懒初始化）。"""
    global _finalizer_executor
    with _finalizer_lock:
        if _finalizer_executor is None:
            _finalizer_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="TSMFinalizer")
        return _finalizer_executor


class TSMRenderCache:
    """磁盘缓存 TSM 渲染缓存，支持分块并行渲染和优先级队列。

    架构：加载时将原始音频保存为源 MP3，后续所有操作（播放、TSM 渲染）
    都从这份 MP3 读取，减少内存占用。
    """

    # MP3 支持的采样率
    _MP3_TARGET_SR = 44100

    def __init__(self) -> None:
        self._source_mp3_path: Optional[Path] = None
        self._sample_rate: int = 0
        self._channels: int = 0
        self._song_name: str = ""

        # 速度级别任务队列：(priority, speed, progress_cb, done_cb)
        self._speed_queue: list = []
        self._queue_lock = threading.Lock()

        # 正在渲染的速度任务：{speed: SpeedTask}
        self._active_tasks: Dict[float, SpeedTask] = {}
        self._active_lock = threading.Lock()

        # 版本控制
        self._render_version: int = 0
        self._lock = threading.Lock()

        # 调度线程
        self._scheduler_thread: Optional[threading.Thread] = None
        self._scheduler_stop = threading.Event()

        # 内存级别的 PCM 缓存，避免重复读盘解码
        self._memory_cache: OrderedDict[float, np.ndarray] = OrderedDict()
        self._mem_cache_lock = threading.Lock()
        self._MAX_MEM_CACHE = 5

        # 音乐感知切点表：set_source 时计算一次，所有速度档共用。
        # 切点位置与速度无关，仅取决于源音频的 RMS 形态。
        self._chunk_plan: Optional[List[ChunkInfo]] = None

    # ---------- 加载 ----------

    def set_source(
        self,
        song_name: str,
        original_pcm: np.ndarray,
        sample_rate: int,
        progress_cb: Optional[LoadProgressCallback] = None,
    ) -> None:
        """切换原始音频。将原始 PCM 保存为源 MP3，清空旧缓存。"""
        self._cancel_all_and_wait()
        with self._lock:
            if progress_cb:
                progress_cb("清理旧缓存...", 0.0)
            clear_cache()
            with self._mem_cache_lock:
                self._memory_cache.clear()
            self._chunk_plan = None

            self._song_name = song_name
            channels = int(original_pcm.shape[1]) if original_pcm.ndim > 1 else 1

            if progress_cb:
                progress_cb("转换为 MP3...", 0.1)
            source_path = _get_source_mp3_path(song_name)
            actual_sr = self._save_source_as_mp3(
                original_pcm, sample_rate, channels, source_path, progress_cb
            )
            self._source_mp3_path = source_path
            self._sample_rate = actual_sr
            self._channels = channels

            # 切点表必须在「解码后的源 MP3」上计算——它正是 _render_chunk 后续要切的
            # 同一份数组（采样率 = actual_sr、长度 N_src，含 MP3 编解码延迟/补齐）。
            # 不能用入参 original_pcm（原始采样率 sample_rate、长度 N_orig），否则切点
            # 产出的样本下标与被切数组不在同一索引空间，会出现两类错位：
            #   1. 文件采样率不在 MP3 支持档（32k/44.1k/48k）被重采样时，_plan_chunks
            #      用 actual_sr 算块长却套在原始采样率数组上，切点整体按
            #      actual_sr/sample_rate 缩放错位，末块 core_end=N_orig≠N_src，
            #      源 MP3 尾部整段被丢弃（或下标越界导致 total_rendered 计算偏大）；
            #   2. 即使不重采样，MP3 编码延迟也让两份数组整体错位 ~26ms，切点偏离真实
            #      RMS 最低点。
            # _load_source_pcm() 解码后缓存（key=1.0），调度线程随后取用直接命中缓存，
            # 无额外解码开销。解码失败才退回 original_pcm。
            try:
                plan_pcm = self._load_source_pcm()
                if plan_pcm is None:
                    plan_pcm = original_pcm
                self._chunk_plan = _plan_chunks(plan_pcm, actual_sr)
                if len(self._chunk_plan) > 1:
                    print(
                        f"[TSM缓存] 已规划 {len(self._chunk_plan)} 块（音乐感知切点，"
                        f"块长 {_MIN_CHUNK_SECONDS:.0f}~{_CHUNK_SECONDS + _SEARCH_RADIUS_SECONDS:.0f}s 浮动）"
                    )
            except Exception as exc:
                # 任何意外都不应阻塞音频加载——切点表为 None 时调度循环会回落
                # 到旧的固定 30s 切分逻辑。
                print(f"[TSM缓存] 切点规划失败，回退固定切分: {exc}")
                self._chunk_plan = None

            if progress_cb:
                progress_cb("完成", 1.0)
            print(f"[TSM缓存] 源音频已保存为 MP3: {source_path} ({actual_sr}Hz, {channels}ch)")

    def _save_source_as_mp3(
        self,
        pcm: np.ndarray,
        sample_rate: int,
        channels: int,
        path: Path,
        progress_cb: Optional[LoadProgressCallback] = None,
    ) -> int:
        """将 PCM 保存为 MP3，如果采样率不支持则降采样。"""
        mp3_rates = [32000, 44100, 48000]
        target_sr = sample_rate
        if sample_rate not in mp3_rates:
            target_sr = min(mp3_rates, key=lambda r: abs(r - sample_rate))
            print(f"[TSM缓存] 采样率不兼容，重采样 {sample_rate}Hz → {target_sr}Hz")

        data = pcm
        if target_sr != sample_rate:
            if progress_cb:
                progress_cb("降采样中...", 0.3)
            from pedalboard.io import StreamResampler
            resampler = StreamResampler(sample_rate, target_sr, channels)
            resampled = resampler.process(pcm.T)
            tail = resampler.process(None)
            resampled_full = np.concatenate([resampled, tail], axis=1)
            data = resampled_full.T.astype(np.float32)

        if progress_cb:
            progress_cb("编码 MP3...", 0.6)
        mp3_bytes = AudioFile.encode(
            data.T,
            samplerate=target_sr,
            format="mp3",
            num_channels=channels,
            quality=_MP3_QUALITY,
        )
        if progress_cb:
            progress_cb("保存文件...", 0.9)
        _atomic_write_bytes(path, mp3_bytes)

        return target_sr

    def clear(self) -> None:
        self._cancel_all_and_wait()
        with self._lock:
            if self._song_name:
                clear_cache_for_song(self._song_name)
            self._source_mp3_path = None
            self._chunk_plan = None
        with self._mem_cache_lock:
            self._memory_cache.clear()

    # ---------- 查询 ----------

    def get(self, speed: float) -> Optional[np.ndarray]:
        """从缓存读取。优先查内存缓存，未命中再查磁盘。"""
        if self._source_mp3_path is None:
            return None
        q = _quantize(speed)
        if abs(q - 1.0) < 1e-9:
            return self._load_source_pcm()

        # 先查内存缓存
        with self._mem_cache_lock:
            if q in self._memory_cache:
                self._memory_cache.move_to_end(q)
                return self._memory_cache[q]

        # 内存没有，查磁盘缓存
        cache_path = _get_cache_path(self._song_name, q)
        if cache_path.exists():
            try:
                data = self._load_from_mp3(cache_path)
                # 存入内存缓存
                with self._mem_cache_lock:
                    self._memory_cache[q] = data
                    if len(self._memory_cache) > self._MAX_MEM_CACHE:
                        self._memory_cache.popitem(last=False)
                return data
            except Exception as e:
                print(f"[TSM缓存] 读取磁盘缓存失败: {e}")
        return None

    def _load_source_pcm(self) -> Optional[np.ndarray]:
        """从源 MP3 加载 PCM 数据（带内存缓存）。"""
        # 1.0x 对应的量化键
        q = 1.0
        with self._mem_cache_lock:
            if q in self._memory_cache:
                self._memory_cache.move_to_end(q)
                return self._memory_cache[q]

        if self._source_mp3_path is None or not self._source_mp3_path.exists():
            return None
        data = self._load_from_mp3(self._source_mp3_path)
        with self._mem_cache_lock:
            self._memory_cache[q] = data
            if len(self._memory_cache) > self._MAX_MEM_CACHE:
                self._memory_cache.popitem(last=False)
        return data

    def _load_from_mp3(self, path: Path) -> np.ndarray:
        """从 MP3 文件加载 PCM 数据。返回 (samples, channels) float32。"""
        with AudioFile(str(path)) as f:
            audio = f.read(f.frames)
        return audio.T.astype(np.float32)

    # ---------- 渲染 ----------

    def ensure(
        self,
        speed: float,
        priority: int = 99,
        progress_cb: Optional[ProgressCallback] = None,
        done_cb: Optional[DoneCallback] = None,
        preempt: bool = False,
        check_only: bool = False,
    ) -> Optional[np.ndarray]:
        """确保 ``speed`` 对应的 PCM 就绪。

        ``preempt=True``（UI 主动申请的当前速度）：打断正在渲染的其它速度任务、
        让出 worker 槽，使本次以最高优先级立即开跑；被打断的任务按原优先级
        重新入队，稍后继续。

        ``check_only=True``（预热路径）：只做存在性检查——内存缓存命中或磁盘
        缓存文件存在即视为就绪并返回 ``True``，未就绪则派发渲染任务并返回
        ``None``。**绝不做整曲 MP3 解码**：预热一次会检查 10 个速度档，若每档
        都把磁盘缓存整曲解码进内存（且大多被 LRU 挤掉），会在调用线程上产生
        长阻塞和 GB 级无效内存占用。
        """
        if self._source_mp3_path is None:
            return None
        q = _quantize(speed)
        if abs(q - 1.0) < 1e-9:
            return True if check_only else self._load_source_pcm()

        if check_only:
            # check_only：只查存在性，不解码。未就绪则跳过解码、直接落入
            # 下方派发路径（预热本来就是要触发后台渲染）。
            with self._mem_cache_lock:
                if q in self._memory_cache:
                    return True
            if _get_cache_path(self._song_name, q).exists():
                return True
        else:
            # 检查磁盘缓存（命中会整曲解码进内存——预热路径不走这里）
            cached = self.get(q)
            if cached is not None:
                print(f"[TSM缓存] 缓存命中，速度 {q}x，无需渲染")
                return cached

        # 检查是否已在活跃渲染中
        with self._active_lock:
            if q in self._active_tasks:
                active_task = self._active_tasks[q]
                # 如果新请求带有 done_cb（用户主动调速），需要把回调注入到
                # 正在跑的任务里，否则预热任务完成后不会触发换源/进度上报。
                # 用 task.lock 保护，避免与 _finalize_task / 闭包回调竞争。
                if done_cb is not None or progress_cb is not None:
                    with active_task.lock:
                        if done_cb is not None:
                            active_task.done_cb = done_cb
                        if progress_cb is not None:
                            active_task.progress_cb = progress_cb
                    print(
                        f"[TSM调度] 速度 {q}x 正在渲染中，注入用户回调"
                        f"（换源回调={'已注入' if done_cb is not None else '无'}）"
                    )
                else:
                    print(f"[TSM调度] 速度 {q}x 已在渲染中，跳过重复派发")
                return None

        with self._queue_lock:
            existing = next(
                ((pr, sp, pc, dc) for (pr, sp, pc, dc) in self._speed_queue
                 if abs(sp - q) < 1e-9),
                None,
            )
            if existing is not None:
                # 已在队列。非抢占、或新优先级不更高 → 跳过；否则提升优先级
                # （并采用本次 UI 提供的回调），让它插到队首。
                if not preempt and priority >= existing[0]:
                    print(f"[TSM调度] 速度 {q}x 已在队列中，跳过重复派发")
                    return None
                self._speed_queue = [
                    item for item in self._speed_queue if abs(item[1] - q) >= 1e-9
                ]
                heapq.heapify(self._speed_queue)
                print(f"[TSM调度] 速度 {q}x 已在队列中，提升优先级至 {priority}，替换回调")

        # 抢占：让出当前正在渲染的其它速度，使本次请求立即获得 worker 槽。
        if preempt:
            self._preempt_active(keep_speed=q)

        with self._queue_lock:
            heapq.heappush(self._speed_queue, (priority, q, progress_cb, done_cb))
            print(
                f"[TSM调度] 速度 {q}x 入队，优先级 {priority}"
                f"{'（抢占模式）' if preempt else '（预热模式）'}"
            )

        self._ensure_scheduler_running()
        return None

    def _preempt_active(self, keep_speed: float) -> None:
        """打断当前正在渲染的（除 ``keep_speed`` 外）速度任务。

        标记取消并取消其尚未开始的块 future（已运行的块靠 cancelled 标志早退），
        从活跃表移除以释放速度槽，并按原优先级重新入队以便稍后续渲。
        """
        requeue = []
        with self._active_lock:
            for spd, task in list(self._active_tasks.items()):
                if abs(spd - keep_speed) < 1e-9:
                    continue
                task.cancelled = True
                for f in task.futures:
                    f.cancel()
                # 清空已渲染的 chunk 数据，防止被抢占后残留的脏数据
                # 在下次重新入队渲染时污染新 SpeedTask（闭包回调已绑定旧
                # task，但旧 task.results 里的数据已无意义，及时释放内存）。
                with task.lock:
                    task.results.clear()
                # 被抢占的任务降级为预热任务：清除 done_cb 和 progress_cb。
                # 理由：当前用户目标速度已经变成 keep_speed，这个任务的速度
                # 对用户而言不再是"期望换源"的目标。若将来用户再次调到该
                # 速度，set_speed() 会通过 ensure(preempt=True) 重新注入正确
                # 的 done_cb；若用户从未再调到该速度，则它仅作后台预热存在。
                # 保留原始优先级（非 -1）用于后续调度排序。
                requeue.append((task.priority, spd, None, None))
                del self._active_tasks[spd]

        if not requeue:
            return
        with self._queue_lock:
            for item in requeue:
                if not any(abs(qs - item[1]) < 1e-9 for _, qs, _, _ in self._speed_queue):
                    heapq.heappush(self._speed_queue, item)
                    print(f"[TSM调度] 速度 {item[1]}x 被抢占，降级为预热任务，重新入队（优先级 {item[0]}）")

    # ---------- 调度 ----------

    def _ensure_scheduler_running(self) -> None:
        """确保调度线程在运行。"""
        with self._lock:
            if self._scheduler_thread is not None and self._scheduler_thread.is_alive():
                return
            self._scheduler_stop.clear()
            self._scheduler_thread = threading.Thread(
                target=self._scheduler_loop, daemon=True, name="TSMScheduler"
            )
            self._scheduler_thread.start()

    def _scheduler_loop(self) -> None:
        """调度线程：从速度队列取出任务，将块提交到全局线程池。"""
        while not self._scheduler_stop.is_set():
            task = None
            with self._queue_lock:
                if self._speed_queue:
                    task = heapq.heappop(self._speed_queue)

            if task is None:
                with self._active_lock:
                    if not self._active_tasks:
                        break
                time.sleep(0.1)
                continue

            priority, speed, progress_cb, done_cb = task

            with self._lock:
                current_version = self._render_version

            # 占位 + 等待空闲速度槽位（原子）。
            # 任务一旦出队，必须在放进 _active_tasks 之前完成耗时的 _load_source_pcm
            # 解码，否则这段窗口内该速度既不在队列也不在 active 表，并发的 ensure()
            # 会判定它"未在渲染"而重复派发 → 同一速度渲染两遍、向同一缓存文件写两次。
            # 这里在确认有空槽的同一把 _active_lock 内立即登记一个空 SpeedTask 占位，
            # 关闭该窗口；chunks 稍后填充（占位时尚未知）。
            speed_task = SpeedTask(
                speed=speed,
                priority=priority,
                progress_cb=progress_cb,
                done_cb=done_cb,
                version=current_version,
            )
            reserved = False
            while not self._scheduler_stop.is_set():
                with self._active_lock:
                    if speed in self._active_tasks:
                        # 已被占（正常情况下 ensure 会先拦截重复派发）；放弃本次。
                        break
                    if len(self._active_tasks) < _MAX_SPEEDS:
                        self._active_tasks[speed] = speed_task
                        reserved = True
                        break
                time.sleep(0.05)

            if not reserved:
                if self._scheduler_stop.is_set():
                    break
                continue

            def _release_reservation() -> None:
                with self._active_lock:
                    if self._active_tasks.get(speed) is speed_task:
                        del self._active_tasks[speed]

            if self._scheduler_stop.is_set():
                _release_reservation()
                break

            # 读取源 PCM 并取得切点表
            source_pcm = self._load_source_pcm()
            if source_pcm is None:
                _release_reservation()
                continue

            chunks = self._chunk_plan
            if chunks is None:
                # 切点表缺失（set_source 未走或失败）：现场算一次并缓存。
                # 现场计算失败则用整曲单块作为最低限度的回退。
                try:
                    chunks = _plan_chunks(source_pcm, self._sample_rate)
                except Exception as exc:
                    print(f"[TSM调度] 现场切点规划失败，整曲单块兜底: {exc}")
                    chunks = [ChunkInfo(
                        index=0, src_start=0, src_end=len(source_pcm),
                        core_start=0, core_end=len(source_pcm),
                    )]
                self._chunk_plan = chunks
            print(f"[TSM渲染] 速度 {speed}x 开始渲染，共 {len(chunks)} 块，提交至线程池")

            # 填充占位任务的分块信息（占位时 chunks 尚未知）。
            with speed_task.lock:
                speed_task.chunks = chunks
                speed_task.pending_chunks = set(range(len(chunks)))

            # 占位到此可能已被抢占/全局取消：此时不提交任何块，释放占位后跳过。
            if speed_task.cancelled or self._render_version != current_version:
                _release_reservation()
                continue

            # 将所有块提交到全局线程池
            # 使用闭包回调，直接绑定 speed_task 引用，避免通过 chunk_index
            # 反查活跃任务表时发生跨任务数据污染（尤其是抢占重入场景）。
            executor = _get_executor()
            chunk_done_cb = self._make_chunk_done_callback(speed_task)
            for chunk in chunks:
                future = executor.submit(
                    self._render_chunk,
                    speed_task, source_pcm, chunk, current_version,
                )
                future.add_done_callback(chunk_done_cb)
                speed_task.futures.append(future)

    def _render_chunk(
        self,
        task: SpeedTask,
        source_pcm: np.ndarray,
        chunk: ChunkInfo,
        render_version: int,
    ) -> Optional[tuple]:
        """渲染单个块（在线程池中执行）。返回 (chunk_index, rendered_pcm) 或 None。"""
        # 降低 worker 线程优先级，避免与 AudioProducer 抢 CPU。
        _set_worker_thread_priority()

        if task.cancelled or self._render_version != render_version:
            return None

        try:
            chunk_pcm = source_pcm[chunk.src_start:chunk.src_end]
            chunk_pcm = np.ascontiguousarray(chunk_pcm, dtype=np.float32)

            rendered = time_stretch(
                chunk_pcm,
                float(self._sample_rate),
                stretch_factor=task.speed,
            )

            if task.cancelled or self._render_version != render_version:
                return None

            return (chunk.index, rendered.astype(np.float32))

        except Exception as e:
            print(f"[TSM渲染] 速度 {task.speed}x 第 {chunk.index} 块渲染出错: {e}")
            return None

    def _make_chunk_done_callback(self, task: SpeedTask) -> Callable[["Future"], None]:
        """为指定 SpeedTask 创建块完成回调，通过闭包直接持有 task 引用，
        避免通过 chunk_index 反查活跃任务表而引入跨任务数据污染。
        """
        def _on_chunk_done(future: Future) -> None:
            """块完成回调（在 TSMWorker 线程中执行）。

            此回调必须极轻量：只做结果存储、进度上报、完成检测和 finalizer 投递，
            绝不执行 merge / MP3编码 / 磁盘IO 等重操作（那些移到 TSMFinalizer 线程）。
            """
            try:
                result = future.result()
            except CancelledError:
                return
            if result is None:
                return

            # 任务已被取消（抢占等），丢弃结果，不写入 task.results
            if task.cancelled:
                return

            chunk_index, rendered_pcm = result

            all_done = False
            with task.lock:
                # 二次确认：取消标志可能在获取锁之前刚被设置
                if task.cancelled:
                    return
                task.results[chunk_index] = rendered_pcm
                task.pending_chunks.discard(chunk_index)
                progress = len(task.results) / len(task.chunks)
                # 检查是否所有块都成功完成
                if (len(task.results) == len(task.chunks)
                        and not task.completed.is_set()
                        and all(v is not None for v in task.results.values())):
                    task.completed.set()
                    all_done = True

            # 报告渲染进度（Worker 线程，轻量）
            # 用 task.lock 读取 progress_cb，防止与 ensure() 的回调升级竞争。
            with task.lock:
                progress_cb = task.progress_cb
            if progress_cb is not None:
                try:
                    progress_cb(task.speed, progress * 0.9)
                except Exception:
                    pass

            # 所有块完成 → 投递到专用 finalizer 线程执行 merge+保存，立即返回
            if all_done:
                _get_finalizer_executor().submit(self._finalize_task, task)

        return _on_chunk_done

    def _finalize_task(self, task: SpeedTask) -> None:
        """在 TSMFinalizer 线程中执行：merge + MP3编码 + 磁盘写入。

        此函数在专用单线程 finalizer 中运行，与 TSMWorker 池完全隔离，
        不会占用渲染 worker 槽，也不会在 Worker 的 done_callback 里阻塞。
        """
        try:
            # 入口复查：任务可能在全部块完成与 finalizer 开跑之间被取消
            # （切歌 _cancel_all_and_wait / 抢占 _preempt_active / 版本推进）。
            # 不复查的话会用「当时」的 _song_name 把旧歌 PCM 写成新歌的缓存
            # 文件——之后调速就静默播放错歌音频。
            if task.cancelled or self._render_version != task.version:
                print(f"[TSM渲染] 速度 {task.speed}x 任务已取消/过期，丢弃合并结果")
                return

            print(f"[TSM渲染] 速度 {task.speed}x 全部块完成，开始合并...")
            # 用 task.lock 读取回调，防止与 ensure() 的回调升级竞争。
            # 在 merge 前快照一次，后续复用快照值（merge 期间回调不可能再变）。
            with task.lock:
                progress_cb = task.progress_cb
                done_cb = task.done_cb

            if progress_cb:
                try:
                    progress_cb(task.speed, 0.95)
                except Exception:
                    pass

            final_pcm = self._merge_chunks(task)

            # 写盘前二次复查：merge 耗时较长，期间可能发生切歌/抢占。
            if task.cancelled or self._render_version != task.version:
                print(f"[TSM渲染] 速度 {task.speed}x 合并后任务已取消/过期，丢弃结果")
                return

            # 保存到磁盘
            cache_path = _get_cache_path(self._song_name, task.speed)
            self._save_as_mp3(final_pcm, cache_path)
            print(f"[TSM渲染] 速度 {task.speed}x 渲染完成，已写入缓存: {cache_path.name}")

            # 释放 chunk results 占用的内存（merge 后不再需要）
            with task.lock:
                task.results.clear()

            if progress_cb:
                try:
                    progress_cb(task.speed, 1.0)
                except Exception:
                    pass

            if done_cb:
                try:
                    done_cb(task.speed)
                except Exception as e:
                    print(f"[TSM渲染] 速度 {task.speed}x 换源回调执行出错: {e}")

        except Exception as e:
            print(f"[TSM渲染] 速度 {task.speed}x 合并出错: {e}")
        finally:
            with self._active_lock:
                # 仅当 active 表里登记的就是本 task 时才移除：被抢占后本 task 已
                # 被 _preempt_active 从表中删除，调度器可能已为同一速度新建并登记
                # 了下一代 task。speed 作 key 无法区分代次，按身份比对避免误删
                # 仍在渲染的新任务（否则后续 ensure 会判定该速度未在渲染而重复派发）。
                if self._active_tasks.get(task.speed) is task:
                    del self._active_tasks[task.speed]

    def _merge_chunks(self, task: SpeedTask) -> np.ndarray:
        """拼接所有块：渲染域过零重对齐 + 8ms 等功率微淡化。

        渲染时每个块向两侧扩展 10% overlap 保证 TSM 端点质量。此处不再"取 core
        硬切"——硬切的去 click 依赖 _plan_chunks 在源域选的过零切点，而 time_stretch
        之后该点既不再过零、两块在边界的相位也各自独立，硬切在密集编曲处会周期性
        爆 click。改为：

        1. 把每块 core 的接缝侧切点在【渲染域】重新吸附到最近过零点（_SEAM_ZC_SEARCH），
           令切点本身落在零值附近；
        2. 用各块已渲染、原本被丢弃的 overlap，在接缝处做 8ms 等功率交叉淡化。
           前一块多取 8ms 进入它的右 overlap、后一块的头 8ms 与之 overlap-add——两者
           覆盖的是边界【同一段源音频】，故只补相位、不串内容、不引入时长漂移。

        内存：仍预分配一次输出缓冲区逐段写入；交叉淡化只读已写入的尾部就地混合，
        不额外分配大数组。
        """
        sorted_chunks = sorted(task.chunks, key=lambda c: c.index)
        last = len(sorted_chunks) - 1

        total_core_samples = sum(c.core_end - c.core_start for c in sorted_chunks)
        total_rendered = int(total_core_samples / task.speed)
        if total_rendered <= 0:
            return np.zeros((0, self._channels), dtype=np.float32)

        xfade = max(0, int(_SEAM_XFADE_SECONDS * self._sample_rate))
        # core 段相加 ≈ total_rendered；处理中最多有一段未消化的 overlap 扩展 +
        # 过零吸附偏移，留出余量防越界，末尾截断。
        cap = total_rendered + xfade + _SEAM_ZC_SEARCH + 16
        result = np.empty((cap, self._channels), dtype=np.float32)
        write_pos = 0
        prev_ext = 0  # 上一块尾部已写入、待与本块头交叉淡化的样本数

        for k, chunk in enumerate(sorted_chunks):
            rendered = task.results[chunk.index]
            if rendered is None:
                raise ValueError(f"Chunk {chunk.index} is None")

            n_r = len(rendered)
            # 按 ratio 把源域 core 边界映射到渲染域（time_stretch 对稳态内容近似线性）
            src_len = chunk.src_end - chunk.src_start
            core_start = int(n_r * ((chunk.core_start - chunk.src_start) / src_len))
            core_end = int(n_r * ((chunk.core_end - chunk.src_start) / src_len))

            # 接缝侧切点在渲染域吸附到最近过零点（外缘——首块左、末块右——不动）
            mono = rendered.mean(axis=1) if rendered.ndim > 1 and rendered.shape[1] > 1 \
                else rendered.reshape(-1)
            if k > 0:
                core_start = _snap_zero_crossing(mono, core_start, _SEAM_ZC_SEARCH)
            if k < last:
                core_end = _snap_zero_crossing(mono, core_end, _SEAM_ZC_SEARCH)
            core_start = max(0, min(core_start, n_r))
            core_end = max(core_start, min(core_end, n_r))

            # 非末块向右多取 xfade 进入右 overlap，供与下一块头部 overlap-add（同源区域）
            ext = min(xfade, n_r - core_end) if k < last else 0
            segment = rendered[core_start : core_end + ext]
            seg_len = len(segment)
            if seg_len <= 0:
                continue

            if write_pos == 0:
                m = min(seg_len, cap)
                np.copyto(result[:m], segment[:m])
                write_pos = m
            else:
                # 本块头 x 样本与上一块尾部 prev_ext 样本等功率交叉淡化（就地混合）
                x = min(prev_ext, seg_len, write_pos)
                if x > 0:
                    t = np.linspace(0.0, 1.0, x, dtype=np.float32)
                    fade_out = np.cos(t * (np.pi / 2.0))[:, None]  # 1→0 等功率
                    fade_in = np.sin(t * (np.pi / 2.0))[:, None]   # 0→1
                    blended = (result[write_pos - x : write_pos] * fade_out
                               + segment[:x] * fade_in)
                    np.copyto(result[write_pos - x : write_pos], blended)
                rest = segment[x:]
                m = min(len(rest), cap - write_pos)
                if m > 0:
                    np.copyto(result[write_pos : write_pos + m], rest[:m])
                    write_pos += m
            prev_ext = ext

        return result[:write_pos]

    def _save_as_mp3(self, pcm: np.ndarray, path: Path) -> None:
        """将 PCM 数据保存为 MP3 文件（原子写，避免读端读到半截文件）。"""
        mp3_bytes = AudioFile.encode(
            pcm.T,
            samplerate=self._sample_rate,
            format="mp3",
            num_channels=self._channels,
            quality=_MP3_QUALITY,
        )
        _atomic_write_bytes(path, mp3_bytes)

    def _cancel_all_and_wait(self) -> None:
        """取消所有渲染任务并等待完成。"""
        with self._lock:
            self._render_version += 1

        # 取消所有活跃任务
        with self._active_lock:
            for task in self._active_tasks.values():
                task.cancelled = True
            tasks = list(self._active_tasks.values())

        # 等待所有 future 完成
        for task in tasks:
            for future in task.futures:
                future.cancel()

        with self._active_lock:
            self._active_tasks.clear()

        with self._queue_lock:
            self._speed_queue.clear()

        self._scheduler_stop.set()
        if self._scheduler_thread and self._scheduler_thread.is_alive():
            self._scheduler_thread.join(timeout=2.0)
        self._scheduler_thread = None
