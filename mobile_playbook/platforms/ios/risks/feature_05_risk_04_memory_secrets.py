"""
ios-feature-05-risk-04: manual-only; testers follow the playbook demonstration on a jailbroken device.
"""

from __future__ import annotations

from mobile_playbook.platforms.ios.risks.base import Risk


class Feature05Risk04(Risk):
    risk_id = "ios-feature-05-risk-04"
    feature_id = "feature-05"
    name = "Extract secrets from memory"
    requires_device = False
    automation_available = False
