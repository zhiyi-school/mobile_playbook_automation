"""
Services for listing, inspecting and resolving icons of uploaded app builds.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from fastapi import HTTPException

from mobile_playbook.api.schemas import Platform
from mobile_playbook.artifact_store.icon_extraction import describe_artifact
from mobile_playbook.artifact_store.resolver import app_icon_file
from mobile_playbook.platforms.android.apk_tools import inspect_apk_metadata
from mobile_playbook.platforms.ios.acquisition.intake_ipa import (
    PINNABLE_IOS_SOURCES,
    intake_dir_for,
    list_intake_ipas,
    resolve_intake_ipa,
)
from mobile_playbook.platforms.ios.ipa.plist import inspect_ipa_metadata

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


# Return the icon's entity tag: its artifact plus a digest of the stored image, so a re-extracted icon revalidates.
def icon_etag(path: Path, artifact_id: str) -> str:
    return f'"{artifact_id}.{hashlib.sha256(path.read_bytes()).hexdigest()[:16]}"'


# List an iOS app's intake builds, highest version first, marking the one a run uses by default.
def list_app_builds(platform: Platform, app_id: str) -> dict:
    from mobile_playbook.api.config_editing import effective_ios_app

    if platform != "ios":
        logger.debug("api: builds requested for %s app %s; only iOS lists builds.", platform, app_id)
        raise HTTPException(status_code=422, detail="Only iOS apps have selectable builds; Android tests the installed app")
    app = effective_ios_app(app_id)
    if app is None:
        logger.debug("api: no iOS app %s; responding 404.", app_id)
        raise HTTPException(status_code=404, detail=f"Unknown app_id: {app_id}")
    artifact = app.get("artifact") or {}
    if artifact.get("source") not in PINNABLE_IOS_SOURCES:
        logger.debug("api: iOS app %s uses source %r; it has no intake builds.", app_id, artifact.get("source"))
        return {"builds": [], "default_version": None, "ambiguous": False}
    resolution = resolve_intake_ipa(
        bundle_id=artifact.get("expected_bundle_id") or app.get("bundle_id") or None,
        app_name=app.get("name"),
        intake_dir=intake_dir_for(artifact),
    )
    builds = [
        {
            "version": build.version,
            "build_number": build.build_number,
            "file": build.path.name,
            "modified_at": build.modified_at,
        }
        for build in resolution.candidates
    ]
    default = resolution.match.version if resolution.match else None
    logger.debug("api: iOS app %s has %d build(s); default version %s.", app_id, len(builds), default)
    return {"builds": builds, "default_version": default, "ambiguous": resolution.ambiguous}


# List the platform's intake builds, highest version first for iOS and newest first for Android.
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
