"""
Drives the accessibility attacker app used for android-feature-05-risk-01 over adb: prepares a run for a target app
and reads what its service recorded. Installing it and switching its service is shared in accessibility_service.
"""

from __future__ import annotations

import json
import logging
import shlex
import time
from dataclasses import dataclass
from pathlib import Path

from mobile_playbook.platforms.android.device.accessibility_service import (
    AccessibilityAttackerError,
    AccessibilityServiceControl,
    ServiceApp,
)
from mobile_playbook.platforms.android.device.adb import AdbClient

logger = logging.getLogger(__name__)

ATTACKER_PACKAGE = "com.mobileplaybook.accessibility"
# Matches android:versionName in the attacker app's manifest; bump both together so devices get the new build.
ATTACKER_VERSION = "1"
ATTACKER_APK_NAME = "F5R1_Accessibility_Attacker.apk"
ATTACKER_LABEL = "Playbook Accessibility Test"
ATTACKER_APP = ServiceApp(
    package=ATTACKER_PACKAGE, version=ATTACKER_VERSION, apk_name=ATTACKER_APK_NAME, label=ATTACKER_LABEL,
    service_class=f"{ATTACKER_PACKAGE}.AttackAccessibilityService",
)
MAIN_ACTIVITY = f"{ATTACKER_PACKAGE}/.MainActivity"
SERVICE_COMPONENT = ATTACKER_APP.component
SERVICE_SHORT_COMPONENT = ATTACKER_APP.short_component
OUTPUT_DIR = f"/sdcard/Android/data/{ATTACKER_PACKAGE}/files"
STATUS_FILE = "status.json"
EVENTS_FILE = "events.json"
OPTIONAL_EVENT_FIELDS = ("package", "strategy", "pattern", "view_id", "class_name", "text", "content_description")
POLL_SECONDS = 0.5


@dataclass(frozen=True)
class AccessibilityStatus:
    """The attacker app's last reported state, its target and how often it scanned, clicked and succeeded."""

    state: str
    detail: str
    updated_at: str | None
    target_package: str | None
    service_connected: bool
    scan_count: int
    click_count: int
    success_count: int


@dataclass(frozen=True)
class AttackEvent:
    """One record from the attacker's events.json; click and success records carry the extra node or match fields."""

    timestamp: str
    event: str
    package: str | None
    message: str
    strategy: str | None = None
    pattern: str | None = None
    view_id: str | None = None
    class_name: str | None = None
    text: str | None = None
    content_description: str | None = None


# Return the attacker APK to install: the configured path, resolved against the repository root, or the intake copy.
def attacker_apk_path(configured: str | None = None) -> Path:
    return ATTACKER_APP.apk_path(configured)


# Report whether a component list names the attacker's service, in either the full or the short form.
def names_attacker_service(components: list[str]) -> bool:
    return ATTACKER_APP.names_service(components)


class AccessibilityAttacker(AccessibilityServiceControl):
    """Prepares the F5R1 accessibility attacker app for a target and reads its status and event log."""

    # Keep the adb client and the APK to install when the device lacks the current attacker app.
    def __init__(self, adb: AdbClient, apk: Path):
        super().__init__(adb, apk, ATTACKER_APP)

    # Clear earlier output and point the attacker at the target package, returning the status it writes for the run.
    def prepare(self, target_package: str, timeout: float = 15) -> AccessibilityStatus:
        # Removed first so a status left by an earlier run cannot satisfy the wait below.
        self.adb.run(["shell", "rm", "-f", f"{OUTPUT_DIR}/{STATUS_FILE}", f"{OUTPUT_DIR}/{EVENTS_FILE}"])
        # No -S: force-stopping the app makes Android drop its service from the enabled list.
        self.adb.run([
            "shell", "am", "start", "-n", MAIN_ACTIVITY,
            "--es", "target_package", shlex.quote(target_package), "--ez", "reset", "true",
        ])
        deadline = time.monotonic() + timeout
        while True:
            status = self.status()
            if status is not None and status.target_package == target_package:
                return status
            if time.monotonic() >= deadline:
                seen = f"target {status.target_package!r}" if status else "no status"
                raise AccessibilityAttackerError(f"the accessibility attacker app was not prepared for {target_package} within {timeout:g}s (last: {seen})")
            time.sleep(POLL_SECONDS)

    # Return the attacker app's current status, or None when it has not written one.
    def status(self) -> AccessibilityStatus | None:
        data = self._read_json(STATUS_FILE)
        if not isinstance(data, dict) or not isinstance(data.get("state"), str):
            return None
        return AccessibilityStatus(
            state=data["state"],
            detail=str(data.get("detail") or ""),
            updated_at=data.get("updated_at"),
            target_package=data.get("target_package") or None,
            service_connected=data.get("service_connected") is True,
            scan_count=int(data.get("scan_count") or 0),
            click_count=int(data.get("click_count") or 0),
            success_count=int(data.get("success_count") or 0),
        )

    # Return the recorded events, oldest first; an absent file means nothing was recorded.
    def events(self) -> list[AttackEvent]:
        data = self._read_json(EVENTS_FILE)
        if not isinstance(data, list):
            return []
        records = [_event(item) for item in data if isinstance(item, dict) and isinstance(item.get("event"), str)]
        logger.debug("android accessibility attacker: %s event(s)", len(records))
        return records

    # Poll the status until its state is one of the expected ones, and return it.
    def wait_for_state(self, states: set[str] | frozenset[str], timeout: float) -> AccessibilityStatus:
        deadline = time.monotonic() + timeout
        while True:
            status = self.status()
            if status is not None and status.state in states:
                return status
            if time.monotonic() >= deadline:
                seen = status.state if status else "no status"
                raise AccessibilityAttackerError(f"the accessibility attacker app did not reach {sorted(states)} within {timeout:g}s (last: {seen})")
            time.sleep(POLL_SECONDS)

    # Read one of the app's JSON output files over adb, falling back to run-as, or None when it is absent or unreadable.
    def _read_json(self, name: str) -> dict | list | None:
        path = f"{OUTPUT_DIR}/{name}"
        for command in (["shell", "cat", path], ["shell", "run-as", ATTACKER_PACKAGE, "cat", path]):
            code, out, _ = self.adb.run(command)
            text = (out or "").strip()
            if code != 0 or not text.startswith(("{", "[")):
                continue
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                logger.debug("android accessibility attacker: %s is not valid JSON yet", name)
        return None


# Build an event from one events.json record, keeping absent optional fields as None.
def _event(item: dict) -> AttackEvent:
    optional = {key: None if item.get(key) is None else str(item[key]) for key in OPTIONAL_EVENT_FIELDS}
    return AttackEvent(
        timestamp=str(item.get("timestamp") or ""),
        event=item["event"],
        message=str(item.get("message") or ""),
        **optional,
    )
