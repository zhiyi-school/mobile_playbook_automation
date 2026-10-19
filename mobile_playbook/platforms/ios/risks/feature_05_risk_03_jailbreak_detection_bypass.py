"""
ios-feature-05-risk-03: manual-only; testers follow the playbook demonstration on a jailbroken device.
"""

from __future__ import annotations

from mobile_playbook.platforms.ios.risks.base import Risk


class Feature05Risk03(Risk):
    risk_id = "ios-feature-05-risk-03"
    feature_id = "feature-05"
    name = "Bypass jailbreak detection"
    requires_device = False
    automation_available = False
