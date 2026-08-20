from __future__ import annotations


class Risk:
    risk_id: str
    feature_id: str
    name: str
    description: str = ""
    goal: str = ""
    is_blocking: bool = False
    mitre_attack_mobile_technique_id: str | None = None
    requires_ipa_artifact: bool = False
    requires_device: bool = True

    def run(self, app_config, global_config, device_client, report_writer):
        raise NotImplementedError
