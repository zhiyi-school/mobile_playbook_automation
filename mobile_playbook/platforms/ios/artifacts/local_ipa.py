"""
Artifact provider that validates a configured local IPA and copies it into the run directory.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from mobile_playbook.platforms.ios.artifacts.base import ArtifactProvider
from mobile_playbook.platforms.ios.mutations.hashing import sha256_file
from mobile_playbook.platforms.ios.ipa.plist_utils import inspect_ipa_metadata
from mobile_playbook.platforms.ios.models import ArtifactAcquisitionResult

logger = logging.getLogger(__name__)


class LocalIpaProvider(ArtifactProvider):
    source = "local_ipa"

    # Return the configured IPA path from the artifact's ipa or path setting, if any.
    def _configured_path(self, artifact: dict) -> Path | None:
        value = artifact.get("ipa") or artifact.get("path")
        return Path(value).expanduser() if value else None

    # Validate the configured IPA and its bundle ID, copy it into the run directory and hash it.
    def acquire(self, app_config, global_config, device_client, run_timestamp: str, out_dir: Path) -> ArtifactAcquisitionResult:
        artifact = app_config.artifact
        ipa_path = self._configured_path(artifact)
        logger.debug("ios artifacts[%s]: %s acquiring from configured path %s", app_config.id, self.source, ipa_path)
        if not ipa_path:
            return ArtifactAcquisitionResult(app_config.id, self.source, "ARTIFACT_REQUIRED", errors=["artifact.ipa is required"])
        if not ipa_path.exists():
            logger.debug("ios artifacts[%s]: ipa %s does not exist", app_config.id, ipa_path)
            return ArtifactAcquisitionResult(app_config.id, self.source, "ARTIFACT_NOT_FOUND", source_path=ipa_path)
        if not ipa_path.is_file():
            logger.debug("ios artifacts[%s]: ipa path %s is not a file", app_config.id, ipa_path)
            return ArtifactAcquisitionResult(app_config.id, self.source, "ARTIFACT_INVALID", source_path=ipa_path, errors=["IPA path is not a file"])
        try:
            metadata = inspect_ipa_metadata(ipa_path)
        except Exception as exc:
            logger.debug("ios artifacts[%s]: inspecting %s failed: %s", app_config.id, ipa_path, exc, exc_info=True)
            return ArtifactAcquisitionResult(app_config.id, self.source, "ARTIFACT_INVALID", source_path=ipa_path, errors=[str(exc)])
        expected = artifact.get("expected_bundle_id")
        logger.debug(
            "ios artifacts[%s]: ipa metadata app=%s bundle_id=%s display_name=%s executable=%s expected_bundle_id=%s",
            app_config.id,
            metadata.get("app_name"),
            metadata.get("bundle_id"),
            metadata.get("display_name"),
            metadata.get("executable_name"),
            expected,
        )
        if expected and metadata.get("bundle_id") != expected:
            return ArtifactAcquisitionResult(
                app_config.id,
                self.source,
                "ARTIFACT_BUNDLE_ID_MISMATCH",
                source_path=ipa_path,
                bundle_id=metadata.get("bundle_id"),
                errors=[f"Expected bundle ID {expected}, found {metadata.get('bundle_id')}"],
            )
        app_out = Path(out_dir) / run_timestamp / app_config.id
        app_out.mkdir(parents=True, exist_ok=True)
        copied = app_out / "original.ipa"
        if ipa_path.resolve() != copied.resolve():
            logger.debug("ios artifacts[%s]: copying %s -> %s", app_config.id, ipa_path, copied)
            shutil.copy2(ipa_path, copied)
        else:
            logger.debug("ios artifacts[%s]: %s already in the run directory; not copying", app_config.id, copied)
        digest = sha256_file(copied)
        logger.debug("ios artifacts[%s]: acquired %s sha256=%s", app_config.id, copied, digest)
        return ArtifactAcquisitionResult(
            app_id=app_config.id,
            source=self.source,
            status="ACQUIRED",
            ipa_path=copied,
            source_path=ipa_path,
            bundle_id=metadata.get("bundle_id"),
            display_name=metadata.get("display_name"),
            executable_name=metadata.get("executable_name"),
            input_sha256=digest,
            metadata=metadata,
        )
