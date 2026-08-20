from __future__ import annotations

import pytest

from mobile_playbook.platforms.android.risks.screen_capture import (
    _security_verdict_from_verdict,
    _status_from_verdict,
)


@pytest.mark.parametrize(
    "verdict,expected_status,expected_security_verdict",
    [
        ("ALLOWED (visible, unprotected)", "SCREEN_CAPTURE_ALLOWED", "At Risk"),
        ("BLOCKED (FLAG_SECURE set)", "SCREEN_CAPTURE_BLOCKED", "Reduced Risk"),
        ("ERROR (driver failure)", "FAILED", "Inconclusive"),
        ("SOMETHING_UNEXPECTED", "UNKNOWN", "Inconclusive"),
    ],
)
def test_screen_capture_status_and_security_verdict_stay_in_sync(verdict, expected_status, expected_security_verdict):
    assert _status_from_verdict(verdict) == expected_status
    assert _security_verdict_from_verdict(verdict) == expected_security_verdict
