from __future__ import annotations

from pathlib import Path

from mobile_playbook.platforms.ios.models import ArtifactAcquisitionResult


class ArtifactProvider:
    source: str

    def acquire(
        self,
        app_config,
        global_config,
        device_client,
        run_timestamp: str,
        out_dir: Path,
    ) -> ArtifactAcquisitionResult:
        raise NotImplementedError
