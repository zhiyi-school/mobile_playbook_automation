"""
Find installable standalone APKs and verified, complete split APK sets by their manifest package names.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from mobile_playbook.common.hashing import sha256_file
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
    split_paths: tuple[Path, ...] = ()

    # Return every original APK in install order, with the base first.
    @property
    def paths(self) -> list[Path]:
        return [self.path, *self.split_paths]

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


# Include set manifests and every APK so replacing or removing a split invalidates the scan cache.
def _dir_signature(directory: Path) -> tuple:
    entries = []
    paths = [*directory.glob("*.apk"), *directory.glob("*/apk-set.json"), *directory.glob("*/*.apk")]
    for path in sorted(paths):
        try:
            stat = path.stat()
        except OSError as exc:
            logger.debug("android intake: cannot stat %s for signature: %s", path, exc, exc_info=True)
            continue
        entries.append((str(path.relative_to(directory)), stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, path.is_symlink(), path.parent.is_symlink()))
    return tuple(entries)


# Read a declared set only when all its files, hashes and manifest identities still match.
def _read_apk_set(folder: Path) -> IntakeApk:
    manifest = folder / "apk-set.json"
    if folder.is_symlink() or manifest.is_symlink():
        raise ValueError("APK set must contain local regular files")
    record = json.loads(manifest.read_text())
    if record.get("schema_version") != 1:
        raise ValueError("Unsupported APK set manifest version")
    files = record["files"]
    names = [item["name"] for item in files]
    if (len(names) < 2 or len(set(names)) != len(names) or "base.apk" not in names
            or any(not isinstance(name, str) or Path(name).name != name or not name.endswith(".apk") for name in names)):
        raise ValueError("APK set needs a base and distinct local split filenames")
    if set(names) != {path.name for path in folder.glob("*.apk")}:
        raise ValueError("APK set files differ from its manifest")
    metadata = {}
    for item in files:
        path = folder / item["name"]
        if path.is_symlink() or not path.is_file() or sha256_file(path) != item["sha256"]:
            raise ValueError(f"APK set file is missing or changed: {path.name}")
        identity = inspect_apk_metadata(path)
        if (not record.get("package_name") or not record.get("version_code")
                or identity.get("package_name") != record["package_name"]
                or identity.get("version_code") != record["version_code"]):
            raise ValueError(f"APK set package/version mismatch: {path.name}")
        metadata[path.name] = identity
    base = metadata["base.apk"]
    return IntakeApk(
        path=folder / "base.apk", package_name=base["package_name"],
        display_name=base.get("display_name"), version_name=base.get("version_name"),
        version_code=base.get("version_code"),
        modified_at=max((folder / name).stat().st_mtime for name in names),
        split_paths=tuple(folder / name for name in sorted(names) if name != "base.apk"),
    )


# Resolve a base APK's declared companion splits without accepting incomplete or changed sets.
def apk_set_paths(base_apk: Path) -> list[Path]:
    if base_apk.name != "base.apk" or not (base_apk.parent / "apk-set.json").exists():
        return [base_apk]
    return _read_apk_set(base_apk.parent).paths


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
    for manifest in sorted(directory.glob("*/apk-set.json")):
        try:
            apks.append(_read_apk_set(manifest.parent))
        except Exception as exc:
            logger.warning("Skipping intake APK set %s: %s", manifest.parent.name, exc)
    apks.sort(key=_apk_order, reverse=True)
    _scan_cache[str(directory)] = (signature, apks)
    logger.debug("android intake: scan of %s found %s APKs", directory, len(apks))
    return apks


# Return the highest-version intake APK for a package, or None.
def find_intake_apk(package: str, intake_dir: Path | None = None) -> IntakeApk | None:
    match = next((apk for apk in list_intake_apks(intake_dir) if apk.package_name == package), None)
    logger.debug("android intake: %s -> %s", package, match.path if match else None)
    return match
