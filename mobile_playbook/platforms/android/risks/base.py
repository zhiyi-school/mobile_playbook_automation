"""
Base class for Android risk checks.
"""

from __future__ import annotations


class AndroidRisk:
    """Metadata and entry point shared by every Android risk check."""

    risk_id: str = ""
    feature_id: str = ""
    name: str = ""
    #: Owned by configs/split/android/risks.yaml, not subclasses; see docs/android/risks.md#risk-metadata.
    description: str = ""
    tactic: str | None = None
    is_blocking: bool = False
    test_case_id: str = ""
    test_case_type: str = ""
    requires: list[str] = []
    requires_device: bool = True

    # Run the check for one app; subclasses implement it.
    def run(self, app_config, global_config, device_client, report_writer):
        raise NotImplementedError
