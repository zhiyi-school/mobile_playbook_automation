"""Describes Event Spy, the accessibility attacker app used for android-feature-05-risk-02, and drives it over adb."""

from __future__ import annotations

from pathlib import Path

from mobile_playbook.platforms.android.device.accessibility_service import ServiceApp
from mobile_playbook.platforms.android.device.adb import AdbClient
from mobile_playbook.platforms.android.device.captured_events import CapturedEventsApp

EVENT_SPY_PACKAGE = "com.malicious.eventspy"
EVENT_SPY_APP = ServiceApp(
    package=EVENT_SPY_PACKAGE, version="1.0", apk_name="F5R2_Event_Capture_Attacker.apk", label="Event Spy",
    service_class=f"{EVENT_SPY_PACKAGE}.EventListenerAccessibilityService",
)


class EventSpy(CapturedEventsApp):
    """Installs, switches, clears and reads Event Spy, the playbook's accessibility event capture app."""

    # Keep the adb client and the APK to install when the device lacks the current Event Spy build.
    def __init__(self, adb: AdbClient, apk: Path):
        super().__init__(adb, apk, EVENT_SPY_APP)
