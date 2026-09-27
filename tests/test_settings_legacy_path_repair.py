"""更名后 settings.json 内残留旧应用名数据目录绝对路径的自愈修复。

场景：Karaoke Studio → Lin-K Lyrics 更名搬迁的是**整个**数据目录，但
settings.json 里存的绝对路径（SUG AI 打轴的缓存根、模型根、Runtime
python.exe……）不跟着改写；SUG 侧「用户显式设置的路径」又优先于宿主注入
的默认值，于是这些设置全部解析到不存在的旧目录——用户看到的就是
「AI 打轴环境丢失」。这些用例是 :func:`krok_helper.app_paths.repair_legacy_appdata_path`
与 ``load_app_settings`` 内建自愈落盘的规格说明。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from krok_helper import app_paths
from krok_helper.app_paths import settings_path_for_app_name
from krok_helper.config import APP_NAME
from krok_helper.settings import load_app_settings


@pytest.fixture(autouse=True)
def _isolated_appdata(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """把 %APPDATA% 指到 tmp_path，并清掉会短路/串味的环境变量。"""

    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv(app_paths.SETTINGS_DIR_ENV, raising=False)
    monkeypatch.setenv(app_paths.SETTINGS_APP_NAME_ENV, APP_NAME)
    # 惰性触发的「只跑一次」标记是进程级的，用例之间必须归零。
    monkeypatch.setattr(app_paths, "_migration_attempted", False)
    monkeypatch.setattr(app_paths, "_migration_running", False)
    yield


def _legacy_root(name: str) -> Path:
    return settings_path_for_app_name(name).parent


def _current_root(name: str = APP_NAME) -> Path:
    return settings_path_for_app_name(name).parent


def test_stale_path_under_legacy_dir_is_repaired_to_current_name(tmp_path: Path) -> None:
    """缓存根指向已搬空的旧目录 → 改写到当前名下确有内容的位置。"""

    content = _current_root() / "lyrics_timing_cache" / "ai_timing"
    content.mkdir(parents=True)
    stale = _legacy_root("Karaoke Studio") / "lyrics_timing_cache" / "ai_timing"

    fixed = app_paths.repair_legacy_appdata_path(str(stale))

    assert fixed is not None
    assert Path(fixed) == content
    assert Path(fixed).is_dir()


def test_forward_slash_stale_path_is_repaired(tmp_path: Path) -> None:
    """真实事故里的存量值是正斜杠形式（文件对话框原样落盘），必须照样能修。"""

    content = _current_root() / "ai_models" / "wav2vec2"
    content.mkdir(parents=True)
    stale = (_legacy_root("Karaoke Studio") / "ai_models" / "wav2vec2").as_posix()

    fixed = app_paths.repair_legacy_appdata_path(stale)

    assert fixed is not None
    assert Path(fixed) == content


def test_existing_path_is_never_touched(tmp_path: Path) -> None:
    """旧目录还在（用户并行保留旧版在用）→ 原值保留，绝不动。"""

    old = _legacy_root("Karaoke Studio") / "lyrics_timing_cache" / "ai_timing"
    old.mkdir(parents=True)

    assert app_paths.repair_legacy_appdata_path(str(old)) is None


def test_no_repair_when_candidate_content_missing(tmp_path: Path) -> None:
    """候选位置都没有对应内容 → 保留原值，不凭空造路径。"""

    stale = _legacy_root("Karaoke Studio") / "lyrics_timing_cache" / "ai_timing"

    assert app_paths.repair_legacy_appdata_path(str(stale)) is None


def test_user_path_with_same_folder_name_is_untouched(tmp_path: Path) -> None:
    r"""应用数据目录**之外**的同名目录（用户的 D:\Videos\Karaoke Studio\x.mp4）
    与本应用无关，即使当前名下存在同名尾巴也不许改写。"""

    new_dir = tmp_path / APP_NAME
    new_dir.mkdir(parents=True)
    (new_dir / "song.mp4").write_text("new", encoding="utf-8")
    decoy = tmp_path / "Videos" / "Karaoke Studio" / "song.mp4"

    assert app_paths.repair_legacy_appdata_path(str(decoy)) is None


def test_non_path_and_relative_values_are_ignored() -> None:
    """相对路径、空串、URL、模型 ID 这类值不是应用数据目录路径，原样跳过。"""

    for raw in (
        "",
        "   ",
        "wav2vec2",
        "pymss\\runtime\\python.exe",
        "https://hf-mirror.com",
    ):
        assert app_paths.repair_legacy_appdata_path(raw) is None, raw


def test_runtime_python_file_path_is_repaired(tmp_path: Path) -> None:
    """文件型路径（Runtime python.exe）与目录型同样自愈。"""

    exe = _current_root() / "ai_runtime" / "Scripts" / "python.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"stub")
    stale = _legacy_root("Karaoke Studio Dev") / "ai_runtime" / "Scripts" / "python.exe"

    fixed = app_paths.repair_legacy_appdata_path(str(stale))

    assert fixed is not None
    assert Path(fixed) == exe


def test_falls_back_to_sibling_profile(tmp_path: Path) -> None:
    """内容只在另一档位（正式/Dev）存在时也要接得住——用户可能在两档间搬过数据。"""

    content = _current_root(APP_NAME) / "ai_models" / "wav2vec2"
    content.mkdir(parents=True)
    stale = _current_root(f"{APP_NAME} Dev") / "ai_models" / "wav2vec2"

    fixed = app_paths.repair_legacy_appdata_path(str(stale))

    assert fixed is not None
    assert Path(fixed) == content


def test_dev_profile_stale_legacy_dev_path_repaired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """源码调试档（…Dev）的旧路径优先修到同名 Dev 目录。"""

    monkeypatch.setenv(app_paths.SETTINGS_APP_NAME_ENV, f"{APP_NAME} Dev")
    content = _current_root(f"{APP_NAME} Dev") / "lyrics_timing_cache" / "ai_timing"
    content.mkdir(parents=True)
    stale = _legacy_root("Karaoke Studio Dev") / "lyrics_timing_cache" / "ai_timing"

    fixed = app_paths.repair_legacy_appdata_path(str(stale))

    assert fixed is not None
    assert Path(fixed) == content


def _write_settings(payload: dict) -> Path:
    path = app_paths.get_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


def test_load_app_settings_repairs_and_persists(tmp_path: Path) -> None:
    """端到端：读取时内存值修复、settings.json 同步落盘、未知键不丢。"""

    content = _current_root() / "lyrics_timing_cache" / "ai_timing"
    content.mkdir(parents=True)
    stale = (_legacy_root("Karaoke Studio") / "lyrics_timing_cache" / "ai_timing").as_posix()
    path = _write_settings(
        {
            "future_field": {"note": "向前兼容的未知键"},
            "lyrics_timing": {
                "ai_timing": {
                    "ai_cache_root": stale,
                    "provider": "wav2vec2",
                }
            },
        }
    )

    loaded = load_app_settings()

    assert Path(loaded.lyrics_timing["ai_timing"]["ai_cache_root"]) == content
    repaired_payload = json.loads(path.read_text(encoding="utf-8"))
    assert (
        Path(repaired_payload["lyrics_timing"]["ai_timing"]["ai_cache_root"]) == content
    )
    assert repaired_payload["future_field"] == {"note": "向前兼容的未知键"}


def test_load_app_settings_repair_is_idempotent(tmp_path: Path) -> None:
    """第二次读取不得再改文件（已修好的路径存在，自愈应无动作）。"""

    content = _current_root() / "ai_models"
    content.mkdir(parents=True)
    stale = (_legacy_root("Karaoke Studio") / "ai_models").as_posix()
    path = _write_settings({"lyrics_timing": {"ai_timing": {"model_root": stale}}})

    load_app_settings()
    first = path.read_bytes()

    loaded_again = load_app_settings()

    assert path.read_bytes() == first
    assert Path(loaded_again.lyrics_timing["ai_timing"]["model_root"]) == content


def test_load_app_settings_without_stale_paths_does_not_write(tmp_path: Path) -> None:
    """没有需要修的值时 load 保持只读（文件内容一个字节都不动）。"""

    path = _write_settings({"ffmpeg_dir": "D:/tools/ffmpeg"})
    before = path.read_bytes()

    load_app_settings()

    assert path.read_bytes() == before
