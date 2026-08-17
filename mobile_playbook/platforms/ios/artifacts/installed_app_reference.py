from __future__ import annotations

from pathlib import Path

from mobile_playbook.platforms.ios.artifacts.base import ArtifactProvider
from mobile_playbook.platforms.ios.models import ArtifactAcquisitionResult


class InstalledAppReferenceProvider(ArtifactProvider):
    source = "installed_app_reference"

    def acquire(self, app_config, global_config, device_client, run_timestamp: str, out_dir: Path) -> ArtifactAcquisitionResult:
        if device_client is None:
            return ArtifactAcquisitionResult(app_config.id, self.source, "FAILED", errors=["Device client is required"])
        try:
            installed = device_client.is_installed(app_config.bundle_id)
        except Exception as exc:
            return ArtifactAcquisitionResult(app_config.id, self.source, "FAILED", errors=[str(exc)])
        if installed:
            return ArtifactAcquisitionResult(
                app_config.id,
                self.source,
                "INSTALLED_APP_VERIFIED",
                bundle_id=app_config.bundle_id,
                metadata={"produces_ipa": False},
            )
        return ArtifactAcquisitionResult(app_config.id, self.source, "INSTALLED_APP_NOT_FOUND", bundle_id=app_config.bundle_id)
