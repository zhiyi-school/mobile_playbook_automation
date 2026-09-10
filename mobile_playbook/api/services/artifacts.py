from __future__ import annotations

from pathlib import Path

from mobile_playbook.api.models import Platform
from mobile_playbook.artifact_store.extraction import describe_artifact
from mobile_playbook.artifact_store.resolver import app_icon_file
from mobile_playbook.platforms.android.apk_tools import inspect_apk_metadata
from mobile_playbook.platforms.ios.artifacts.intake_ipa import list_intake_ipas
from mobile_playbook.platforms.ios.ipa.plist_utils import inspect_ipa_metadata

def intake_dirs() -> dict[Platform, Path]:
    from mobile_playbook.storage import android_intake_dir, ios_intake_dir

    return {"ios": ios_intake_dir(), "android": android_intake_dir()}


INTAKE_DIRS: dict[Platform, Path] = intake_dirs()
ARTIFACT_SUFFIXES: dict[Platform, str] = {"ios": ".ipa", "android": ".apk"}

ICON_CACHE_CONTROL = "private, max-age=300"


def inspect_uploaded_artifact(platform: Platform, path: Path) -> dict:
    try:
        metadata = inspect_apk_metadata(path) if platform == "android" else inspect_ipa_metadata(path)
    except Exception as exc:
        return {"error": str(exc)}
    return {**metadata, **_artifact_reference(platform, path)}


def _artifact_reference(platform: Platform, path: Path) -> dict:
    """Checksum and icon state for an uploaded build. Never fails the upload."""
    try:
        described = describe_artifact(platform, path)
    except Exception:
        return {}
    return {key: described.get(key) for key in ("artifact_id", "sha256", "platform", "icon")}


def app_icon_file_path(platform: Platform, app_id: str) -> tuple[Path, str] | None:
    """(file, artifact_id) for a configured app's icon, or `None` when there is none to serve."""
    return app_icon_file(platform, app_id)


def list_artifacts(platform: Platform) -> list[dict]:
    directory = intake_dirs()[platform]
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
