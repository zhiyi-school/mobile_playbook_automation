from __future__ import annotations

import shutil
from pathlib import Path

from mobile_playbook.platforms.ios.artifacts.base import ArtifactProvider
from mobile_playbook.platforms.ios.mutations.hashing import sha256_file
from mobile_playbook.platforms.ios.ipa.plist_utils import inspect_ipa_metadata
from mobile_playbook.platforms.ios.models import ArtifactAcquisitionResult


class LocalIpaProvider(ArtifactProvider):
    source = "local_ipa"

    def _configured_path(self, artifact: dict) -> Path | None:
        value = artifact.get("ipa") or artifact.get("path")
        return Path(value).expanduser() if value else None

    def acquire(self, app_config, global_config, device_client, run_timestamp: str, out_dir: Path) -> ArtifactAcquisitionResult:
        artifact = app_config.artifact
        ipa_path = self._configured_path(artifact)
        if not ipa_path:
            return ArtifactAcquisitionResult(app_config.id, self.source, "ARTIFACT_REQUIRED", errors=["artifact.ipa is required"])
        if not ipa_path.exists():
            return ArtifactAcquisitionResult(app_config.id, self.source, "ARTIFACT_NOT_FOUND", source_path=ipa_path)
        if not ipa_path.is_file():
            return ArtifactAcquisitionResult(app_config.id, self.source, "ARTIFACT_INVALID", source_path=ipa_path, errors=["IPA path is not a file"])
        try:
            metadata = inspect_ipa_metadata(ipa_path)
        except Exception as exc:
            return ArtifactAcquisitionResult(app_config.id, self.source, "ARTIFACT_INVALID", source_path=ipa_path, errors=[str(exc)])
        expected = artifact.get("expected_bundle_id")
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
            shutil.copy2(ipa_path, copied)
        digest = sha256_file(copied)
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
