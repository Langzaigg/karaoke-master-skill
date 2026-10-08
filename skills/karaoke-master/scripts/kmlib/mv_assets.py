"""Image pool for the MV montage.

Sources: files the user gives, images the agent found on the web (downloaded
only after the user agreed — see SKILL.md), or distinct scenes extracted from
the job's own source video. Every asset keeps its origin (``source`` page,
``credit``) so the agent can list credits; exact duplicates (SHA-1) and near
duplicates (8×8 average hash) are rejected so the montage never shows the same
picture twice by accident.

Pool: ``render/mv_assets/assets.json`` + image files + ``thumbs/``.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import shutil
import subprocess
import time
import urllib.parse
import uuid
import zipfile
from pathlib import Path

from .jobstore import JobStore, read_json, write_json
from .paths import ffmpeg_exe, hidden_subprocess_kwargs

MIN_SHORT_SIDE = 480
NEAR_DUP_BITS = 6
MAX_BYTES = 25 * 1024 * 1024


def pool_dir(store: JobStore) -> Path:
    d = store.dir / "render" / "mv_assets"
    (d / "thumbs").mkdir(parents=True, exist_ok=True)
    return d


def load_pool(store: JobStore) -> list[dict]:
    return read_json(pool_dir(store) / "assets.json", []) or []


def save_pool(store: JobStore, pool: list[dict]) -> None:
    write_json(pool_dir(store) / "assets.json", pool)
    store.update(lambda s: s.setdefault("previews", {}).update(
        mv_assets=[{"id": a["id"], "thumb": f"render/mv_assets/thumbs/{a['id']}.jpg", "w": a["w"], "h": a["h"],
                    "origin": origin_of(a), "name": a.get("name"), "credit": a.get("credit"),
                    "source": a.get("source"), "tags": a.get("tags", [])} for a in pool]))


def origin_of(a: dict) -> str:
    """user (image packs / files the user gave) · web (found online) · video (source-video scenes)."""
    if a.get("origin"):
        return a["origin"]
    src = a.get("source") or ""
    return "video" if src.startswith("source-video@") else "web" if "://" in src else "user"


def _ahash(img) -> int:
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QImage

    small = img.scaled(8, 8, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation) \
        .convertToFormat(QImage.Format.Format_Grayscale8)
    vals = [small.pixelColor(x, y).red() for y in range(8) for x in range(8)]
    mean = sum(vals) / 64
    bits = 0
    for v in vals:
        bits = (bits << 1) | (1 if v >= mean else 0)
    return bits


def _download(url: str, dest_stem: Path) -> Path:
    import requests

    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) KaraokeMaster/1.0"}
    with requests.get(url, headers=headers, timeout=30, stream=True) as r:
        r.raise_for_status()
        ctype = r.headers.get("content-type", "")
        if not ctype.startswith("image/"):
            raise ValueError(f"不是图片（{ctype or '未知类型'}）")
        ext = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}.get(
            ctype.split(";")[0], ".jpg")
        data = bytearray()
        for chunk in r.iter_content(256 * 1024):
            data.extend(chunk)
            if len(data) > MAX_BYTES:
                raise ValueError("图片超过 25MB")
    out = dest_stem.with_suffix(ext)
    out.write_bytes(bytes(data))
    return out


IMAGE_EXTS = {".jpg", ".jpeg", ".jfif", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
MAX_LOCAL_BYTES = 80 * 1024 * 1024
ORIGIN_LABEL = {"user": "用户提供", "web": "网络", "video": "素材视频截图"}


@contextlib.contextmanager
def _pool_lock(store: JobStore, timeout: float = 120.0):
    """Short critical sections around read-modify-write of ``assets.json``
    (imports from the page and the agent may run at the same time)."""
    lock = pool_dir(store) / ".pool.lock"
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            break
        except FileExistsError:
            with contextlib.suppress(FileNotFoundError):
                if time.time() - lock.stat().st_mtime > 60:
                    lock.unlink(missing_ok=True)
                    continue
            if time.monotonic() > deadline:
                raise TimeoutError("素材池正忙")
            time.sleep(0.05)
    try:
        yield
    finally:
        lock.unlink(missing_ok=True)


def _natural_key(path) -> list:
    return [int(x) if x.isdigit() else x.lower() for x in re.split(r"(\d+)", str(path))]


def _zip_name(info: zipfile.ZipInfo) -> str:
    if info.flag_bits & 0x800:
        return info.filename
    try:  # Windows zip tools write GBK names without the UTF-8 flag
        return info.filename.encode("cp437").decode("gbk")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return info.filename


def _unzip(store: JobStore, path: Path) -> list[Path]:
    out_dir = store.dir / "uploads" / "packs" / f"{path.stem[:40]}_{int(time.time())}"
    out_dir.mkdir(parents=True, exist_ok=True)
    files = []
    with zipfile.ZipFile(path) as zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        infos.sort(key=lambda i: _natural_key(_zip_name(i)))
        for k, info in enumerate(infos):
            name = Path(_zip_name(info).replace("\\", "/"))
            if "__MACOSX" in name.parts or name.name.startswith(".") or name.suffix.lower() not in IMAGE_EXTS:
                continue
            if info.file_size > MAX_LOCAL_BYTES:
                continue
            safe = re.sub(r'[\\/:*?"<>|]+', "_", name.name)
            target = out_dir / f"{k:04d}_{safe}"  # flat folder: no zip-slip
            with zf.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            files.append(target)
    return files


def expand_sources(store: JobStore, srcs: list) -> list[dict]:
    """Paths / folders / .zip packs / URLs -> one item per image, in natural
    file order (a numbered pack keeps its order)."""
    items: list[dict] = []
    for raw in srcs:
        item = dict(raw) if isinstance(raw, dict) else {"src": raw}
        s = str(item["src"]).strip().strip('"')
        if re.match(r"^https?://", s):
            items.append({**item, "src": s, "origin": item.get("origin") or "web"})
            continue
        p = Path(s)
        if not p.is_absolute() and (store.dir / p).exists():
            p = store.dir / p  # page uploads are job-relative
        if p.is_dir():
            files = sorted((f for f in p.rglob("*") if f.is_file() and f.suffix.lower() in IMAGE_EXTS
                            and not any(part.startswith(".") or part == "__MACOSX" for part in f.parts)),
                           key=_natural_key)
        elif p.suffix.lower() == ".zip" and p.is_file():
            items += [{**item, "src": str(f), "name": re.sub(r"^\d{4}_", "", f.name), "origin": item.get("origin") or "user"}
                      for f in _unzip(store, p)]
            continue
        else:
            files = [p]
        items += [{**item, "src": str(f), "name": f.name, "origin": item.get("origin") or "user"} for f in files]
    return items


def _prepare(item: dict, staging: Path) -> dict:
    """Fetch / copy, decode, measure and thumbnail one image outside the pool lock."""
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QImage

    aid = "a" + uuid.uuid4().hex[:10]
    src = item["src"]
    try:
        if re.match(r"^https?://", src):
            path = _download(src, staging / aid)
        else:
            p = Path(src)
            if not p.is_file():
                return {"ok": False, "reason": f"文件不存在：{src}"}
            if p.stat().st_size > MAX_LOCAL_BYTES:
                return {"ok": False, "reason": "文件超过 80MB"}
            path = staging / f"{aid}{p.suffix.lower() or '.jpg'}"
            shutil.copyfile(p, path)
    except Exception as exc:
        return {"ok": False, "reason": f"获取失败：{exc}"}
    img = QImage(str(path))
    if img.isNull():
        path.unlink(missing_ok=True)
        return {"ok": False, "reason": "无法解码为图片"}
    if min(img.width(), img.height()) < MIN_SHORT_SIDE:
        path.unlink(missing_ok=True)
        return {"ok": False, "reason": f"分辨率太低（{img.width()}×{img.height()}）"}
    thumb = staging / f"{aid}.thumb.jpg"
    img.scaled(320, 180, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
               Qt.TransformationMode.SmoothTransformation).save(str(thumb), quality=82)
    return {"ok": True, "id": aid, "path": path, "thumb": thumb, "w": img.width(), "h": img.height(),
            "sha1": hashlib.sha1(path.read_bytes()).hexdigest(), "ahash": _ahash(img)}


def _commit(store: JobStore, staged: list, results: list[dict]) -> None:
    d = pool_dir(store)
    with _pool_lock(store):
        pool = load_pool(store)
        for item, r in staged:
            dup = next((a for a in pool if a["sha1"] == r["sha1"]), None)
            reason = f"与 {dup['id']} 完全相同" if dup else None
            if not dup:
                dup = next((a for a in pool if bin(int(a["ahash"]) ^ r["ahash"]).count("1") <= NEAR_DUP_BITS), None)
                reason = f"与 {dup['id']} 几乎相同" if dup else None
            if dup:
                r["path"].unlink(missing_ok=True)
                r["thumb"].unlink(missing_ok=True)
                results.append({"src": item["src"], "ok": False, "reason": reason})
                continue
            final = d / f"{r['id']}{r['path'].suffix}"
            shutil.move(str(r["path"]), final)
            shutil.move(str(r["thumb"]), d / "thumbs" / f"{r['id']}.jpg")
            source = item.get("source") or (item["src"] if item["origin"] == "web" else None)
            host = urllib.parse.urlparse(source).netloc if source and "://" in source else None
            pool.append({"id": r["id"], "file": final.name, "w": r["w"], "h": r["h"], "sha1": r["sha1"],
                         "ahash": str(r["ahash"]), "origin": item["origin"], "name": item.get("name"),
                         "source": source, "credit": item.get("credit") or host or ORIGIN_LABEL.get(item["origin"]),
                         "tags": item.get("tags") or [], "note": item.get("note"), "added": time.time()})
            results.append({"src": item["src"], "ok": True, "id": r["id"], "w": r["w"], "h": r["h"]})
        save_pool(store, pool)


def add_many(store: JobStore, srcs: list, *, origin: str | None = None, source: str | None = None,
             credit: str | None = None, tags: list[str] | None = None, progress=None) -> list[dict]:
    """Import images: local files, folders, .zip packs (user image packs) or
    http(s) URLs (only after the user agreed). ``origin`` = user / web / video
    (default: URL -> web, file -> user). Returns one result per image."""
    from .render import qt_app

    qt_app()
    defaults = {k: v for k, v in (("origin", origin), ("source", source), ("credit", credit), ("tags", tags)) if v}
    items = expand_sources(store, [{**defaults, **(s if isinstance(s, dict) else {"src": s})} for s in srcs])
    staging = pool_dir(store) / "_staging"
    staging.mkdir(exist_ok=True)
    results: list[dict] = []
    batch: list = []
    for k, item in enumerate(items):
        r = _prepare(item, staging)
        if r["ok"]:
            batch.append((item, r))
        else:
            results.append({"src": item["src"], **r})
        if batch and (len(batch) >= 8 or k == len(items) - 1):
            _commit(store, batch, results)
            batch = []
        if progress:
            progress(k + 1, len(items))
    return results


def add(store: JobStore, src: str, *, source: str | None = None, credit: str | None = None,
        tags: list[str] | None = None, note: str | None = None, origin: str | None = None) -> dict:
    """Add one image (local path or http(s) URL). Returns {"ok", "id"|"reason"}."""
    res = add_many(store, [{"src": src, "note": note}], origin=origin, source=source, credit=credit, tags=tags)
    return res[0] if res else {"ok": False, "reason": "没有可导入的图片"}


def remove(store: JobStore, ids: list[str]) -> int:
    with _pool_lock(store):
        pool = load_pool(store)
        keep, gone = [], 0
        for a in pool:
            if a["id"] in ids or "all" in ids:
                (pool_dir(store) / a["file"]).unlink(missing_ok=True)
                (pool_dir(store) / "thumbs" / f"{a['id']}.jpg").unlink(missing_ok=True)
                gone += 1
            else:
                keep.append(a)
        save_pool(store, keep)
    return gone


def from_video(store: JobStore, count: int = 40, *, threshold: float = 0.3, min_gap: float = 2.5) -> list[dict]:
    """Distinct scenes from the job's source video (two passes: scene detection
    on a small decode, then full-resolution stills at the chosen times)."""
    st = store.load()
    src = (st.get("media") or {}).get("source", {}).get("path")
    if not src or not (st["media"]["source"].get("has_video")):
        raise SystemExit("任务素材没有视频画面")
    cmd = [ffmpeg_exe(), "-hide_banner", "-i", src, "-an", "-vf",
           f"scale=480:-2,select='gt(scene,{threshold})',showinfo", "-f", "null", "-"]
    res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                         **hidden_subprocess_kwargs())
    times = [float(m) for m in re.findall(r"pts_time:([0-9.]+)", res.stderr)]
    picked: list[float] = []
    for t in times:
        if not picked or t - picked[-1] >= min_gap:
            picked.append(t)
    if len(picked) > count:  # spread evenly over the song
        step = len(picked) / count
        picked = [picked[int(k * step)] for k in range(count)]
    tmp_dir = pool_dir(store) / "_scenes"
    tmp_dir.mkdir(exist_ok=True)
    items = []
    for t in picked:
        target = tmp_dir / f"scene_{t:08.2f}.jpg"
        subprocess.run([ffmpeg_exe(), "-y", "-v", "error", "-ss", f"{t + 0.35:.2f}", "-i", src, "-frames:v", "1",
                        "-q:v", "2", str(target)], capture_output=True, **hidden_subprocess_kwargs())
        if target.exists():
            items.append({"src": str(target), "source": f"source-video@{t:.1f}s", "tags": ["scene"],
                          "origin": "video"})
    out = add_many(store, items)
    shutil.rmtree(tmp_dir, ignore_errors=True)
    return out


def credits(store: JobStore) -> list[str]:
    seen, lines = set(), []
    for a in load_pool(store):
        key = (a.get("credit"), a.get("source"))
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"{a.get('credit') or '未知来源'} — {a.get('source') or ''}".strip(" —"))
    return lines
