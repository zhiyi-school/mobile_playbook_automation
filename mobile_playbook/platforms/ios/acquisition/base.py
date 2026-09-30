"""
Base interface for iOS artifact acquisition providers.
"""

from __future__ import annotations

from pathlib import Path

from mobile_playbook.platforms.ios.models import ArtifactAcquisitionResult


class ArtifactProvider:
    source: str

    # Acquire the app artifact for a run and describe the outcome; subclasses implement it.
    def acquire(
        self,
        app_config,
        global_config,
        device_client,
        run_timestamp: str,
        out_dir: Path,
    ) -> ArtifactAcquisitionResult:
        raise NotImplementedError
