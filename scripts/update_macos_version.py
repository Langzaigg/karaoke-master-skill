"""Write macOS bundle versions derived from APP_VERSION."""

from __future__ import annotations

import argparse
import plistlib
import re
from pathlib import Path


VERSION_FILE = Path(__file__).resolve().parents[1] / "krok_helper" / "config.py"


def read_app_version() -> str:
    text = VERSION_FILE.read_text(encoding="utf-8")
    match = re.search(r"^APP_VERSION\s*=\s*['\"]([^'\"]+)['\"]", text, re.MULTILINE)
    if not match:
        raise ValueError(f"APP_VERSION was not found in {VERSION_FILE}")
    return match.group(1)


def macos_versions(version: str) -> tuple[str, str]:
    match = re.fullmatch(r"([0-9]+)\.([0-9]+)\.([0-9]+)(?:\.([0-9]+))?", version)
    if not match:
        raise ValueError(f"Invalid APP_VERSION: {version}")
    major, minor, patch = (int(part) for part in match.groups()[:3])
    revision = int(match.group(4) or 0)
    if revision >= 1000:
        raise ValueError("The fourth APP_VERSION component must be between 0 and 999.")

    display_version = f"{major}.{minor}.{patch}"
    # Pack the SUG revision into the third component to preserve version ordering.
    build_version = f"{major}.{minor}.{patch * 1000 + revision}"
    return display_version, build_version


def update_version_metadata(plist_path: Path, version: str) -> tuple[str, str]:
    display_version, build_version = macos_versions(version)
    with plist_path.open("rb") as stream:
        metadata = plistlib.load(stream)
    metadata["CFBundleShortVersionString"] = display_version
    metadata["CFBundleVersion"] = build_version
    with plist_path.open("wb") as stream:
        plistlib.dump(metadata, stream)
    return display_version, build_version


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plist_path", type=Path, help="Path to the app's Contents/Info.plist")
    args = parser.parse_args(argv)
    try:
        display_version, build_version = update_version_metadata(args.plist_path, read_app_version())
    except ValueError as exc:
        parser.error(str(exc))
    print(f"  Display version: {display_version}; build version: {build_version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
