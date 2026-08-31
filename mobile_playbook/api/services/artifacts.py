from __future__ import annotations

from pathlib import Path

from mobile_playbook.api.models import Platform
from mobile_playbook.platforms.android.apk_tools import inspect_apk_metadata
from mobile_playbook.platforms.ios.artifacts.intake_ipa import list_intake_ipas
from mobile_playbook.platforms.ios.ipa.plist_utils import inspect_ipa_metadata

INTAKE_DIRS: dict[Platform, Path] = {"ios": Path("intake/ios/ipas"), "android": Path("intake/android/apks")}
ARTIFACT_SUFFIXES: dict[Platform, str] = {"ios": ".ipa", "android": ".apk"}


def inspect_uploaded_artifact(platform: Platform, path: Path) -> dict:
    try:
        if platform == "android":
            return inspect_apk_metadata(path)
        return inspect_ipa_metadata(path)
    except Exception as exc:
        return {"error": str(exc)}


def list_artifacts(platform: Platform) -> list[dict]:
    directory = INTAKE_DIRS[platform]
    if platform == "ios":
        return [build.as_dict() for build in list_intake_ipas(directory)]
    if not directory.is_dir():
        return []
    files = sorted(directory.glob(f"*{ARTIFACT_SUFFIXES[platform]}"), key=lambda p: p.stat().st_mtime, reverse=True)
    return [
        {
            "file": path.name,
            "path": str(path),
            "bundle_id": None,
            "display_name": None,
            "version": None,
            "modified_at": path.stat().st_mtime,
        }
        for path in files
    ]
