from __future__ import annotations


class Risk:
    risk_id: str
    feature_id: str
    name: str
    #: Displayed text, owned by configs/split/ios/risks.yaml — subclasses leave
    #: these alone. See docs/ios/risks.md#risk-metadata.
    description: str = ""
    tactic: str | None = None
    is_blocking: bool = False
    requires_ipa_artifact: bool = False
    requires_device: bool = True
    #: False for risks that can only be tested by hand; a dashboard shows these
    #: as manual-only and a run never selects them.
    automation_available: bool = True

    def run(self, app_config, global_config, device_client, report_writer):
        raise NotImplementedError
