"""
Resolves configured apps to their on-disk builds and derived icon references.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mobile_playbook.artifact_store import store
from mobile_playbook.artifact_store.extraction import (
    STATUS_UNAVAILABLE,
    IconExtraction,
    extract_icon,
)

logger = logging.getLogger(__name__)


# Returns the Android intake directory.
def _android_intake_dir() -> Path:
    from mobile_playbook.common.storage_paths import android_intake_dir

    return android_intake_dir()


# Returns the directory holding APKs acquired by the Android repackaging workflow.
def _android_workflow_apk_dir() -> Path:
    from mobile_playbook.common.storage_paths import android_work_dir

    return android_work_dir() / "repackaging"


ANDROID_INTAKE_DIR = _android_intake_dir()
ANDROID_WORKFLOW_APK_DIR = _android_workflow_apk_dir()
REASON_UNKNOWN_APP = "unknown_app"
_LOCAL_IPA_SOURCES = {"local_ipa", "ci_artifact", "vendor_ipa", "xcode_archive_export"}


@dataclass(frozen=True)
class AppArtifact:
    platform: str
    app_id: str
    path: Path


# Returns the configured app entry for a platform and id, or None if absent or the config is unreadable.
def _app_entry(platform: str, app_id: str) -> dict[str, Any] | None:
    from mobile_playbook.api import config_editing

    try:
        apps = config_editing.list_ios_apps() if platform == "ios" else config_editing.list_android_apps()
    except Exception:
        logger.debug("artifact store: listing %s apps failed.", platform, exc_info=True)
        logger.warning("Config for platform %s could not be read while resolving an icon.", platform)
        return None
    entry = next((entry for entry in apps if entry.get("id") == app_id), None)
    logger.debug("artifact store: %s app %s configured=%s.", platform, app_id, entry is not None)
    return entry


# Returns the expanded path if it names an existing file, else None.
def _existing(path_value: Any) -> Path | None:
    if not path_value:
        return None
    path = Path(str(path_value)).expanduser()
    return path if path.is_file() else None


# Returns the on-disk IPA for an iOS app from intake or a local source, or None.
def _resolve_ios(app: dict[str, Any]) -> Path | None:
    from mobile_playbook.platforms.ios.artifacts.intake_ipa import intake_dir_for, resolve_intake_ipa

    artifact = app.get("artifact") or {}
    source = artifact.get("source") or ""

    if source == "intake_ipa":
        resolution = resolve_intake_ipa(
            bundle_id=artifact.get("expected_bundle_id") or app.get("bundle_id") or None,
            app_name=app.get("name"),
            intake_dir=intake_dir_for(artifact),
        )
        logger.debug(
            "artifact store: intake IPA for %r -> %s (ambiguous=%s).",
            app.get("id"),
            resolution.match.path if resolution.match else None,
            resolution.ambiguous,
        )
        return resolution.match.path if resolution.match else None

    if source in _LOCAL_IPA_SOURCES:
        path = _existing(artifact.get("ipa") or artifact.get("path"))
        logger.debug("artifact store: %s IPA for %r -> %s.", source, app.get("id"), path)
        return path
    logger.debug("artifact store: iOS app %r source %r has no local build.", app.get("id"), source)
    return None


# Returns the app's configured, acquired or name-matched intake APK, or None.
def _resolve_android(app: dict[str, Any]) -> Path | None:
    from mobile_playbook.platforms.ios.artifacts.intake_ipa import normalize_app_name

    artifact = app.get("artifact") or {}
    configured = _existing(artifact.get("apk") or artifact.get("path"))
    if configured is not None:
        logger.debug("artifact store: Android app %r uses configured APK %s.", app.get("id"), configured)
        return configured

    package_name = app.get("package_name") or ""
    if package_name:
        acquired = ANDROID_WORKFLOW_APK_DIR / package_name.replace(".", "_") / "original" / "base.apk"
        if acquired.is_file():
            logger.debug("artifact store: Android app %r uses acquired APK %s.", app.get("id"), acquired)
            return acquired
        logger.debug("artifact store: no acquired APK at %s.", acquired)

    intake_dir = Path(artifact.get("intake_dir") or _android_intake_dir()).expanduser()
    if not intake_dir.is_dir():
        logger.debug("artifact store: Android intake directory %s missing.", intake_dir)
        return None
    wanted = {normalize_app_name(package_name), normalize_app_name(app.get("name"))} - {""}
    for candidate in sorted(intake_dir.glob("*.apk")):
        if normalize_app_name(candidate.stem) in wanted:
            logger.debug("artifact store: Android app %r matched intake APK %s.", app.get("id"), candidate)
            return candidate
    logger.debug("artifact store: no intake APK in %s matches %s.", intake_dir, sorted(wanted))
    return None


# Returns the build a configured app would be tested from, if one is on disk.
def resolve_app_artifact(platform: str, app_id: str) -> AppArtifact | None:
    app = _app_entry(platform, app_id)
    if app is None:
        return None
    path = _resolve_ios(app) if platform == "ios" else _resolve_android(app)
    return AppArtifact(platform=platform, app_id=app_id, path=path) if path else None


# Returns the icon state for a configured app, extracting on first use and then serving from the store.
def app_icon(platform: str, app_id: str, force: bool = False) -> IconExtraction:
    if _app_entry(platform, app_id) is None:
        logger.debug("artifact store: icon unavailable; %s app %s is not configured.", platform, app_id)
        return IconExtraction(status=STATUS_UNAVAILABLE, reason=REASON_UNKNOWN_APP)
    artifact = resolve_app_artifact(platform, app_id)
    if artifact is None:
        logger.debug("artifact store: icon unavailable; no build on disk for %s app %s.", platform, app_id)
        return IconExtraction(status=STATUS_UNAVAILABLE, reason="no_artifact_available")
    logger.debug(
        "artifact store: extracting icon for %s app %s from %s (force=%s).", platform, app_id, artifact.path, force
    )
    return extract_icon(platform, artifact.path, force=force)


# Returns the icon file and artifact id for the icon endpoint, or None when there is nothing to serve.
def app_icon_file(platform: str, app_id: str) -> tuple[Path, str] | None:
    extraction = app_icon(platform, app_id)
    if not extraction.available or not extraction.artifact_id:
        logger.debug("artifact store: no icon file for %s app %s (status %s).", platform, app_id, extraction.status)
        return None
    path = store.resolve_icon_ref(extraction.storage_ref)
    return (path, extraction.artifact_id) if path else None


# Returns the small icon reference the dashboard stores; never raises, and unavailable is a normal answer.
def app_icon_reference(platform: str, app_id: str, force: bool = False) -> dict[str, Any]:
    try:
        extraction = app_icon(platform, app_id, force=force)
    except Exception as exc:
        logger.debug("artifact store: icon reference for %s app %s failed.", platform, app_id, exc_info=True)
        logger.warning("Icon reference for app %r could not be resolved: %s", app_id, type(exc).__name__)
        return {"artifact_sha256": None, "icon_ref": None, "icon_extraction_status": "failed"}
    return {
        "artifact_sha256": extraction.artifact_id,
        "icon_ref": extraction.storage_ref,
        "icon_extraction_status": extraction.status,
    }


# Lists the configured app ids for a platform, or an empty list if the config is unreadable.
def configured_app_ids(platform: str) -> list[str]:
    from mobile_playbook.api import config_editing

    try:
        apps = config_editing.list_ios_apps() if platform == "ios" else config_editing.list_android_apps()
    except Exception:
        logger.debug("artifact store: listing %s app ids failed.", platform, exc_info=True)
        logger.warning("Config for platform %s could not be read.", platform)
        return []
    logger.debug("artifact store: %d configured %s app(s).", len(apps), platform)
    return [str(entry["id"]) for entry in apps if entry.get("id")]


# Returns the config fields artifact resolution needs from an app config object.
def _app_config_view(app_config: Any) -> dict[str, Any]:
    return {
        "id": getattr(app_config, "id", None),
        "name": getattr(app_config, "name", None),
        "bundle_id": getattr(app_config, "bundle_id", None),
        "package_name": getattr(app_config, "package_name", None),
        "artifact": getattr(app_config, "artifact", None) or {},
    }


# Returns the SHA-256 of the build a run is actually using, or None when there is none on disk.
def artifact_digest_for_app_config(platform: str, app_config: Any) -> str | None:
    view = _app_config_view(app_config)
    path = _resolve_ios(view) if platform == "ios" else _resolve_android(view)
    if path is None:
        logger.debug("artifact store: no build on disk to digest for %s app %r.", platform, view["id"])
        return None
    try:
        return store.artifact_digest(path)
    except OSError:
        logger.debug("artifact store: digest of %s failed.", path, exc_info=True)
        return None


# Digests the run's build and derives its icon now, so a later sync cannot pick a newer build.
def prepare_icon_for_app_config(platform: str, app_config: Any) -> str | None:
    digest = artifact_digest_for_app_config(platform, app_config)
    if digest is None:
        return None
    view = _app_config_view(app_config)
    path = _resolve_ios(view) if platform == "ios" else _resolve_android(view)
    logger.debug(
        "artifact store: preparing icon for %s app %r from %s (digest %s).", platform, view["id"], path, digest[:12]
    )
    if path is not None:
        extract_icon(platform, path, digest)
    return digest


# Returns the stored icon reference for one exact build, or None when nothing was derived from it.
def icon_reference_for_artifact(artifact_sha256: str) -> dict[str, Any] | None:
    if not store.is_artifact_id(artifact_sha256):
        logger.debug("artifact store: %r is not an artifact id.", artifact_sha256)
        return None
    if not store.icon_path(artifact_sha256).is_file():
        logger.debug("artifact store: no stored icon for artifact %s.", artifact_sha256[:12])
        return None
    logger.debug("artifact store: stored icon found for artifact %s.", artifact_sha256[:12])
    return {
        "artifact_sha256": artifact_sha256,
        "icon_ref": store.icon_ref(artifact_sha256),
        "icon_extraction_status": "available",
    }


# Returns the configured display name of an app, or None.
def app_display_name(platform: str, app_id: str) -> str | None:
    entry = _app_entry(platform, app_id)
    name = (entry or {}).get("name")
    return str(name) if name else None
