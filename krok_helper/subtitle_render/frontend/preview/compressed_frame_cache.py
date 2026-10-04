"""压缩帧缓存：band 打包 + LZ4/zlib，PR 式预渲染回放的存储层。

设计要点（2026-10，对标 Premiere Pro 预览文件 / AE RAM preview）：
- 字幕层帧内容是 (t, 场景) 的纯函数且大面积透明——只存内容行带；
- 无损压缩：解码结果与原帧逐位一致（不引入任何视觉差异）；
- 编解码器优先 LZ4（解压 ~1.3GB/s，回放 15ms 预算内绰绰有余；
  真实字幕帧比率 ~8-30x），无 LZ4 时回退 zlib（解压 ~240MB/s）；
- 压缩后每帧约 0.2~2MB（原始 4K 帧 15~33MB），同等内存预算可缓存
  数秒时间线，播放回放时"解压 + 合成"取代"渲染 + 回读 + 跨进程"。

线程安全：encode/decode 的压缩调用在 C 层释放 GIL，可在工作线程与
编码线程并发使用。
"""
from __future__ import annotations

import struct
import threading
import zlib
from collections import OrderedDict
from typing import Optional

import numpy as np
from PyQt6.QtGui import QImage

try:  # LZ4 可选依赖：缺失时自动回退 zlib（编解码仍无损，只是回放解码慢 ~3x）
    import lz4.frame as _lz4_frame

    _HAS_LZ4 = True
except ImportError:  # pragma: no cover - 环境相关
    _lz4_frame = None
    _HAS_LZ4 = False

# 头部: magic(4) codec(1) version(1) width(4) height(4) band_count(4)
_HEADER = struct.Struct("<4sBBIII")
_BAND = struct.Struct("<II")
_MAGIC = b"KSFC"
_VERSION = 1
_CODEC_ZLIB = 1
_CODEC_LZ4 = 2


def extract_content_bands(alpha_rows: np.ndarray) -> list[tuple[int, int]]:
    """把逐行 alpha 指纹压成 (top, height) 行带列表。"""
    nz = np.flatnonzero(alpha_rows)
    if nz.size == 0:
        return []
    bands: list[tuple[int, int]] = []
    start = prev = int(nz[0])
    for idx in nz[1:]:
        i = int(idx)
        if i != prev + 1:
            bands.append((start, prev - start + 1))
            start = i
        prev = i
    bands.append((start, prev - start + 1))
    return bands


def encode_frame(image: QImage) -> bytes:
    """把 QImage 压缩成自包含 blob（无损、含行带稀疏化 + LZ4/zlib）。"""
    dpr = image.devicePixelRatioF() or 1.0
    width = image.width()
    height = image.height()
    ptr = image.constBits()
    ptr.setsize(image.sizeInBytes())
    rows = np.frombuffer(ptr, dtype=np.uint8).reshape(
        height, image.bytesPerLine()
    )[:, : width * 4]
    alpha_fingerprint = rows.reshape(height, width, 4)[..., 3].max(axis=1)
    bands = extract_content_bands(alpha_fingerprint)
    tail = b"".join(_BAND.pack(top, band_h) for top, band_h in bands)
    dpr_bytes = struct.pack("<d", dpr)

    if not bands:
        return _HEADER.pack(
            _MAGIC, _CODEC_ZLIB, _VERSION, width, height, 0
        ) + dpr_bytes

    packed_parts: list[bytes] = []
    for top, band_h in bands:
        packed_parts.append(rows[top : top + band_h].tobytes())
    packed = b"".join(packed_parts)
    if _HAS_LZ4:
        codec = _CODEC_LZ4
        compressed = _lz4_frame.compress(packed, compression_level=1)
    else:
        codec = _CODEC_ZLIB
        compressed = zlib.compress(packed, 1)
    header = _HEADER.pack(_MAGIC, codec, _VERSION, width, height, len(bands))
    # dpr 附加在末尾（8 字节 double），解码后由调用方 setDevicePixelRatio
    return header + tail + compressed + dpr_bytes


def decode_frame(blob: bytes) -> Optional[QImage]:
    """解压 blob 还原 QImage（与原帧逐位一致；透明帧返回全零帧）。"""
    if len(blob) < _HEADER.size + 8:
        return None
    magic, codec, version, width, height, band_count = _HEADER.unpack_from(
        blob, 0
    )
    if magic != _MAGIC or version != _VERSION or width <= 0 or height <= 0:
        return None
    offset = _HEADER.size
    bands: list[tuple[int, int]] = []
    for _ in range(band_count):
        top, band_h = _BAND.unpack_from(blob, offset)
        offset += _BAND.size
        if top < 0 or band_h <= 0 or top + band_h > height:
            return None
        bands.append((top, band_h))
    dpr = struct.unpack_from("<d", blob, len(blob) - 8)[0]
    compressed = blob[offset : len(blob) - 8]
    if band_count == 0:
        packed = b""
    else:
        try:
            if codec == _CODEC_LZ4:
                if not _HAS_LZ4:
                    return None
                packed = _lz4_frame.decompress(compressed)
            elif codec == _CODEC_ZLIB:
                packed = zlib.decompress(compressed)
            else:
                return None
        except (zlib.error, RuntimeError, ValueError):
            return None
        expected = sum(band_h for _top, band_h in bands) * width * 4
        if len(packed) != expected:
            return None

    image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(0)
    image.setDevicePixelRatio(dpr if dpr > 0 else 1.0)
    if band_count == 0:
        return image
    dest_ptr = image.bits()
    dest_ptr.setsize(image.sizeInBytes())
    dest = np.frombuffer(dest_ptr, dtype=np.uint8).reshape(
        height, image.bytesPerLine()
    )
    src = np.frombuffer(packed, dtype=np.uint8)
    pos = 0
    row_bytes = width * 4
    for top, band_h in bands:
        chunk = band_h * row_bytes
        band = src[pos : pos + chunk].reshape(band_h, row_bytes)
        dest[top : top + band_h, :row_bytes] = band
        pos += chunk
    return image


class CompressedFrameCache:
    """线程安全的 LRU 压缩帧缓存，按字节预算驱逐。"""

    def __init__(self, budget_bytes: int) -> None:
        self._budget = max(int(budget_bytes), 1)
        self._items: OrderedDict[int, bytes] = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()

    def store(self, t_ms: int, blob: bytes) -> None:
        key = int(t_ms)
        with self._lock:
            old = self._items.pop(key, None)
            if old is not None:
                self._bytes -= len(old)
            self._items[key] = blob
            self._bytes += len(blob)
            while self._bytes > self._budget and len(self._items) > 1:
                _k, evicted = self._items.popitem(last=False)
                self._bytes -= len(evicted)

    def fetch(self, t_ms: int) -> Optional[bytes]:
        with self._lock:
            blob = self._items.get(int(t_ms))
            if blob is not None:
                self._items.move_to_end(int(t_ms))
            return blob

    def has(self, t_ms: int) -> bool:
        with self._lock:
            return int(t_ms) in self._items

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._bytes = 0

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    @property
    def bytes(self) -> int:
        with self._lock:
            return self._bytes
