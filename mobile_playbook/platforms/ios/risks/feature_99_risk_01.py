"""Placeholder manual-only risk. Delete once a real manual-only risk exists."""

from __future__ import annotations

from mobile_playbook.platforms.ios.risks.base import Risk


class ManualOnlyPlaceholderRisk(Risk):
    risk_id = "ios-feature-99-risk-01"
    feature_id = "feature-99"
    name = "Manual review placeholder"
    is_blocking = False
    requires_device = False
    automation_available = False

    def run(self, app_config, global_config, device_client, report_writer):
        raise NotImplementedError("This risk is manual only and is never run automatically.")
