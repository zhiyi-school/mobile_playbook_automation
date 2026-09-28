from __future__ import annotations

import logging
from pathlib import Path

from mobile_playbook.platforms.ios.artifacts.base import ArtifactProvider
from mobile_playbook.platforms.ios.models import ArtifactAcquisitionResult

logger = logging.getLogger(__name__)


class InstalledAppReferenceProvider(ArtifactProvider):
    source = "installed_app_reference"

    def acquire(self, app_config, global_config, device_client, run_timestamp: str, out_dir: Path) -> ArtifactAcquisitionResult:
        logger.debug("ios artifacts[%s]: %s checking installed bundle %s", app_config.id, self.source, app_config.bundle_id)
        if device_client is None:
            logger.debug("ios artifacts[%s]: no device client; cannot verify installed app", app_config.id)
            return ArtifactAcquisitionResult(app_config.id, self.source, "FAILED", errors=["Device client is required"])
        try:
            installed = device_client.is_installed(app_config.bundle_id)
        except Exception as exc:
            logger.debug("ios artifacts[%s]: is_installed(%s) failed: %s", app_config.id, app_config.bundle_id, exc, exc_info=True)
            return ArtifactAcquisitionResult(app_config.id, self.source, "FAILED", errors=[str(exc)])
        logger.debug("ios artifacts[%s]: is_installed(%s) -> %s", app_config.id, app_config.bundle_id, installed)
        if installed:
            return ArtifactAcquisitionResult(
                app_config.id,
                self.source,
                "INSTALLED_APP_VERIFIED",
                bundle_id=app_config.bundle_id,
                metadata={"produces_ipa": False},
            )
        return ArtifactAcquisitionResult(app_config.id, self.source, "INSTALLED_APP_NOT_FOUND", bundle_id=app_config.bundle_id)
