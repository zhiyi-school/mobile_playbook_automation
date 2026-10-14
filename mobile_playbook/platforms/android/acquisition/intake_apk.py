"""
Finds an app's installable APK in the intake folder by the package name read from each APK.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from mobile_playbook.platforms.android.apk_tools import inspect_apk_metadata

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IntakeApk:
    path: Path
    package_name: str
    display_name: str | None
    version_name: str | None
    version_code: str | None
    modified_at: float

    # Return the APK as a JSON-compatible dict for the artifacts listing.
    def as_dict(self) -> dict:
        return {
            "file": self.path.name,
            "path": str(self.path),
            "bundle_id": self.package_name,
            "display_name": self.display_name,
            "version": self.version_name,
            "modified_at": self.modified_at,
        }


# Return the storage-configured Android intake directory.
def _default_intake_dir() -> Path:
    from mobile_playbook.common.storage_paths import android_intake_dir

    return android_intake_dir()


_scan_cache: dict[str, tuple[tuple, list[IntakeApk]]] = {}


# Return the name, mtime and size of each APK in a directory as a cache signature.
def _dir_signature(directory: Path) -> tuple:
    entries = []
    for path in sorted(directory.glob("*.apk")):
        try:
            stat = path.stat()
        except OSError as exc:
            logger.debug("android intake: cannot stat %s for signature: %s", path, exc, exc_info=True)
            continue
        entries.append((path.name, stat.st_mtime, stat.st_size))
    return tuple(entries)


# Order APKs highest version code first, then newest file.
def _apk_order(apk: IntakeApk) -> tuple:
    code = apk.version_code or ""
    return (int(code) if code.isdigit() else -1, apk.modified_at)


# Return every APK in the intake directory whose package an SDK tool can read, highest version first.
def list_intake_apks(intake_dir: Path | None = None) -> list[IntakeApk]:
    directory = intake_dir or _default_intake_dir()
    if not directory.is_dir():
        logger.debug("android intake: %s is not a directory; no APKs", directory)
        return []

    # Cached on a stat-only signature: reading each APK's package runs an SDK tool per file.
    signature = _dir_signature(directory)
    cached = _scan_cache.get(str(directory))
    if cached is not None and cached[0] == signature:
        logger.debug("android intake: scan cache hit for %s (%s APKs)", directory, len(cached[1]))
        return cached[1]

    logger.debug("android intake: scanning %s (%s apk files)", directory, len(signature))
    apks: list[IntakeApk] = []
    for candidate in sorted(directory.glob("*.apk")):
        try:
            metadata = inspect_apk_metadata(candidate)
        except Exception as exc:
            logger.warning("Skipping intake APK %s: its package could not be read (%s).", candidate.name, exc)
            continue
        if not metadata.get("package_name"):
            logger.debug("android intake: skipping %s; no package name in its manifest", candidate)
            continue
        apks.append(
            IntakeApk(
                path=candidate,
                package_name=metadata["package_name"],
                display_name=metadata.get("display_name"),
                version_name=metadata.get("version_name"),
                version_code=metadata.get("version_code"),
                modified_at=candidate.stat().st_mtime,
            )
        )
    apks.sort(key=_apk_order, reverse=True)
    _scan_cache[str(directory)] = (signature, apks)
    logger.debug("android intake: scan of %s found %s APKs", directory, len(apks))
    return apks


# Return the highest-version intake APK for a package, or None.
def find_intake_apk(package: str, intake_dir: Path | None = None) -> IntakeApk | None:
    match = next((apk for apk in list_intake_apks(intake_dir) if apk.package_name == package), None)
    logger.debug("android intake: %s -> %s", package, match.path if match else None)
    return match
