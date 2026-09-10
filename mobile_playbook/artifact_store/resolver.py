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

def _android_intake_dir() -> Path:
    from mobile_playbook.storage import android_intake_dir

    return android_intake_dir()


def _android_workflow_apk_dir() -> Path:
    from mobile_playbook.storage import android_work_dir

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


def _app_entry(platform: str, app_id: str) -> dict[str, Any] | None:
    from mobile_playbook.api import config_editor

    try:
        apps = config_editor.list_ios_apps() if platform == "ios" else config_editor.list_android_apps()
    except Exception:
        logger.warning("Config for platform %s could not be read while resolving an icon.", platform)
        return None
    return next((entry for entry in apps if entry.get("id") == app_id), None)


def _existing(path_value: Any) -> Path | None:
    if not path_value:
        return None
    path = Path(str(path_value)).expanduser()
    return path if path.is_file() else None


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
        return resolution.match.path if resolution.match else None

    if source in _LOCAL_IPA_SOURCES:
        return _existing(artifact.get("ipa") or artifact.get("path"))
    return None


def _resolve_android(app: dict[str, Any]) -> Path | None:
    from mobile_playbook.platforms.ios.artifacts.intake_ipa import normalize_app_name

    artifact = app.get("artifact") or {}
    configured = _existing(artifact.get("apk") or artifact.get("path"))
    if configured is not None:
        return configured

    package_name = app.get("package_name") or ""
    if package_name:
        acquired = ANDROID_WORKFLOW_APK_DIR / package_name.replace(".", "_") / "original" / "base.apk"
        if acquired.is_file():
            return acquired

    intake_dir = Path(artifact.get("intake_dir") or _android_intake_dir()).expanduser()
    if not intake_dir.is_dir():
        return None
    wanted = {normalize_app_name(package_name), normalize_app_name(app.get("name"))} - {""}
    for candidate in sorted(intake_dir.glob("*.apk")):
        if normalize_app_name(candidate.stem) in wanted:
            return candidate
    return None


def resolve_app_artifact(platform: str, app_id: str) -> AppArtifact | None:
    """The build a configured app would be tested from, if one is on disk."""
    app = _app_entry(platform, app_id)
    if app is None:
        return None
    path = _resolve_ios(app) if platform == "ios" else _resolve_android(app)
    return AppArtifact(platform=platform, app_id=app_id, path=path) if path else None


def app_icon(platform: str, app_id: str, force: bool = False) -> IconExtraction:
    """Icon state for a configured app. Extracts on first use, then serves from the store."""
    if _app_entry(platform, app_id) is None:
        return IconExtraction(status=STATUS_UNAVAILABLE, reason=REASON_UNKNOWN_APP)
    artifact = resolve_app_artifact(platform, app_id)
    if artifact is None:
        return IconExtraction(status=STATUS_UNAVAILABLE, reason="no_artifact_available")
    return extract_icon(platform, artifact.path, force=force)


def app_icon_file(platform: str, app_id: str) -> tuple[Path, str] | None:
    """(file, artifact_id) for the icon endpoint, or `None` when there is nothing to serve."""
    extraction = app_icon(platform, app_id)
    if not extraction.available or not extraction.artifact_id:
        return None
    path = store.resolve_icon_ref(extraction.storage_ref)
    return (path, extraction.artifact_id) if path else None


def app_icon_reference(platform: str, app_id: str, force: bool = False) -> dict[str, Any]:
    """The small reference the dashboard stores. Never raises; unavailable is a normal answer."""
    try:
        extraction = app_icon(platform, app_id, force=force)
    except Exception as exc:
        logger.warning("Icon reference for app %r could not be resolved: %s", app_id, type(exc).__name__)
        return {"artifact_sha256": None, "icon_ref": None, "icon_extraction_status": "failed"}
    return {
        "artifact_sha256": extraction.artifact_id,
        "icon_ref": extraction.storage_ref,
        "icon_extraction_status": extraction.status,
    }


def configured_app_ids(platform: str) -> list[str]:
    from mobile_playbook.api import config_editor

    try:
        apps = config_editor.list_ios_apps() if platform == "ios" else config_editor.list_android_apps()
    except Exception:
        logger.warning("Config for platform %s could not be read.", platform)
        return []
    return [str(entry["id"]) for entry in apps if entry.get("id")]


def _app_config_view(app_config: Any) -> dict[str, Any]:
    return {
        "id": getattr(app_config, "id", None),
        "name": getattr(app_config, "name", None),
        "bundle_id": getattr(app_config, "bundle_id", None),
        "package_name": getattr(app_config, "package_name", None),
        "artifact": getattr(app_config, "artifact", None) or {},
    }


def artifact_digest_for_app_config(platform: str, app_config: Any) -> str | None:
    """SHA-256 of the build this run is actually using. `None` when there is none on disk."""
    view = _app_config_view(app_config)
    path = _resolve_ios(view) if platform == "ios" else _resolve_android(view)
    if path is None:
        return None
    try:
        return store.artifact_digest(path)
    except OSError:
        return None


def prepare_icon_for_app_config(platform: str, app_config: Any) -> str | None:
    """Digest the run's build and derive its icon now, so a later sync cannot pick a newer one."""
    digest = artifact_digest_for_app_config(platform, app_config)
    if digest is None:
        return None
    view = _app_config_view(app_config)
    path = _resolve_ios(view) if platform == "ios" else _resolve_android(view)
    if path is not None:
        extract_icon(platform, path, digest)
    return digest


def icon_reference_for_artifact(artifact_sha256: str) -> dict[str, Any] | None:
    """The stored reference for one exact build, or `None` when nothing was derived from it."""
    if not store.is_artifact_id(artifact_sha256):
        return None
    if not store.icon_path(artifact_sha256).is_file():
        return None
    return {
        "artifact_sha256": artifact_sha256,
        "icon_ref": store.icon_ref(artifact_sha256),
        "icon_extraction_status": "available",
    }


def app_display_name(platform: str, app_id: str) -> str | None:
    entry = _app_entry(platform, app_id)
    name = (entry or {}).get("name")
    return str(name) if name else None
