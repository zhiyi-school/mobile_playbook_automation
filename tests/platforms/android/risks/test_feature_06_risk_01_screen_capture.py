from __future__ import annotations

import pytest

from mobile_playbook.platforms.android.risks.feature_06_risk_01_screen_capture import (
    AndroidScreenCaptureRisk,
    _security_verdict_from_verdict,
    _status_from_verdict,
)


@pytest.mark.parametrize(
    "verdict,expected_status,expected_security_verdict",
    [
        ("ALLOWED (visible, unprotected)", "SCREEN_CAPTURE_ALLOWED", "At Risk"),
        ("BLOCKED (FLAG_SECURE set)", "SCREEN_CAPTURE_BLOCKED", "Reduced Risk"),
        ("ERROR (driver failure)", "FAILED", "Inconclusive"),
        ("INCONCLUSIVE (Redirected to the Play Store)", "SCREEN_CAPTURE_NOT_TESTED", "Inconclusive"),
        ("SOMETHING_UNEXPECTED", "UNKNOWN", "Inconclusive"),
    ],
)
def test_screen_capture_status_and_security_verdict_stay_in_sync(verdict, expected_status, expected_security_verdict):
    assert _status_from_verdict(verdict) == expected_status
    assert _security_verdict_from_verdict(verdict) == expected_security_verdict


class ForegroundDriver:
    def __init__(self, current_package: str):
        self.current_package = current_package
        self.terminated: list[str] = []

    def terminate_app(self, package: str) -> None:
        self.terminated.append(package)


@pytest.mark.parametrize(
    ("foreground", "expected"),
    [
        ("com.android.vending", "INCONCLUSIVE"),
        ("com.android.chrome", "BLOCKED (Redirected to Browser"),
        ("com.google.android.apps.nexuslauncher", "BLOCKED (App exited to Home Screen"),
        ("com.huawei.android.launcher", "BLOCKED (App exited to Home Screen"),
        ("com.example.other", "BLOCKED (Switched to unknown app"),
        ("com.example.app", "Unknown"),
    ],
)
def test_only_a_real_hand_off_counts_as_blocking_screen_capture(foreground, expected):
    driver = ForegroundDriver(foreground)

    verdict = AndroidScreenCaptureRisk()._check_app_switch(driver, "com.example.app")

    assert verdict.startswith(expected)


def test_a_play_store_redirect_is_inconclusive_not_reduced_risk():
    verdict = AndroidScreenCaptureRisk()._check_app_switch(ForegroundDriver("com.android.vending"), "com.example.app")

    assert _security_verdict_from_verdict(verdict) == "Inconclusive"
    assert _status_from_verdict(verdict) == "SCREEN_CAPTURE_NOT_TESTED"
