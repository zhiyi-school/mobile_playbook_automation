"""
ios-feature-05-risk-01: manual-only; testers follow the playbook demonstration on a jailbroken device.
"""

from __future__ import annotations

from mobile_playbook.platforms.ios.risks.base import Risk


class Feature05Risk01(Risk):
    risk_id = "ios-feature-05-risk-01"
    feature_id = "feature-05"
    name = "Access app sandbox data"
    requires_device = False
    automation_available = False
