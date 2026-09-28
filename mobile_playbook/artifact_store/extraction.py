"""
Extracts, normalizes and caches app icons from iOS IPAs and Android APKs.
"""

from __future__ import annotations

import logging
import plistlib
import posixpath
import re
import tempfile
import zipfile
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from mobile_playbook.artifact_store import asset_catalog, png, store
from mobile_playbook.platforms.ios.ipa.unpacker import is_safe_member_name

logger = logging.getLogger(__name__)

MIME_TYPE = "image/png"
MAX_CANDIDATES = 12

STATUS_AVAILABLE = "available"
STATUS_UNAVAILABLE = "unavailable"
STATUS_FAILED = "failed"

ENVIRONMENT_DEPENDENT_REASONS = frozenset({"asset_catalog_tool_unavailable", "unsupported_platform"})

_IOS_FALLBACK_STEM = re.compile(r"^(appicon|icon)", re.IGNORECASE)
_ANDROID_ICON_STEM = re.compile(r"(ic_launcher|ic_app|app_icon)", re.IGNORECASE)
_ANDROID_DENSITY = ("xxxhdpi", "xxhdpi", "xhdpi", "hdpi", "mdpi", "ldpi", "nodpi", "anydpi")


@dataclass(frozen=True)
class IconExtraction:
    status: str
    reason: str | None = None
    artifact_id: str | None = None
    storage_ref: str | None = None
    width: int | None = None
    height: int | None = None

    # Reports whether an icon is available.
    @property
    def available(self) -> bool:
        return self.status == STATUS_AVAILABLE

    # Returns the extraction state as the dict recorded in metadata and API responses.
    def as_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "status": self.status,
            "reason": self.reason,
            "storage_ref": self.storage_ref,
            "mime_type": MIME_TYPE if self.available else None,
            "width": self.width,
            "height": self.height,
        }


@dataclass(frozen=True)
class _Candidate:
    name: str
    size: int


# Returns an unavailable extraction with the given reason.
def _unavailable(reason: str, artifact_id: str | None = None) -> IconExtraction:
    return IconExtraction(status=STATUS_UNAVAILABLE, reason=reason, artifact_id=artifact_id)


# Reads a safe archive member within max_bytes, or returns None when unsafe, missing or too large.
def _read_member(zf: zipfile.ZipFile, name: str, max_bytes: int = png.MAX_SOURCE_BYTES) -> bytes | None:
    if not is_safe_member_name(name):
        logger.debug("artifact store: unsafe archive member %r skipped.", name)
        logger.warning("Skipped an archive entry with an unsafe path.")
        return None
    try:
        info = zf.getinfo(name)
    except KeyError:
        logger.debug("artifact store: archive member %s not present.", name)
        return None
    if info.file_size > max_bytes:
        logger.debug(
            "artifact store: archive member %s is %d bytes, over the %d-byte limit.", name, info.file_size, max_bytes
        )
        return None
    logger.debug("artifact store: reading archive member %s (%d bytes).", name, info.file_size)
    with zf.open(info) as handle:
        return handle.read(max_bytes + 1)


# Returns the widest usable PNG among the largest candidates, with its member name.
def _best_icon(zf: zipfile.ZipFile, candidates: Iterable[_Candidate]) -> tuple[png.NormalizedPng, str] | None:
    ordered = sorted(candidates, key=lambda item: item.size, reverse=True)[:MAX_CANDIDATES]
    logger.debug("artifact store: evaluating %d icon candidate(s).", len(ordered))
    best: tuple[png.NormalizedPng, str] | None = None
    for candidate in ordered:
        data = _read_member(zf, candidate.name)
        if not data:
            continue
        try:
            normalized = png.normalize(data)
        except png.UnsupportedImage as exc:
            logger.debug("artifact store: candidate %s is not a usable PNG: %s", candidate.name, exc)
            continue
        logger.debug(
            "artifact store: candidate %s normalized to %dx%d.", candidate.name, normalized.width, normalized.height
        )
        if best is None or normalized.width > best[0].width:
            best = (normalized, candidate.name)
    logger.debug("artifact store: best icon candidate %s.", best[1] if best else None)
    return best


# Returns the Payload/<name>.app/ prefix of an IPA's app bundle, or None.
def _payload_app_prefix(names: Iterable[str]) -> str | None:
    for name in names:
        parts = PurePosixPath(name).parts
        if len(parts) >= 2 and parts[0] == "Payload" and parts[1].endswith(".app"):
            return f"Payload/{parts[1]}/"
    return None


# Collects the icon file names declared in an app's Info.plist.
def _ios_icon_base_names(info: dict[str, Any]) -> list[str]:
    names: list[str] = []

    # Appends a string, or each string in a nested list, to names.
    def add(value: Any) -> None:
        if isinstance(value, str) and value:
            names.append(value)
        elif isinstance(value, list):
            for item in value:
                add(item)

    for key in ("CFBundleIcons", "CFBundleIcons~ipad"):
        primary = (info.get(key) or {}).get("CFBundlePrimaryIcon") if isinstance(info.get(key), dict) else None
        if isinstance(primary, dict):
            add(primary.get("CFBundleIconFiles"))
            add(primary.get("CFBundleIconName"))
    add(info.get("CFBundleIconFiles"))
    add(info.get("CFBundleIconFile"))
    add(info.get("CFBundleIconName"))
    return names


# Returns bundle-root PNGs matching declared or conventional icon names, and whether Assets.car exists.
def _ios_candidates(zf: zipfile.ZipFile, prefix: str, info: dict[str, Any]) -> tuple[list[_Candidate], bool]:
    base_names = {name.lower() for name in _ios_icon_base_names(info)}
    root_files: list[zipfile.ZipInfo] = []
    has_asset_catalog = False
    for item in zf.infolist():
        if not item.filename.startswith(prefix) or item.is_dir():
            continue
        relative = item.filename[len(prefix) :]
        if relative == "Assets.car":
            has_asset_catalog = True
        if "/" in relative or not relative.lower().endswith(".png"):
            continue
        root_files.append(item)

    named = [
        _Candidate(item.filename, item.file_size)
        for item in root_files
        if any(PurePosixPath(item.filename).stem.lower().startswith(base) for base in base_names)
    ]
    logger.debug(
        "artifact store: %d root PNG(s), %d matching declared icon names %s, asset catalog=%s.",
        len(root_files),
        len(named),
        sorted(base_names),
        has_asset_catalog,
    )
    if named:
        return named, has_asset_catalog

    fallback = [
        _Candidate(item.filename, item.file_size)
        for item in root_files
        if _IOS_FALLBACK_STEM.match(PurePosixPath(item.filename).stem)
    ]
    logger.debug("artifact store: %d fallback icon candidate(s) by file name.", len(fallback))
    return fallback, has_asset_catalog


# Extracts the primary icon from an IPA's bundle-root PNGs, else reports what its asset catalog holds.
def _extract_ios_icon(artifact_path: Path) -> tuple[png.NormalizedPng | None, str]:
    with zipfile.ZipFile(artifact_path) as zf:
        names = zf.namelist()
        prefix = _payload_app_prefix(names)
        logger.debug("artifact store: %s has %d member(s); app bundle prefix %s.", artifact_path, len(names), prefix)
        if prefix is None:
            return None, "no_app_bundle"
        info_name = f"{prefix}Info.plist"
        raw_info = _read_member(zf, info_name)
        info: dict[str, Any] = {}
        if raw_info:
            try:
                loaded = plistlib.loads(raw_info)
                info = loaded if isinstance(loaded, dict) else {}
            except Exception:
                logger.debug("artifact store: %s could not be parsed.", info_name, exc_info=True)
                info = {}
        else:
            logger.debug("artifact store: %s missing or unreadable.", info_name)

        candidates, has_asset_catalog = _ios_candidates(zf, prefix, info)
        if candidates:
            best = _best_icon(zf, candidates)
            if best is not None:
                return best[0], STATUS_AVAILABLE
            if not has_asset_catalog:
                return None, "unsupported_icon_format"
        if not has_asset_catalog:
            return None, "no_icon_in_bundle"
        logger.debug("artifact store: falling back to the asset catalog in %s.", artifact_path)
        return None, _inspect_asset_catalog(zf, prefix, info)


# Identifies the catalog's primary icon and returns why it is not extracted. See docs/api.md#ios-asset-catalogs.
def _inspect_asset_catalog(zf: zipfile.ZipFile, prefix: str, info: dict[str, Any]) -> str:
    if not asset_catalog.assetutil_available():
        logger.debug("artifact store: assetutil unavailable; cannot inspect the asset catalog.")
        return "asset_catalog_tool_unavailable"

    raw = _read_member(zf, f"{prefix}Assets.car", asset_catalog.MAX_CATALOG_BYTES)
    if not raw:
        return "asset_catalog_unreadable"

    with tempfile.TemporaryDirectory(prefix="mp-assets-") as workspace:
        car_path = Path(workspace) / "Assets.car"
        try:
            car_path.write_bytes(raw)
            logger.debug("artifact store: wrote %d-byte asset catalog to %s.", len(raw), car_path)
            entries = asset_catalog.read_catalog(car_path)
        except OSError:
            logger.debug("artifact store: asset catalog could not be staged.", exc_info=True)
            return "asset_catalog_unreadable"

    if entries is None:
        return "asset_catalog_unreadable"
    icon_name = info.get("CFBundleIconName")
    rendition = asset_catalog.primary_icon_rendition(entries, icon_name if isinstance(icon_name, str) else None)
    if rendition is None:
        return "asset_catalog_no_icon"
    logger.info(
        "Asset catalog holds a %dx%d primary icon that no available tool can export.",
        rendition.width,
        rendition.height,
    )
    return "asset_catalog_no_extractor"


# Returns the icon resource paths an APK's manifest declares, or an empty list on failure.
def _android_declared_icons(artifact_path: Path) -> list[str]:
    from mobile_playbook.platforms.android import apk_tools

    try:
        declared = apk_tools.icon_resource_paths(artifact_path)
    except Exception:
        logger.debug("artifact store: declared icon lookup failed for %s.", artifact_path, exc_info=True)
        return []
    logger.debug("artifact store: %s declares icon resource(s) %s.", artifact_path, declared)
    return declared


# Returns the resource names an icon may be stored under.
def _android_declared_stems(declared: Iterable[str]) -> set[str]:
    from mobile_playbook.platforms.android import apk_tools

    stems: set[str] = set()
    for reference in declared:
        if not reference:
            continue
        named = apk_tools.resource_reference_name(reference)
        stems.add((named or PurePosixPath(reference).stem).lower())
    return stems


# Returns an adaptive icon's raster foreground or monochrome layers when shipped alongside the XML.
def _android_adaptive_layers(zf: zipfile.ZipFile, declared: Iterable[str]) -> list[_Candidate]:
    wanted = {f"{stem}_foreground" for stem in _android_declared_stems(declared)}
    wanted |= {"ic_launcher_foreground", "ic_launcher_monochrome"}
    return [
        _Candidate(item.filename, item.file_size)
        for item in zf.infolist()
        if not item.is_dir()
        and item.filename.startswith("res/")
        and item.filename.lower().endswith(".png")
        and PurePosixPath(item.filename).stem.lower() in wanted
    ]


# Returns res/ PNGs matching declared icon names or launcher icon naming conventions.
def _android_zip_candidates(zf: zipfile.ZipFile, declared: Iterable[str]) -> list[_Candidate]:
    declared_stems = _android_declared_stems(declared)
    candidates: list[_Candidate] = []
    for item in zf.infolist():
        name = item.filename
        if item.is_dir() or not name.lower().endswith(".png"):
            continue
        if not name.startswith("res/"):
            continue
        directory = posixpath.dirname(name)
        stem = PurePosixPath(name).stem.lower()
        matches_declared = stem in declared_stems
        matches_convention = (
            _ANDROID_ICON_STEM.search(stem) is not None
            and any(directory.startswith(f"res/{kind}") for kind in ("mipmap", "drawable"))
        )
        if matches_declared or matches_convention:
            candidates.append(_Candidate(name, item.file_size))
    return candidates


# Ranks a resource path by screen density, highest density first.
def _android_density_rank(name: str) -> int:
    lowered = name.lower()
    for index, density in enumerate(_ANDROID_DENSITY):
        if density in lowered:
            return len(_ANDROID_DENSITY) - index
    return 0


# Extracts the best-density launcher icon PNG from an APK, or returns why none is usable.
def _extract_android_icon(artifact_path: Path) -> tuple[png.NormalizedPng | None, str]:
    declared = _android_declared_icons(artifact_path)
    with zipfile.ZipFile(artifact_path) as zf:
        members = set(zf.namelist())
        direct = [
            _Candidate(name, zf.getinfo(name).file_size)
            for name in declared
            if name.lower().endswith(".png") and is_safe_member_name(name) and name in members
        ]
        candidates = direct or _android_zip_candidates(zf, declared)
        only_xml = bool(declared) and all(name.lower().endswith(".xml") for name in declared)
        logger.debug(
            "artifact store: %d direct and %d total Android icon candidate(s); declared only XML=%s.",
            len(direct),
            len(candidates),
            only_xml,
        )
        if not candidates and only_xml:
            candidates = _android_adaptive_layers(zf, declared)
            logger.debug("artifact store: %d adaptive icon layer candidate(s).", len(candidates))
        if not candidates:
            return None, "adaptive_icon_vector_only" if only_xml else "no_icon_in_archive"
        candidates.sort(key=lambda item: (_android_density_rank(item.name), item.size), reverse=True)
        best = _best_icon(zf, candidates)
        if best is None:
            return None, "unsupported_icon_format"
        return best[0], STATUS_AVAILABLE


_EXTRACTORS = {"ios": _extract_ios_icon, "android": _extract_android_icon}


# Resolves, normalizes and caches one artifact's icon, remembering absences; never raises.
def extract_icon(
    platform: str,
    artifact_path: Path,
    artifact_id: str | None = None,
    force: bool = False,
) -> IconExtraction:
    path = Path(artifact_path)
    try:
        resolved_id = artifact_id or store.artifact_digest(path)
    except OSError:
        logger.debug("artifact store: %s could not be digested.", path, exc_info=True)
        return IconExtraction(status=STATUS_FAILED, reason="artifact_unreadable")

    cached = store.icon_path(resolved_id)
    cached_meta = store.read_metadata(resolved_id) or {}
    icon_meta = cached_meta.get("icon") or {}
    logger.debug("artifact store: icon for %s artifact %s (force=%s).", platform, resolved_id[:12], force)
    if not force:
        if cached.is_file():
            logger.debug("artifact store: icon cache hit at %s.", cached)
            return IconExtraction(
                status=STATUS_AVAILABLE,
                artifact_id=resolved_id,
                storage_ref=store.icon_ref(resolved_id),
                width=icon_meta.get("width"),
                height=icon_meta.get("height"),
            )
        remembered = _remembered_absence(icon_meta)
        if remembered is not None:
            logger.debug(
                "artifact store: remembered absence for %s (%s); not rescanning.", resolved_id[:12], remembered
            )
            return IconExtraction(status=STATUS_UNAVAILABLE, reason=remembered, artifact_id=resolved_id)
        logger.debug("artifact store: icon cache miss for %s.", resolved_id[:12])

    extractor = _EXTRACTORS.get(platform)
    if extractor is None:
        logger.debug("artifact store: no icon extractor for platform %s.", platform)
        return _unavailable("unsupported_platform", resolved_id)

    try:
        normalized, reason = extractor(path)
    except (zipfile.BadZipFile, OSError, ValueError) as exc:
        logger.debug("artifact store: icon extraction from %s raised.", path, exc_info=True)
        logger.warning("Icon extraction failed for a %s artifact: %s", platform, type(exc).__name__)
        return IconExtraction(status=STATUS_FAILED, reason="artifact_unreadable", artifact_id=resolved_id)
    except Exception as exc:
        logger.debug("artifact store: icon extraction from %s raised unexpectedly.", path, exc_info=True)
        logger.warning("Icon extraction failed for a %s artifact: %s", platform, type(exc).__name__)
        return IconExtraction(status=STATUS_FAILED, reason="extraction_error", artifact_id=resolved_id)

    logger.debug("artifact store: extractor for %s returned %s.", resolved_id[:12], reason)
    if normalized is None:
        absent = _unavailable(reason, resolved_id)
        _record_icon(platform, resolved_id, absent)
        return absent

    try:
        store.write_atomic(cached, normalized.data)
    except OSError:
        logger.debug("artifact store: icon write to %s failed.", cached, exc_info=True)
        logger.warning("Extracted icon could not be written to the artifact store.")
        return IconExtraction(status=STATUS_FAILED, reason="store_unwritable", artifact_id=resolved_id)

    result = IconExtraction(
        status=STATUS_AVAILABLE,
        artifact_id=resolved_id,
        storage_ref=store.icon_ref(resolved_id),
        width=normalized.width,
        height=normalized.height,
    )
    _record_icon(platform, resolved_id, result)
    return result


# Returns a recorded, environment-independent absence reason so a large artifact is not rescanned every pass.
def _remembered_absence(icon_meta: dict[str, Any]) -> str | None:
    if icon_meta.get("status") != STATUS_UNAVAILABLE:
        return None
    reason = icon_meta.get("reason")
    if not isinstance(reason, str) or reason in ENVIRONMENT_DEPENDENT_REASONS:
        logger.debug("artifact store: recorded absence %r is environment-dependent or invalid; retrying.", reason)
        return None
    return reason


# Persists the icon block so a later cache hit can report its dimensions.
def _record_icon(platform: str, artifact_id: str, icon: IconExtraction) -> None:
    metadata = store.read_metadata(artifact_id) or {}
    metadata.update({"artifact_id": artifact_id, "sha256": artifact_id, "platform": platform})
    metadata["icon"] = icon.as_dict()
    logger.debug("artifact store: recording icon status %s for %s.", icon.status, artifact_id[:12])
    try:
        store.write_metadata(artifact_id, metadata)
    except OSError:
        logger.debug("artifact store: metadata write for %s failed.", artifact_id[:12], exc_info=True)
        logger.warning("Artifact metadata could not be written to the artifact store.")


# Returns an IPA's bundle id, display name and version.
def _ios_artifact_facts(artifact_path: Path) -> dict[str, Any]:
    from mobile_playbook.platforms.ios.ipa.plist_utils import inspect_ipa_metadata

    metadata = inspect_ipa_metadata(artifact_path)
    info = metadata.get("info_plist") or {}
    return {
        "bundle_id": metadata.get("bundle_id"),
        "display_name": metadata.get("display_name"),
        "version": info.get("CFBundleShortVersionString") or info.get("CFBundleVersion"),
    }


# Returns an APK's package name, display name and version.
def _android_artifact_facts(artifact_path: Path) -> dict[str, Any]:
    from mobile_playbook.platforms.android.apk_tools import inspect_apk_metadata

    metadata = inspect_apk_metadata(artifact_path)
    return {
        "bundle_id": metadata.get("package_name"),
        "display_name": metadata.get("display_name"),
        "version": metadata.get("version_name"),
    }


# Records identity, checksum and icon state for one stored artifact; icon failure is not artifact failure.
def describe_artifact(platform: str, artifact_path: Path) -> dict[str, Any]:
    path = Path(artifact_path)
    artifact_id = store.artifact_digest(path)

    facts: dict[str, Any] = {"bundle_id": None, "display_name": None, "version": None}
    error: str | None = None
    logger.debug("artifact store: describing %s artifact %s (%s).", platform, path, artifact_id[:12])
    try:
        facts = _ios_artifact_facts(path) if platform == "ios" else _android_artifact_facts(path)
    except Exception as exc:
        logger.debug("artifact store: reading facts from %s failed.", path, exc_info=True)
        error = str(exc)

    icon = extract_icon(platform, path, artifact_id)
    metadata = {
        "artifact_id": artifact_id,
        "sha256": artifact_id,
        "platform": platform,
        **facts,
        "icon": icon.as_dict(),
    }
    if error:
        metadata["error"] = error

    try:
        store.write_metadata(artifact_id, metadata)
    except OSError:
        logger.debug("artifact store: metadata write for %s failed.", artifact_id[:12], exc_info=True)
        logger.warning("Artifact metadata could not be written to the artifact store.")
    return metadata


# Returns the cached icon extraction for an artifact id, or None when no icon is stored.
def cached_extraction(artifact_id: str, metadata: dict[str, Any] | None = None) -> IconExtraction | None:
    if not store.is_artifact_id(artifact_id) or not store.icon_path(artifact_id).is_file():
        logger.debug("artifact store: no cached icon for %r.", artifact_id)
        return None
    icon_meta = ((metadata or store.read_metadata(artifact_id) or {}).get("icon")) or {}
    return replace(
        IconExtraction(status=STATUS_AVAILABLE, artifact_id=artifact_id, storage_ref=store.icon_ref(artifact_id)),
        width=icon_meta.get("width"),
        height=icon_meta.get("height"),
    )
