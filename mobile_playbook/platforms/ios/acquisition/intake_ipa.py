"""
Resolves an app's IPA out of the intake directory; see docs/ios/risks.md#artifact-sources.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from mobile_playbook.platforms.ios.acquisition.local_ipa import LocalIpaProvider
from mobile_playbook.platforms.ios.ipa.plist import inspect_ipa_metadata
from mobile_playbook.platforms.ios.models import ArtifactAcquisitionResult

logger = logging.getLogger(__name__)


# Return the storage-configured intake directory.
def _default_intake_dir() -> Path:
    from mobile_playbook.common.storage_paths import ios_intake_dir

    return ios_intake_dir()


# Lowercase a name and drop non-alphanumerics so display names compare loosely.
def normalize_app_name(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


@dataclass(frozen=True)
class IntakeBuild:
    path: Path
    bundle_id: str
    display_name: str | None
    version: str | None
    modified_at: float

    # Return the build as a JSON-compatible dict.
    def as_dict(self) -> dict:
        return {
            "file": self.path.name,
            "path": str(self.path),
            "bundle_id": self.bundle_id,
            "display_name": self.display_name,
            "version": self.version,
            "modified_at": self.modified_at,
        }


@dataclass(frozen=True)
class IntakeMatch:
    path: Path
    bundle_id: str
    version: str | None
    candidate_count: int


@dataclass(frozen=True)
class IntakeResolution:
    match: IntakeBuild | None
    ambiguous: bool
    candidates: list[IntakeBuild]
    matched_on: str | None


# Return the short version string, falling back to the build number, from IPA metadata.
def _version_of(metadata: dict) -> str | None:
    info = metadata.get("info_plist") or {}
    return info.get("CFBundleShortVersionString") or info.get("CFBundleVersion") or None


# Return the artifact's configured intake directory, or the default one.
def intake_dir_for(artifact: dict | None) -> Path:
    configured = (artifact or {}).get("intake_dir")
    return Path(configured).expanduser() if configured else _default_intake_dir()


_intake_dir = intake_dir_for


_scan_cache: dict[str, tuple[tuple, list[IntakeBuild]]] = {}


# Return the name, mtime and size of each IPA in a directory as a cache signature.
def _dir_signature(directory: Path) -> tuple:
    entries = []
    for path in sorted(directory.glob("*.ipa")):
        try:
            stat = path.stat()
        except OSError as exc:
            logger.debug("ios intake: cannot stat %s for signature: %s", path, exc, exc_info=True)
            continue
        entries.append((path.name, stat.st_mtime, stat.st_size))
    return tuple(entries)


# Return every readable IPA in the intake directory, newest first, skipping unreadable files.
def list_intake_ipas(intake_dir: Path | None = None) -> list[IntakeBuild]:
    directory = intake_dir or _default_intake_dir()
    if not directory.is_dir():
        logger.debug("ios intake: intake dir %s is not a directory; no builds", directory)
        return []

    # Cached on a stat-only signature: config revalidates on every load and scanning opens every zip.
    signature = _dir_signature(directory)
    cached = _scan_cache.get(str(directory))
    if cached is not None and cached[0] == signature:
        logger.debug("ios intake: scan cache hit for %s (%s ipa files, %s builds)", directory, len(signature), len(cached[1]))
        return cached[1]

    logger.debug("ios intake: scanning %s (%s ipa files)", directory, len(signature))
    builds: list[IntakeBuild] = []
    for candidate in sorted(directory.glob("*.ipa")):
        try:
            metadata = inspect_ipa_metadata(candidate)
        except Exception as exc:
            logger.debug("ios intake: skipping unreadable ipa %s: %s", candidate, exc, exc_info=True)
            continue
        bundle_id = metadata.get("bundle_id")
        if not bundle_id:
            logger.debug("ios intake: skipping %s; Info.plist has no CFBundleIdentifier", candidate)
            continue
        logger.debug(
            "ios intake: found %s bundle_id=%s display_name=%s version=%s",
            candidate.name,
            bundle_id,
            metadata.get("display_name"),
            _version_of(metadata),
        )
        builds.append(
            IntakeBuild(
                path=candidate,
                bundle_id=bundle_id,
                display_name=metadata.get("display_name"),
                version=_version_of(metadata),
                modified_at=candidate.stat().st_mtime,
            )
        )
    builds.sort(key=lambda build: build.modified_at, reverse=True)
    _scan_cache[str(directory)] = (signature, builds)
    logger.debug("ios intake: scan of %s produced %s builds", directory, len(builds))
    return builds


# Find the newest build by bundle ID, else by name; different apps sharing a name are ambiguous.
def resolve_intake_ipa(
    *,
    bundle_id: str | None = None,
    app_name: str | None = None,
    intake_dir: Path | None = None,
) -> IntakeResolution:
    builds = list_intake_ipas(intake_dir)

    if bundle_id:
        candidates = [build for build in builds if build.bundle_id == bundle_id]
        matched_on = "bundle_id"
    elif app_name and normalize_app_name(app_name):
        wanted = normalize_app_name(app_name)
        candidates = [build for build in builds if normalize_app_name(build.display_name) == wanted]
        matched_on = "name"
    else:
        logger.debug("ios intake: no bundle id or usable app name given; nothing to resolve")
        return IntakeResolution(None, False, [], None)

    logger.debug(
        "ios intake: resolving bundle_id=%s app_name=%s matched_on=%s -> %s candidates of %s builds",
        bundle_id,
        app_name,
        matched_on,
        len(candidates),
        len(builds),
    )
    if not candidates:
        return IntakeResolution(None, False, [], matched_on)
    if len({build.bundle_id for build in candidates}) > 1:
        logger.debug(
            "ios intake: ambiguous match for %s; bundle ids %s", app_name, sorted({build.bundle_id for build in candidates})
        )
        return IntakeResolution(None, True, candidates, matched_on)
    logger.debug("ios intake: selected newest build %s (version %s)", candidates[0].path, candidates[0].version)
    return IntakeResolution(candidates[0], False, candidates, matched_on)


# Return the newest intake build for a bundle ID, or None.
def find_intake_ipa(bundle_id: str, intake_dir: Path | None = None) -> IntakeMatch | None:
    resolution = resolve_intake_ipa(bundle_id=bundle_id, intake_dir=intake_dir)
    if resolution.match is None:
        return None
    return IntakeMatch(
        path=resolution.match.path,
        bundle_id=bundle_id,
        version=resolution.match.version,
        candidate_count=len(resolution.candidates),
    )


# Resolve an app config's intake build by its expected bundle ID or name.
def resolve_for_app(app_config) -> IntakeResolution:
    artifact = getattr(app_config, "artifact", None) or {}
    return resolve_intake_ipa(
        bundle_id=artifact.get("expected_bundle_id") or getattr(app_config, "bundle_id", None) or None,
        app_name=getattr(app_config, "name", None),
        intake_dir=_intake_dir(artifact),
    )


class IntakeIpaProvider(LocalIpaProvider):
    source = "intake_ipa"

    # Return the IPA path set by intake resolution, if any.
    def _configured_path(self, artifact: dict) -> Path | None:
        value = (artifact or {}).get("ipa")
        return Path(value).expanduser() if value else None

    # Resolve the app's intake build, fill in its bundle IDs, then acquire it as a local IPA.
    def acquire(self, app_config, global_config, device_client, run_timestamp: str, out_dir: Path):
        artifact = dict(app_config.artifact or {})
        directory = _intake_dir(artifact)
        logger.debug("ios artifacts[%s]: %s resolving from %s", app_config.id, self.source, directory)
        resolution = resolve_for_app(app_config)

        if resolution.ambiguous:
            logger.debug("ios artifacts[%s]: intake resolution ambiguous (%s candidates)", app_config.id, len(resolution.candidates))
            found = ", ".join(sorted({build.bundle_id for build in resolution.candidates}))
            return ArtifactAcquisitionResult(
                app_config.id,
                self.source,
                "ARTIFACT_INVALID",
                source_path=directory,
                errors=[
                    f"Several different apps in {directory} are named {app_config.name!r} "
                    f"({found}). Remove the ones that don't belong, or set "
                    "artifact.expected_bundle_id to say which is meant."
                ],
            )

        if resolution.match is None:
            wanted = artifact.get("expected_bundle_id") or app_config.bundle_id or app_config.name
            logger.debug("ios artifacts[%s]: no intake build for %s in %s", app_config.id, wanted, directory)
            return ArtifactAcquisitionResult(
                app_config.id,
                self.source,
                "ARTIFACT_NOT_FOUND",
                source_path=directory,
                errors=[
                    f"No build for {wanted!r} in {directory}. "
                    "Extract it from the test device and drop the file in there."
                ],
            )

        artifact["ipa"] = str(resolution.match.path)
        artifact["expected_bundle_id"] = resolution.match.bundle_id
        app_config.artifact = artifact
        if not app_config.bundle_id:
            app_config.bundle_id = resolution.match.bundle_id
        if not app_config.test_bundle_id:
            app_config.test_bundle_id = resolution.match.bundle_id
        logger.debug(
            "ios artifacts[%s]: intake resolved %s (bundle_id=%s, matched_on=%s); bundle_id=%s test_bundle_id=%s",
            app_config.id,
            resolution.match.path,
            resolution.match.bundle_id,
            resolution.matched_on,
            app_config.bundle_id,
            app_config.test_bundle_id,
        )

        return super().acquire(app_config, global_config, device_client, run_timestamp, out_dir)
