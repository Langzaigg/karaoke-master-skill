"""Path discovery: skill dir, install home (KM_HOME), engine repo, venvs, ffmpeg.

Install home resolution (first match wins):

1. ``KM_HOME`` environment variable;
2. the install that owns the running interpreter (``<home>/venv`` or
   ``<home>/ai_venv`` next to ``km_config.json``);
3. a project-local install: ``.karaoke-master/km_config.json`` in the current
   directory or any parent (``km.py setup --target project``);
4. the per-user default ``%LOCALAPPDATA%\\KaraokeMaster`` / ``~/.karaoke-master``.

Engine (Lin-K Lyrics repo with the SUG submodule): ``KM_REPO``, else the
checkout that contains this skill, else ``<home>/engine/karaoke-studio``
(cloned by ``km.py setup``).
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = SKILL_DIR / "scripts"
WEB_DIR = SKILL_DIR / "web"
CONFIG_NAME = "km_config.json"
PROJECT_DIR_NAME = ".karaoke-master"


def user_home_default() -> Path:
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "KaraokeMaster"
    return Path.home() / ".karaoke-master"


def find_project_home(start: Path | None = None) -> Path | None:
    cur = (start or Path.cwd()).resolve()
    for d in [cur, *cur.parents]:
        if (d / PROJECT_DIR_NAME / CONFIG_NAME).is_file():
            return d / PROJECT_DIR_NAME
    return None


def km_home() -> Path:
    env = os.environ.get("KM_HOME")
    if env:
        home = Path(env)
    else:
        prefix = Path(sys.prefix).resolve()
        if prefix.name in ("venv", "ai_venv") and (prefix.parent / CONFIG_NAME).is_file():
            home = prefix.parent
        else:
            home = find_project_home() or user_home_default()
    home.mkdir(parents=True, exist_ok=True)
    return home


def _find_repo_root() -> Path | None:
    env = os.environ.get("KM_REPO")
    if env:
        return Path(env).resolve()
    for parent in [SKILL_DIR, *SKILL_DIR.parents]:
        if (parent / "krok_helper" / "__init__.py").is_file():
            return parent
    bundled = SKILL_DIR / "engine"  # skill published as its own repo with the engine as a submodule
    if (bundled / "krok_helper" / "__init__.py").is_file():
        return bundled
    engine = km_home() / "engine" / "karaoke-studio"
    if (engine / "krok_helper" / "__init__.py").is_file():
        return engine
    return None


REPO_ROOT = _find_repo_root()
SUG_ROOT = REPO_ROOT / "krok_helper" / "lyrics_timing" if REPO_ROOT else None
SUG_SRC = SUG_ROOT / "src" if SUG_ROOT else None


def venv_dir() -> Path:
    return km_home() / "venv"


def venv_python() -> Path:
    if os.name == "nt":
        return venv_dir() / "Scripts" / "python.exe"
    return venv_dir() / "bin" / "python"


def models_dir() -> Path:
    path = km_home() / "models"
    path.mkdir(parents=True, exist_ok=True)
    return path


def projects_dir() -> Path:
    """Where ``new`` creates KTV projects (like ppt-master): ``KM_PROJECTS`` or
    ``projects/`` in the current working folder."""
    path = Path(os.environ.get("KM_PROJECTS") or os.environ.get("KM_JOBS") or (Path.cwd() / "projects"))
    path.mkdir(parents=True, exist_ok=True)
    return path


jobs_dir = projects_dir  # older name


def ensure_repo_on_path() -> None:
    """Make ``krok_helper`` and ``strange_uta_game`` importable."""
    if REPO_ROOT is None:
        raise RuntimeError("找不到 Lin-K Lyrics 引擎（krok_helper）。请先运行 km.py setup，或设置 KM_REPO。")
    if not (SUG_SRC / "strange_uta_game").is_dir():
        raise RuntimeError(
            f"SUG 子模块未初始化：请在 {REPO_ROOT} 运行 git submodule update --init --recursive")
    for entry in (str(SUG_SRC), str(REPO_ROOT)):
        if entry not in sys.path:
            sys.path.insert(0, entry)


def ffmpeg_exe(name: str = "ffmpeg") -> str:
    env = os.environ.get("KM_FFMPEG_DIR")
    if env:
        candidate = Path(env) / (name + (".exe" if os.name == "nt" else ""))
        if candidate.is_file():
            return str(candidate)
    found = shutil.which(name)
    if found:
        return found
    raise RuntimeError(f"找不到 {name}，请安装 FFmpeg 或设置 KM_FFMPEG_DIR。")


def hidden_subprocess_kwargs() -> dict:
    if os.name == "nt":
        import subprocess

        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}
