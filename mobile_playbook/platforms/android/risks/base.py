from __future__ import annotations


class AndroidRisk:
    risk_id: str = ""
    name: str = ""
    description: str = ""
    goal: str = ""
    is_blocking: bool = False
    mitre_attack_mobile_technique_id: str | None = None
    test_case_id: str = ""
    test_case_type: str = ""
    requires: list[str] = []
    requires_device: bool = True

    def run(self, app_config, global_config, device_client, report_writer):
        raise NotImplementedError
