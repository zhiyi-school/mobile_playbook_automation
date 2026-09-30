"""
Services for listing, inspecting and resolving icons of uploaded app builds.
"""

from __future__ import annotations

import logging
from pathlib import Path

from mobile_playbook.api.models import Platform
from mobile_playbook.artifact_store.extraction import describe_artifact
from mobile_playbook.artifact_store.resolver import app_icon_file
from mobile_playbook.platforms.android.apk_tools import inspect_apk_metadata
from mobile_playbook.platforms.ios.artifacts.intake_ipa import list_intake_ipas
from mobile_playbook.platforms.ios.ipa.plist_utils import inspect_ipa_metadata

logger = logging.getLogger(__name__)


# Return the current iOS and Android intake directories.
def intake_dirs() -> dict[Platform, Path]:
    from mobile_playbook.common.storage_paths import android_intake_dir, ios_intake_dir

    return {"ios": ios_intake_dir(), "android": android_intake_dir()}


INTAKE_DIRS: dict[Platform, Path] = intake_dirs()
ARTIFACT_SUFFIXES: dict[Platform, str] = {"ios": ".ipa", "android": ".apk"}

ICON_CACHE_CONTROL = "private, max-age=300"


# Read an uploaded build's metadata and artifact reference, or return the inspection error.
def inspect_uploaded_artifact(platform: Platform, path: Path) -> dict:
    logger.debug("api: inspecting uploaded %s artifact %s.", platform, path)
    try:
        metadata = inspect_apk_metadata(path) if platform == "android" else inspect_ipa_metadata(path)
    except Exception as exc:
        logger.debug("api: metadata inspection failed for %s.", path, exc_info=True)
        return {"error": str(exc)}
    logger.debug("api: uploaded artifact %s metadata keys %s.", path, sorted(metadata))
    return {**metadata, **_artifact_reference(platform, path)}


# Return an uploaded build's checksum and icon state, or an empty dict so the upload never fails.
def _artifact_reference(platform: Platform, path: Path) -> dict:
    try:
        described = describe_artifact(platform, path)
    except Exception:
        logger.debug("api: artifact store could not describe %s; omitting reference.", path, exc_info=True)
        return {}
    logger.debug("api: artifact %s described as %s.", path, described.get("artifact_id"))
    return {key: described.get(key) for key in ("artifact_id", "sha256", "platform", "icon")}


# Return the icon file and artifact id for a configured app, or None when there is none.
def app_icon_file_path(platform: Platform, app_id: str) -> tuple[Path, str] | None:
    resolved = app_icon_file(platform, app_id)
    logger.debug("api: icon for %s app %s resolved=%s.", platform, app_id, resolved is not None)
    return resolved


# List the platform's intake builds, newest first for Android.
def list_artifacts(platform: Platform) -> list[dict]:
    directory = intake_dirs()[platform]
    logger.debug("api: listing %s artifacts in %s.", platform, directory)
    if platform == "ios":
        return [build.as_dict() for build in list_intake_ipas(directory)]
    if not directory.is_dir():
        logger.debug("api: intake directory %s does not exist; no artifacts.", directory)
        return []
    files = sorted(directory.glob(f"*{ARTIFACT_SUFFIXES[platform]}"), key=lambda p: p.stat().st_mtime, reverse=True)
    logger.debug("api: found %d %s file(s) in %s.", len(files), ARTIFACT_SUFFIXES[platform], directory)
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
