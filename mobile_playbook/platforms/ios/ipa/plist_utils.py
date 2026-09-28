from __future__ import annotations

import logging
import plistlib
import zipfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def read_plist(path: Path) -> dict[str, Any]:
    with Path(path).open("rb") as handle:
        data = plistlib.load(handle)
    logger.debug("ios plist: read %s (%s top-level keys)", path, len(data) if isinstance(data, dict) else type(data).__name__)
    return data


def write_plist(path: Path, data: dict[str, Any]) -> None:
    logger.debug("ios plist: writing %s (%s top-level keys)", path, len(data))
    with Path(path).open("wb") as handle:
        plistlib.dump(data, handle)


def read_info_plist(app_dir: Path) -> dict[str, Any]:
    return read_plist(Path(app_dir) / "Info.plist")


def write_info_plist(app_dir: Path, data: dict[str, Any]) -> None:
    write_plist(Path(app_dir) / "Info.plist", data)


def get_bundle_identifier(app_dir: Path) -> str | None:
    return read_info_plist(app_dir).get("CFBundleIdentifier")


def get_bundle_executable(app_dir: Path) -> str | None:
    return read_info_plist(app_dir).get("CFBundleExecutable")


def get_bundle_display_name(app_dir: Path) -> str | None:
    info = read_info_plist(app_dir)
    return info.get("CFBundleDisplayName") or info.get("CFBundleName")


def _payload_app_names(zf: zipfile.ZipFile) -> list[str]:
    apps: set[str] = set()
    for name in zf.namelist():
        parts = name.split("/")
        if len(parts) >= 2 and parts[0] == "Payload" and parts[1].endswith(".app"):
            apps.add(parts[1])
    return sorted(apps)


def inspect_ipa_metadata(ipa_path: Path) -> dict[str, Any]:
    logger.debug("ios plist: inspecting ipa %s", ipa_path)
    with zipfile.ZipFile(ipa_path) as zf:
        app_names = _payload_app_names(zf)
        logger.debug("ios plist: %s Payload apps %s", ipa_path, app_names)
        if len(app_names) != 1:
            raise ValueError(f"Expected exactly one Payload/*.app, found {len(app_names)}")
        info_name = f"Payload/{app_names[0]}/Info.plist"
        if info_name not in zf.namelist():
            raise ValueError("IPA is missing Payload/*.app/Info.plist")
        with zf.open(info_name) as handle:
            info = plistlib.load(handle)
        logger.debug(
            "ios plist: %s Info.plist bundle_id=%s executable=%s version=%s build=%s min_os=%s",
            ipa_path,
            info.get("CFBundleIdentifier"),
            info.get("CFBundleExecutable"),
            info.get("CFBundleShortVersionString"),
            info.get("CFBundleVersion"),
            info.get("MinimumOSVersion"),
        )
        return {
            "app_name": app_names[0],
            "bundle_id": info.get("CFBundleIdentifier"),
            "display_name": info.get("CFBundleDisplayName") or info.get("CFBundleName"),
            "executable_name": info.get("CFBundleExecutable"),
            "info_plist": info,
        }
