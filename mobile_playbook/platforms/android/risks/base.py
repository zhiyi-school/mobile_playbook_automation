from __future__ import annotations


class AndroidRisk:
    risk_id: str = ""
    feature_id: str = ""
    name: str = ""
    #: Displayed text, owned by configs/split/android/risks.yaml — subclasses
    #: leave these alone. See docs/android/risks.md#risk-metadata.
    description: str = ""
    tactic: str | None = None
    is_blocking: bool = False
    test_case_id: str = ""
    test_case_type: str = ""
    requires: list[str] = []
    requires_device: bool = True

    def run(self, app_config, global_config, device_client, report_writer):
        raise NotImplementedError
