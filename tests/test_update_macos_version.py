"""``scripts/update_macos_version.py`` 的版本派生与 Info.plist 写入。

macOS 的 ``CFBundleVersion`` 只认三段数字，SUG 修订号（APP_VERSION 第四段）
打包进第三段（``patch * 1000 + revision``），必须保证与下个 patch 版本之间
的排序关系不翻转。
"""

from __future__ import annotations

import plistlib

import pytest

from scripts import update_macos_version as umv
from scripts.update_macos_version import macos_versions, update_version_metadata


@pytest.mark.parametrize(
    ("version", "display", "build"),
    [
        ("4.3.6", "4.3.6", "4.3.6000"),
        ("4.3.6.5", "4.3.6", "4.3.6005"),
        ("4.3.6.999", "4.3.6", "4.3.6999"),
        ("10.20.30", "10.20.30", "10.20.30000"),
    ],
)
def test_macos_versions_packs_revision_into_patch(version, display, build):
    assert macos_versions(version) == (display, build)


@pytest.mark.parametrize(
    "version",
    ["4.3", "4.3.6.1000", "v4.3.6", "4.3.x", "4.3.6.7.8", "", "4.3.6.0.1"],
)
def test_macos_versions_rejects_invalid_versions(version):
    with pytest.raises(ValueError):
        macos_versions(version)


def test_revision_packing_keeps_ordering_against_next_patch():
    assert macos_versions("4.3.6.999")[1] < macos_versions("4.3.7")[1]


@pytest.mark.parametrize("quote", ['"', "'"])
def test_read_app_version_parses_config_quotes(tmp_path, monkeypatch, quote):
    config = tmp_path / "config.py"
    config.write_text(f"APP_VERSION = {quote}5.6.7.2{quote}\n", encoding="utf-8")
    monkeypatch.setattr(umv, "VERSION_FILE", config)
    assert umv.read_app_version() == "5.6.7.2"


def test_read_app_version_matches_real_config():
    from krok_helper.config import APP_VERSION

    assert umv.read_app_version() == APP_VERSION


def test_update_version_metadata_writes_plist(tmp_path):
    plist = tmp_path / "Info.plist"
    plist.write_bytes(
        plistlib.dumps({"CFBundleShortVersionString": "0.0.0", "CFBundleVersion": "0"})
    )

    display, build = update_version_metadata(plist, "4.3.6.5")

    assert (display, build) == ("4.3.6", "4.3.6005")
    metadata = plistlib.loads(plist.read_bytes())
    assert metadata["CFBundleShortVersionString"] == "4.3.6"
    assert metadata["CFBundleVersion"] == "4.3.6005"
