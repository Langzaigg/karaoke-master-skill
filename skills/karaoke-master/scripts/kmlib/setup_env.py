"""``km.py setup``: install wizard (stdlib only, runs with any Python 3.10–3.13).

The skill folder only ships this wizard and the requirement lists. Everything
heavy is expanded into an *install home*:

* ``--target user``     → ``%LOCALAPPDATA%\\KaraokeMaster`` / ``~/.karaoke-master`` (default)
* ``--target project``  → ``<current dir>/.karaoke-master`` (keeps it with the project)
* ``--target <dir>``    → any directory

Layout of an install home::

    km_config.json   install target, engine location, acceleration settings
    venv/            skill runtime (PyQt6 + Lin-K Lyrics / SUG dependencies)
    ai_venv/         AI runtime, CPU baseline (torch, transformers,
                     audio-separator, onnxruntime, faster-whisper)
    engine/          Lin-K Lyrics checkout, only when the skill is not already
                     inside one (pinned tag, SUG submodule included)
    models/ jobs/    model weights (downloaded on first use) and job folders

Two venvs because the AI stack pins versions (transformers 5.15, librosa 0.10)
that conflict with the app's locked numpy — the same split SUG uses.
Hardware acceleration is not installed here: the agent probes the machine with
``km.py accel`` and tries a GPU-capable runtime afterwards (see SKILL.md).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import accel, paths

# Where setup fetches the engine when the skill folder was copied on its own:
# 1. the minimal `skill` branch of this fork (engine subset + vendored StrangeUtaGame backend);
# 2. the official Lin-K Lyrics release the skill was validated against (full repo + submodule).
ENGINE_SOURCES = [
    ("https://github.com/Langzaigg/karaoke-master-skill", "skill"),
    ("https://github.com/karaoke-studio/karaoke-studio", "v4.3.7.1"),
]
ENGINE_URL, ENGINE_REF = ENGINE_SOURCES[-1]

AI_PROBE = ("import torch, transformers, audio_separator, faster_whisper, soundfile, onnxruntime as ort; "
            "print(torch.__version__, transformers.__version__, ort.__version__)")
APP_PROBE = "import PyQt6.QtCore, PyQt6.QtGui, numpy, pykakasi, pypinyin, jieba, soundfile; print(PyQt6.QtCore.PYQT_VERSION_STR)"


def _py(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _probe(python: Path, code: str) -> tuple[bool, str]:
    if not python.is_file():
        return False, "未安装"
    try:
        res = subprocess.run([str(python), "-c", code], capture_output=True, text=True, timeout=240,
                             encoding="utf-8", errors="replace")
    except Exception as exc:  # pragma: no cover
        return False, str(exc)
    out = (res.stdout or res.stderr).strip().splitlines()
    return res.returncode == 0, (out[-1] if out else "")


def _create_venv(target: Path) -> Path:
    if not _py(target).is_file():
        print(f"[setup] 创建虚拟环境 {target}", flush=True)
        subprocess.run([sys.executable, "-m", "venv", str(target)], check=True)
    py = _py(target)
    subprocess.run([str(py), "-m", "pip", "install", "-q", "--upgrade", "pip"], check=True)
    return py


def _pip(py: Path, *args: str) -> None:
    print("[setup] pip install " + " ".join(args), flush=True)
    subprocess.run([str(py), "-m", "pip", "install", *args], check=True)


def resolve_target(target: str | None) -> Path:
    if not target or target == "user":
        return paths.user_home_default()
    if target == "project":
        return Path.cwd() / paths.PROJECT_DIR_NAME
    return Path(target).expanduser().resolve()


# ------------------------------------------------------------------ engine
def ensure_engine(home: Path, ref: str) -> Path:
    found = paths._find_repo_root()
    if found is not None:
        sug = found / "krok_helper" / "lyrics_timing" / "src" / "strange_uta_game"
        if not sug.is_dir() and (found / ".gitmodules").is_file() and shutil.which("git"):
            # clone without --recursive: fetch the StrangeUtaGame submodule now
            print("[setup] 初始化 StrangeUtaGame 子模块", flush=True)
            subprocess.run(["git", "-C", str(found), "submodule", "update", "--init", "--recursive",
                            "krok_helper/lyrics_timing"], check=True)
        return found
    bundled = paths.SKILL_DIR / "engine"
    gitmodules = paths.SKILL_DIR / ".gitmodules"
    declared = gitmodules.is_file() and "path = engine" in gitmodules.read_text(encoding="utf-8")
    if declared and shutil.which("git"):
        # uninitialised submodule (minimal clone of a standalone skill repo): init on demand
        print("[setup] 初始化技能内置的 Lin-K Lyrics 子模块", flush=True)
        subprocess.run(["git", "-C", str(paths.SKILL_DIR), "submodule", "update", "--init", "--recursive", "engine"],
                       check=True)
        return bundled
    dest = home / "engine" / "karaoke-studio"
    if not (dest / "krok_helper").is_dir():
        if not shutil.which("git"):
            raise SystemExit("需要 git 才能获取 Lin-K Lyrics 引擎（或设置 KM_REPO 指向已有检出）")
        dest.parent.mkdir(parents=True, exist_ok=True)
        sources = [(u, r) for u, r in ENGINE_SOURCES if r == ref] if ref != ENGINE_REF else list(ENGINE_SOURCES)
        for url, branch in sources or [(ENGINE_URL, ref)]:
            print(f"[setup] 获取 Lin-K Lyrics 引擎 {url} @ {branch} → {dest}", flush=True)
            cmd = ["git", "clone", "--depth", "1", "--branch", branch, "--recurse-submodules", "--shallow-submodules",
                   url, str(dest)]
            if subprocess.run(cmd).returncode == 0 and (dest / "krok_helper").is_dir():
                break
            shutil.rmtree(dest, ignore_errors=True)
        else:
            raise SystemExit("无法获取 Lin-K Lyrics 引擎，请检查网络，或设置 KM_REPO 指向已有检出")
    sug = dest / "krok_helper" / "lyrics_timing" / "src" / "strange_uta_game"
    if not sug.is_dir():
        subprocess.run(["git", "-C", str(dest), "submodule", "update", "--init", "--recursive"], check=True)
    return dest


# ------------------------------------------------------------------ status
def status(home: Path | None = None) -> dict:
    home = home or paths.km_home()
    cfg = accel.load(home)
    repo = paths._find_repo_root()
    sug_ok = bool(repo and (repo / "krok_helper" / "lyrics_timing" / "src" / "strange_uta_game").is_dir())
    app_ok, app_msg = _probe(_py(home / "venv"), APP_PROBE)
    ai_py = Path(accel.profile().get("ai_python") or _py(home / "ai_venv"))
    ai_ok, ai_msg = _probe(ai_py, AI_PROBE)
    models = home / "models"
    settings = accel.profile()
    gpus = accel.list_gpus()
    accelerated = any(settings.get(k) not in (None, "cpu", "auto") for k in ("align_device", "onnx", "encoder"))
    return {
        "home": str(home),
        "target": cfg.get("target"),
        "engine": {"path": str(repo) if repo else None, "sug_submodule": sug_ok,
                   "mode": None if not repo else ("in-place" if paths.SKILL_DIR.is_relative_to(repo) else "separate")},
        "ffmpeg": shutil.which("ffmpeg") or os.environ.get("KM_FFMPEG_DIR"),
        "python": str(_py(home / "venv")),
        "app_env": {"ok": app_ok, "detail": app_msg},
        "ai_python": str(ai_py),
        "ai_env": {"ok": ai_ok, "detail": ai_msg},
        "gpus": gpus,
        "acceleration": settings,
        "acceleration_hint": None if accelerated or not gpus else
            "检测到显卡但尚未启用硬件加速：运行 km.py accel 查看可用设备，并按 SKILL.md「Hardware acceleration」尝试",
        "models": {
            "align": any((models / "align").glob("*/manifest.json")) if (models / "align").is_dir() else False,
            "separation": (models / "separation" / "UVR-MDX-NET-Inst_HQ_3.onnx").is_file(),
            "whisper": (models / "whisper").is_dir() and any((models / "whisper").rglob("model.bin")),
            "note": "缺失的模型在首次使用时自动下载（对齐 ~1.2GB，分离 ~60MB，识别 ~0.8GB）",
        },
        "km": f'"{_py(home / "venv")}" "{paths.SKILL_DIR / "scripts" / "km.py"}"',
        "ready": bool(app_ok and ai_ok and sug_ok and shutil.which("ffmpeg")),
    }


# ------------------------------------------------------------------ main
def main(check: bool = False, with_models: bool = False, target: str | None = None,
         engine_ref: str = ENGINE_REF, reinstall_ai: bool = False) -> int:
    home = resolve_target(target) if target else paths.km_home()
    os.environ["KM_HOME"] = str(home)
    if check:
        info = status(home)
        print(json.dumps(info, ensure_ascii=False, indent=1))
        return 0 if info["ready"] else 1

    home.mkdir(parents=True, exist_ok=True)
    cfg = accel.load(home)
    cfg.update(target=target or cfg.get("target") or "user", updated_at=time.time())
    accel.save(home, cfg)
    print(f"[setup] 安装目录：{home}", flush=True)

    repo = ensure_engine(home, engine_ref)
    cfg["engine"] = str(repo)
    accel.save(home, cfg)

    app_ok, _ = _probe(_py(home / "venv"), APP_PROBE)
    if not app_ok:
        py = _create_venv(home / "venv")
        _pip(py, "-r", str(paths.SKILL_DIR / "requirements.txt"))

    ai_ok, _ = _probe(_py(home / "ai_venv"), AI_PROBE)
    if not ai_ok or reinstall_ai:
        if reinstall_ai and (home / "ai_venv").is_dir():
            shutil.rmtree(home / "ai_venv", ignore_errors=True)
        py = _create_venv(home / "ai_venv")
        _pip(py, "-r", str(paths.SKILL_DIR / "requirements-ai.txt"))
    if with_models:
        code = ("import sys; sys.path.insert(0, r'%s'); from kmlib import sugbridge; print(sugbridge.ensure_align_model())"
                % (paths.SKILL_DIR / "scripts"))
        subprocess.run([str(_py(home / "venv")), "-c", code], check=False, env=dict(os.environ, KM_HOME=str(home)))
    info = status(home)
    print(json.dumps(info, ensure_ascii=False, indent=1))
    return 0 if info["ready"] else 1
