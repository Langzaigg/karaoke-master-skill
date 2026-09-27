"""改名后仍必须成立的发布契约。

这些不是普通的字符串断言，而是**防止后人「顺手清理」**的护栏。执行自动更新的是
用户机器上的旧版代码，新版怎么写都救不了它 —— 一旦下面任何一条被破坏，存量用户
要么彻底断更，要么更新完拉不起来。详见 docs/auto_update.md §8。
"""

from __future__ import annotations

import scripts.build_parts as build_parts
from krok_helper.updater.installer import DEFAULT_APP_EXE_NAME, LEGACY_APP_EXE_NAME
from krok_helper.updater.worker import current_asset_name


def test_release_asset_names_keep_the_pre_rename_prefix() -> None:
    """资产名不可改：旧 worker 硬编码全量 zip 名，并从 zip 名派生 manifest 名。

    ``pick_primary_asset`` 找不到精确名时会回退成「文件名含 windows 且以 .zip
    结尾」，而这条规则同时匹配 ``-app.zip`` 与 ``-runtime.zip`` —— 改名有让旧客户端
    把分包当全量包、装出不可启动安装的风险。
    """

    assert build_parts.ASSET_BASE == "KaraokeStudio-windows"
    assert current_asset_name() in {
        "KaraokeStudio-windows.zip",
        "KaraokeStudio-macos.zip",
    }


def test_legacy_exe_shipping_cuts_off_after_4_3_0() -> None:
    """旧名副本只随 ≤4.3.0 的版本分发，其后（含 4 段 4.3.0.1）仅打 Lin-K Lyrics.exe。

    迁移机制（固定传新名 + 启动后清理）自 4.2.8.9（2026-09-14）起持续为最新版，
    活跃安装已收敛到新名会话；仍以旧名会话停留在 ≤4.2.8.8 的存量会断更，只能
    手动重下——release body 头部的手动更新提示（scripts/release.py）就是给这批
    人看的。详见 docs/auto_update.md §8.1。
    """

    assert LEGACY_APP_EXE_NAME == "Karaoke Studio.exe"
    assert DEFAULT_APP_EXE_NAME != LEGACY_APP_EXE_NAME
    assert build_parts.LEGACY_APP_EXE_NAME == LEGACY_APP_EXE_NAME
    assert build_parts.LAST_LEGACY_APP_VERSION == (4, 3, 0)
    for version in ("4.2.8.8", "4.2.8.12", "4.2.9", "4.2.9.1", "4.3.0"):
        assert build_parts.ship_legacy_app_exe(version) is True, version
    for version in ("4.3.0.1", "4.3.1", "4.4.0"):
        assert build_parts.ship_legacy_app_exe(version) is False, version


def test_app_targets_follow_the_legacy_gate_for_the_current_version() -> None:
    """当前仓库版本打出的 APP_TARGETS 必须与版本闸一致，bump 过 4.3.0 后自动翻转。

    迁移期还要求旧名副本进 app part targets：增量更新的 orphan cleanup 会删掉
    「本地 manifest 有、新 manifest 没有」的文件，漏了它旧客户端更新完就重启
    不起来；停发后则必须不在（manifest targets 有而本地缺文件会让 part 校验失败）。
    """

    assert build_parts.APP_EXE_NAME in build_parts.APP_TARGETS
    assert build_parts.SHIP_LEGACY_APP_EXE == build_parts.ship_legacy_app_exe()
    assert (build_parts.LEGACY_APP_EXE_NAME in build_parts.APP_TARGETS) is (
        build_parts.SHIP_LEGACY_APP_EXE
    )


def test_release_notes_cutoff_matches_the_packaging_gate() -> None:
    """手动更新提示的截止版本必须与打包停发闸一致，改一处漏一处会让提示与包内容错位。"""

    import importlib.util
    from pathlib import Path

    script = Path(__file__).resolve().parents[1] / "scripts" / "release.py"
    spec = importlib.util.spec_from_file_location("ks_release_cutoff", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    cutoff = tuple(int(part) for part in module.LEGACY_APP_EXE_LAST_VERSION.split("."))
    assert cutoff == build_parts.LAST_LEGACY_APP_VERSION
    # 提示从首个无旧名版本起生效，与打包闸互补（4.3.0 仍带旧名副本 → 不提示）。
    assert module._needs_manual_update_notice("4.3.0") is False
    assert module._needs_manual_update_notice("4.3.0.1") is True
    assert module._needs_manual_update_notice("4.3.1") is True


def test_onedir_layout_names_are_unchanged() -> None:
    """``_internal/`` 布局与更新器文件名同样被存量客户端硬编码。"""

    from krok_helper.updater.installer import (
        LOCAL_MANIFEST_FILENAME,
        TMP_DIR_NAME,
        UPDATER_EXE_NAME,
    )

    assert UPDATER_EXE_NAME == "Updater.exe"
    assert LOCAL_MANIFEST_FILENAME == ".installed_manifest.json"
    # 三份副本（installer / updater_app / separation.runtime 的目的地校验）必须一致
    assert TMP_DIR_NAME == "KaraokeStudioUpdater"


def test_full_update_payload_names_match_installer() -> None:
    """全量回退路径的必备根目录负载命名必须与 installer / build_parts 口径一致。

    2026-09 事故根因之一：全量 ``_apply_workbench_update`` 的回写清单与包内容
    脱节，sidecar 与另一份主程序名被静默跳过（详见
    tests/test_workbench_updater_lock_guard.py 的回归测试）。
    """

    from krok_helper.updater_app.main import (
        LEGACY_APP_EXE_NAME as UPDATER_APP_LEGACY,
        NATIVE_RENDERER_EXE_NAME as UPDATER_APP_SIDECAR,
        PRIMARY_APP_EXE_NAME,
    )
    from krok_helper.updater.installer import LEGACY_APP_EXE_NAME

    assert UPDATER_APP_LEGACY == LEGACY_APP_EXE_NAME
    assert PRIMARY_APP_EXE_NAME == DEFAULT_APP_EXE_NAME
    assert UPDATER_APP_SIDECAR == build_parts.NATIVE_RENDERER_EXE_NAME
    assert build_parts.NATIVE_RENDERER_EXE_NAME in build_parts.APP_TARGETS


def test_updater_sessions_always_use_the_canonical_exe_name(
    tmp_path, monkeypatch
) -> None:
    """新版主程序无论以哪个文件名启动，更新会话都按新名走（2026-09 迁移机制）。

    旧名快捷方式启动的用户也会传 ``Lin-K Lyrics.exe`` 给 Updater：按新名校验/
    回写/重启，并触发旧名副本清理（docs/auto_update.md §8.1）。回到「传实际
    启动名」的旧逻辑会让旧名安装永远收敛不到新名，停发旧名副本时全部断更。
    """
    import sys

    from krok_helper.updater import installer

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(
        sys, "executable", str(tmp_path / LEGACY_APP_EXE_NAME), raising=False
    )
    assert installer.find_app_exe_name() == DEFAULT_APP_EXE_NAME


def test_updater_app_names_migration_constants_match_installer() -> None:
    """清理旧名副本用的双名常量必须与 installer 口径一致，改一处漏一处会误删/漏删。"""

    from krok_helper.updater.installer import LEGACY_APP_EXE_NAME
    from krok_helper.updater_app.main import (
        LEGACY_APP_EXE_NAME as UPDATER_APP_LEGACY,
        PRIMARY_APP_EXE_NAME,
    )

    assert UPDATER_APP_LEGACY == LEGACY_APP_EXE_NAME
    assert PRIMARY_APP_EXE_NAME == DEFAULT_APP_EXE_NAME


def test_updater_temp_dir_name_is_consistent_across_copies() -> None:
    """同一个临时目录名散在三处，改一处漏两处会让更新交接直接错位。"""

    from krok_helper.updater.installer import TMP_DIR_NAME

    runtime_source = (
        __import__("krok_helper.audio_processing.separation.runtime", fromlist=["runtime"])
    )
    import inspect

    assert TMP_DIR_NAME in inspect.getsource(runtime_source.preflight_install_destination)
