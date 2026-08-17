from __future__ import annotations


class Risk:
    risk_id: str
    feature_id: str
    name: str
    requires_ipa_artifact: bool = False
    requires_device: bool = True

    def run(self, app_config, global_config, device_client, report_writer):
        raise NotImplementedError
