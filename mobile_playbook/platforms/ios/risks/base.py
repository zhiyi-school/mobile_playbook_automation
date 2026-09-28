"""
Base class and shared metadata attributes for iOS risks.
"""

from __future__ import annotations


class Risk:
    risk_id: str
    feature_id: str
    name: str
    #: Displayed text owned by configs/split/ios/risks.yaml; see docs/ios/risks.md#risk-metadata.
    description: str = ""
    tactic: str | None = None
    is_blocking: bool = False
    requires_ipa_artifact: bool = False
    requires_device: bool = True
    #: False for manual-only risks, which a run never selects.
    automation_available: bool = True

    # Execute the risk against an app and return its result; subclasses implement it.
    def run(self, app_config, global_config, device_client, report_writer):
        raise NotImplementedError
