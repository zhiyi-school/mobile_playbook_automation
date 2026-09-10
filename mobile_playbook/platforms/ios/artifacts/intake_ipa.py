"""Resolve an app's IPA out of the intake directory.

See docs/ios/risks.md#artifact-sources and docs/api.md#is-an-app-ready-to-test.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from mobile_playbook.platforms.ios.artifacts.local_ipa import LocalIpaProvider
from mobile_playbook.platforms.ios.ipa.plist_utils import inspect_ipa_metadata
from mobile_playbook.platforms.ios.models import ArtifactAcquisitionResult

def _default_intake_dir() -> Path:
    from mobile_playbook.storage import ios_intake_dir

    return ios_intake_dir()


def normalize_app_name(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


@dataclass(frozen=True)
class IntakeBuild:
    path: Path
    bundle_id: str
    display_name: str | None
    version: str | None
    modified_at: float

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


def _version_of(metadata: dict) -> str | None:
    info = metadata.get("info_plist") or {}
    return info.get("CFBundleShortVersionString") or info.get("CFBundleVersion") or None


def intake_dir_for(artifact: dict | None) -> Path:
    configured = (artifact or {}).get("intake_dir")
    return Path(configured).expanduser() if configured else _default_intake_dir()


_intake_dir = intake_dir_for


_scan_cache: dict[str, tuple[tuple, list[IntakeBuild]]] = {}


def _dir_signature(directory: Path) -> tuple:
    entries = []
    for path in sorted(directory.glob("*.ipa")):
        try:
            stat = path.stat()
        except OSError:
            continue
        entries.append((path.name, stat.st_mtime, stat.st_size))
    return tuple(entries)


def list_intake_ipas(intake_dir: Path | None = None) -> list[IntakeBuild]:
    """Every readable IPA in `intake_dir`, newest first. Unreadable files are skipped."""
    directory = intake_dir or _default_intake_dir()
    if not directory.is_dir():
        return []

    # Cached on a stat-only signature: the config is revalidated on every load
    # and scanning opens every zip.
    signature = _dir_signature(directory)
    cached = _scan_cache.get(str(directory))
    if cached is not None and cached[0] == signature:
        return cached[1]

    builds: list[IntakeBuild] = []
    for candidate in sorted(directory.glob("*.ipa")):
        try:
            metadata = inspect_ipa_metadata(candidate)
        except Exception:
            continue
        bundle_id = metadata.get("bundle_id")
        if not bundle_id:
            continue
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
    return builds


def resolve_intake_ipa(
    *,
    bundle_id: str | None = None,
    app_name: str | None = None,
    intake_dir: Path | None = None,
) -> IntakeResolution:
    """Find the build for an app, by bundle ID if one is known, else by name.

    Several versions of the same app resolve to the newest. Several *different*
    apps sharing a name resolve to `ambiguous`, never a guess.
    """
    builds = list_intake_ipas(intake_dir)

    if bundle_id:
        candidates = [build for build in builds if build.bundle_id == bundle_id]
        matched_on = "bundle_id"
    elif app_name and normalize_app_name(app_name):
        wanted = normalize_app_name(app_name)
        candidates = [build for build in builds if normalize_app_name(build.display_name) == wanted]
        matched_on = "name"
    else:
        return IntakeResolution(None, False, [], None)

    if not candidates:
        return IntakeResolution(None, False, [], matched_on)
    if len({build.bundle_id for build in candidates}) > 1:
        return IntakeResolution(None, True, candidates, matched_on)
    return IntakeResolution(candidates[0], False, candidates, matched_on)


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


def resolve_for_app(app_config) -> IntakeResolution:
    artifact = getattr(app_config, "artifact", None) or {}
    return resolve_intake_ipa(
        bundle_id=artifact.get("expected_bundle_id") or getattr(app_config, "bundle_id", None) or None,
        app_name=getattr(app_config, "name", None),
        intake_dir=_intake_dir(artifact),
    )


class IntakeIpaProvider(LocalIpaProvider):
    source = "intake_ipa"

    def _configured_path(self, artifact: dict) -> Path | None:
        value = (artifact or {}).get("ipa")
        return Path(value).expanduser() if value else None

    def acquire(self, app_config, global_config, device_client, run_timestamp: str, out_dir: Path):
        artifact = dict(app_config.artifact or {})
        directory = _intake_dir(artifact)
        resolution = resolve_for_app(app_config)

        if resolution.ambiguous:
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

        return super().acquire(app_config, global_config, device_client, run_timestamp, out_dir)
