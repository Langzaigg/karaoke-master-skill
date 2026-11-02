"""NetEase Cloud Music ``.ncm`` container → the lossless file inside it.

An ``.ncm`` is the player's encrypted wrapper around an ordinary FLAC/MP3:

    "CTENFDAM"      8   magic
    gap             2
    key_len         4   little-endian
    key_data        -   each byte XOR 0x64, then AES-128-ECB (core key),
                        unpad, drop the leading "neteasecloudmusic"
    meta_len        4   little-endian
    meta_data       -   each byte XOR 0x63, base64, AES-128-ECB (meta key),
                        unpad, drop 6 bytes ("music:"), UTF-8 JSON
    crc32           4
    gap             5
    image_len       4
    image           -   cover art
    audio           -   raw file XORed with a 256-byte RC4-derived keystream

The keystream index only depends on ``i & 0xFF``, so the 256-entry pattern is
computed once and applied with numpy — the reference implementation does it in a
Python loop, which takes minutes on a 116 MB lossless file.

The km runtime ships no cryptography package (and installing one is unreliable on
this machine), so AES-128 decryption is implemented here in pure Python. Only
decryption is needed — two 16-byte blocks per file.
"""

from __future__ import annotations

import base64
import json
import struct
from pathlib import Path

MAGIC = b"CTENFDAM"
CORE_KEY = bytes.fromhex("687A4852416D736F356B496E62617857")   # "hzHRAmso5kInbaxX"
META_KEY = bytes.fromhex("2331346C6A6B5F215C5D2630553C2728")   # "#14ljk_!\\]&0U<'"


# --------------------------------------------------------------------- AES-128
def _xtime(a: int) -> int:
    a <<= 1
    if a & 0x100:
        a ^= 0x11B
    return a & 0xFF


def _mul(a: int, b: int) -> int:
    out = 0
    while b:
        if b & 1:
            out ^= a
        a = _xtime(a)
        b >>= 1
    return out


def _tables():
    inv = [0] * 256
    for a in range(1, 256):
        for b in range(1, 256):
            if _mul(a, b) == 1:
                inv[a] = b
                break
    sbox, inv_sbox = [0] * 256, [0] * 256
    for a in range(256):
        v, x = inv[a], 0
        for _ in range(5):                      # affine transform
            x = ((x << 1) | (x >> 7)) & 0xFF
            x ^= v
        x ^= 0x63
        sbox[a] = x
        inv_sbox[x] = a
    return sbox, inv_sbox


SBOX, INV_SBOX = _tables()
RCON = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36]


def _expand(key: bytes) -> list[list[int]]:
    w = [list(key[i * 4:i * 4 + 4]) for i in range(4)]
    for i in range(4, 44):
        t = list(w[i - 1])
        if i % 4 == 0:
            t = t[1:] + t[:1]
            t = [SBOX[b] for b in t]
            t[0] ^= RCON[i // 4 - 1]
        w.append([w[i - 4][j] ^ t[j] for j in range(4)])
    return [sum(w[r * 4:r * 4 + 4], []) for r in range(11)]


def _decrypt_block(block: bytes, round_keys: list[list[int]]) -> bytes:
    s = [block[r + 4 * c] ^ round_keys[10][r + 4 * c] for c in range(4) for r in range(4)]
    for rnd in range(9, -1, -1):
        # inverse shift rows (row r rotates right by r)
        s = [s[((c - r) % 4) * 4 + r] for c in range(4) for r in range(4)]
        s = [INV_SBOX[b] for b in s]
        s = [b ^ k for b, k in zip(s, round_keys[rnd])]
        if rnd:                                  # inverse mix columns
            out = []
            for c in range(4):
                a = s[c * 4:c * 4 + 4]
                out += [
                    _mul(a[0], 14) ^ _mul(a[1], 11) ^ _mul(a[2], 13) ^ _mul(a[3], 9),
                    _mul(a[0], 9) ^ _mul(a[1], 14) ^ _mul(a[2], 11) ^ _mul(a[3], 13),
                    _mul(a[0], 13) ^ _mul(a[1], 9) ^ _mul(a[2], 14) ^ _mul(a[3], 11),
                    _mul(a[0], 11) ^ _mul(a[1], 13) ^ _mul(a[2], 9) ^ _mul(a[3], 14),
                ]
            s = out
    return bytes(s)


def aes_ecb_decrypt(data: bytes, key: bytes) -> bytes:
    if len(data) % 16:
        data = data[: len(data) // 16 * 16]
    rk = _expand(key)
    return b"".join(_decrypt_block(data[i:i + 16], rk) for i in range(0, len(data), 16))


def _unpad(data: bytes) -> bytes:
    if not data:
        return data
    n = data[-1]
    return data[:-n] if 0 < n <= 16 else data


# ------------------------------------------------------------------------ NCM
def _key_box(key_data: bytes) -> list[int]:
    box = list(range(256))
    c = last = off = 0
    n = len(key_data)
    for i in range(256):
        swap = box[i]
        c = (swap + last + key_data[off]) & 0xFF
        off += 1
        if off >= n:
            off = 0
        box[i] = box[c]
        box[c] = swap
        last = c
    return box


def read_meta(path: str | Path) -> dict:
    """Metadata + inner format, without writing anything."""
    with open(path, "rb") as f:
        if f.read(8) != MAGIC:
            raise ValueError(f"{Path(path).name} 不是 ncm 文件（缺少 CTENFDAM 头）")
        f.seek(2, 1)
        key_len = struct.unpack("<I", f.read(4))[0]
        key_data = _unpad(aes_ecb_decrypt(bytes(b ^ 0x64 for b in f.read(key_len)), CORE_KEY))[17:]
        meta_len = struct.unpack("<I", f.read(4))[0]
        raw = bytes(b ^ 0x63 for b in f.read(meta_len))
        meta: dict = {}
        if raw:
            try:
                text = _unpad(aes_ecb_decrypt(base64.b64decode(raw[22:]), META_KEY)).decode("utf-8")[6:]
                meta = json.loads(text)
            except Exception:
                meta = {}
    meta["_key_len"] = len(key_data)
    meta.setdefault("format", "flac")
    return meta


def dump(path: str | Path, out_dir: str | Path | None = None, *, overwrite: bool = False,
         progress=None) -> Path:
    """Decrypt ``path`` and write the inner audio file. Returns the new path."""
    import numpy as np

    path = Path(path)
    out_dir = Path(out_dir) if out_dir else path.parent

    with open(path, "rb") as f:
        if f.read(8) != MAGIC:
            raise ValueError(f"{path.name} 不是 ncm 文件（缺少 CTENFDAM 头）")
        f.seek(2, 1)
        key_len = struct.unpack("<I", f.read(4))[0]
        key_raw = bytes(b ^ 0x64 for b in f.read(key_len))
        key_data = _unpad(aes_ecb_decrypt(key_raw, CORE_KEY))[17:]
        box = _key_box(key_data)

        meta_len = struct.unpack("<I", f.read(4))[0]
        raw = bytes(b ^ 0x63 for b in f.read(meta_len))
        meta = {}
        if raw:
            try:
                text = _unpad(aes_ecb_decrypt(base64.b64decode(raw[22:]), META_KEY)).decode("utf-8")[6:]
                meta = json.loads(text)
            except Exception:
                meta = {}
        f.seek(4 + 5, 1)                                 # crc32 (4) + gap (5)
        image_len = struct.unpack("<I", f.read(4))[0]
        f.seek(image_len, 1)                             # cover art

        fmt = str(meta.get("format") or "flac").lower()
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / (path.stem + "." + fmt)
        if out.exists() and not overwrite:
            return out

        # 256-byte keystream; byte at position i uses index ((i + 1) & 0xFF), continuing across chunks
        ks = np.array([box[(box[m] + box[(box[m] + m) & 0xFF]) & 0xFF] for m in range(256)], dtype=np.uint8)
        total = 0
        with open(out, "wb") as w:
            while True:
                chunk = f.read(0x10000)
                if not chunk:
                    break
                a = np.frombuffer(chunk, dtype=np.uint8)
                idx = (np.arange(len(a), dtype=np.uint32) + 1 + total) & 0xFF
                w.write((a ^ ks[idx]).tobytes())
                total += len(a)
                if progress:
                    progress(total)
    meta["_out"] = str(out)
    return out


def ensure_audio(path: str | Path, out_dir: str | Path | None = None, *, log=None) -> Path:
    """Return a usable audio path: ``.ncm`` gets decrypted, anything else passes through."""
    p = Path(path)
    if p.suffix.lower() != ".ncm":
        return p
    if log:
        log(f"解密 ncm：{p.name}")
    out = dump(p, out_dir)
    if log:
        log(f"ncm 已转为 {out.name}")
    return out


def is_ncm(path: str | Path) -> bool:
    return Path(path).suffix.lower() == ".ncm"


def describe(path: str | Path) -> str:
    m = read_meta(path)

    def names(node) -> list[str]:
        """NCM nests artists as [[{...}, {...}], ...] (one inner list per artist credit)."""
        if isinstance(node, dict):
            return [str(node.get("name") or "")]
        if isinstance(node, (list, tuple)):
            out: list[str] = []
            for item in node:
                out += names(item)
            return out
        return []

    artists = "、".join(n for n in names(m.get("artist")) if n) or str(m.get("album") or "")
    return "%s - %s（%s，%s）" % (artists or "?", m.get("musicName") or Path(path).stem,
                                 str(m.get("format") or "?").upper(), m.get("album") or "")
