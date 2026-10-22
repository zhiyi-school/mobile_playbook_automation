"""
Drives the accessibility attacker app used for android-feature-05-risk-01 over adb: installs it, prepares a run for
a target app, reads what its service recorded, and turns its service on and off through secure settings.
"""

from __future__ import annotations

import json
import logging
import re
import shlex
import time
from dataclasses import dataclass
from pathlib import Path

from mobile_playbook.common.storage_paths import REPOSITORY_ROOT, android_intake_dir
from mobile_playbook.platforms.android.device.adb import AdbClient
from mobile_playbook.platforms.android.device.app_version import installed_version
from mobile_playbook.platforms.android.device.install import install_apks
from mobile_playbook.platforms.android.device.phone_role import uninstall

logger = logging.getLogger(__name__)

ATTACKER_PACKAGE = "com.mobileplaybook.accessibility"
# Matches android:versionName in the attacker app's manifest; bump both together so devices get the new build.
ATTACKER_VERSION = "1"
ATTACKER_APK_NAME = "F5R1_Accessibility_Attacker.apk"
ATTACKER_LABEL = "Playbook Accessibility Test"
MAIN_ACTIVITY = f"{ATTACKER_PACKAGE}/.MainActivity"
SERVICE_COMPONENT = f"{ATTACKER_PACKAGE}/{ATTACKER_PACKAGE}.AttackAccessibilityService"
SERVICE_SHORT_COMPONENT = f"{ATTACKER_PACKAGE}/.AttackAccessibilityService"
OUTPUT_DIR = f"/sdcard/Android/data/{ATTACKER_PACKAGE}/files"
STATUS_FILE = "status.json"
EVENTS_FILE = "events.json"
ENABLED_SERVICES_KEY = "enabled_accessibility_services"
ACCESSIBILITY_ENABLED_KEY = "accessibility_enabled"
OPTIONAL_EVENT_FIELDS = ("package", "strategy", "pattern", "view_id", "class_name", "text", "content_description")
BOUND_SERVICES_RE =re.compile(r"^\s*Bound services:\{(.*)\}\s*$", re.MULTILINE)
POLL_SECONDS = 0.5


class AccessibilityAttackerError(Exception):
    """The attacker app could not be installed or prepared, or its service settings could not be changed."""


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


@dataclass(frozen=True)
class AccessibilitySettings:
    """The secure settings that turn accessibility services on, as read before a test; None means the key is unset."""

    enabled_services: str | None
    accessibility_enabled: str | None


# Return the attacker APK's place in the Android intake folder.
def default_attacker_apk() -> Path:
    return android_intake_dir() / ATTACKER_APK_NAME


# Return the attacker APK to install: the configured path, resolved against the repository root, or the intake copy.
def attacker_apk_path(configured: str | None = None) -> Path:
    if not configured:
        return default_attacker_apk()
    path = Path(configured)
    return path if path.is_absolute() else REPOSITORY_ROOT / path


# Split a colon-separated enabled-services value into its component names.
def enabled_components(value: str | None) -> list[str]:
    return [component for component in (value or "").split(":") if component]


# Report whether a component list names the attacker's service, in either the full or the short form.
def names_attacker_service(components: list[str]) -> bool:
    return any(component in (SERVICE_COMPONENT, SERVICE_SHORT_COMPONENT) for component in components)


class AccessibilityAttacker:
    """Installs, prepares, reads and stops the accessibility attacker app, and switches its service over adb."""

    # Keep the adb client and the APK to install when the device lacks the current attacker app.
    def __init__(self, adb: AdbClient, apk: Path):
        self.adb = adb
        self.apk = apk

    # Install the attacker app unless the device already has this version, leaving its service off.
    def ensure_installed(self) -> None:
        installed = installed_version(self.adb, ATTACKER_PACKAGE)
        if installed is not None and installed.version_name == ATTACKER_VERSION:
            logger.debug("android accessibility attacker: version %s already installed", ATTACKER_VERSION)
            return
        if not self.apk.is_file():
            raise AccessibilityAttackerError(f"accessibility attacker APK not found at {self.apk}")
        installed_ok, reason = install_apks(self.adb, [self.apk], grant_permissions=False)
        logger.debug("android accessibility attacker: installed %s ok=%s (%s)", self.apk, installed_ok, reason)
        if not installed_ok:
            raise AccessibilityAttackerError(f"could not install the accessibility attacker app: {reason}")

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

    # Read the accessibility secure settings so they can be restored exactly after the test.
    def read_settings(self) -> AccessibilitySettings:
        return AccessibilitySettings(
            enabled_services=self._get_setting(ENABLED_SERVICES_KEY),
            accessibility_enabled=self._get_setting(ACCESSIBILITY_ENABLED_KEY),
        )

    # Add the attacker's service to the enabled services already recorded and turn accessibility on.
    def enable_via_secure_settings(self, before: AccessibilitySettings) -> None:
        components = enabled_components(before.enabled_services)
        if not names_attacker_service(components):
            components.append(SERVICE_COMPONENT)
        self._put_setting(ENABLED_SERVICES_KEY, ":".join(components))
        self._put_setting(ACCESSIBILITY_ENABLED_KEY, "1")

    # Put both settings back to the recorded values, deleting keys that were unset, and confirm the device has them.
    def restore_settings(self, before: AccessibilitySettings) -> None:
        for key, value in ((ENABLED_SERVICES_KEY, before.enabled_services), (ACCESSIBILITY_ENABLED_KEY, before.accessibility_enabled)):
            if value is None:
                self._run_setting(["delete", "secure", key])
            else:
                self._put_setting(key, value)
        after = self.read_settings()
        if (after.enabled_services or "") != (before.enabled_services or "") or (after.accessibility_enabled or "") != (before.accessibility_enabled or ""):
            raise AccessibilityAttackerError(f"accessibility settings were not restored: expected {before}, found {after}")

    # Report whether the enabled-services setting names the attacker's service.
    def is_enabled(self) -> bool:
        return names_attacker_service(enabled_components(self._get_setting(ENABLED_SERVICES_KEY)))

    # Report whether Android has bound the attacker's service; the dump lists bound services by label, not component.
    def is_bound(self) -> bool:
        code, out, err = self.adb.run(["shell", "dumpsys", "accessibility"])
        if code != 0:
            raise AccessibilityAttackerError(f"adb dumpsys accessibility failed: {err or out}")
        return any(f"label={ATTACKER_LABEL}," in match or f"label={ATTACKER_LABEL}]" in match for match in BOUND_SERVICES_RE.findall(out or ""))

    # Uninstall the attacker app, removing its output with it, and raise unless the phone confirms it is gone.
    def uninstall(self) -> None:
        if not uninstall(self.adb, ATTACKER_PACKAGE):
            raise AccessibilityAttackerError(f"{ATTACKER_PACKAGE} is still installed")

    # Stop the attacker app; Android also drops a force-stopped app's service from the enabled list.
    def stop(self) -> None:
        self.adb.run(["shell", "am", "force-stop", ATTACKER_PACKAGE])

    # Read a secure setting, treating Android's "null" as unset.
    def _get_setting(self, key: str) -> str | None:
        code, out, err = self.adb.run(["shell", "settings", "get", "secure", key])
        if code != 0:
            raise AccessibilityAttackerError(f"adb settings get secure {key} failed: {err or out}")
        value = (out or "").strip()
        return None if value == "null" else value

    # Write a secure setting; values are quoted because adb joins shell arguments into one command line.
    def _put_setting(self, key: str, value: str) -> None:
        self._run_setting(["put", "secure", key, shlex.quote(value)])

    # Run a settings command and raise when the device rejects it.
    def _run_setting(self, args: list[str]) -> None:
        code, out, err = self.adb.run(["shell", "settings", *args])
        if code != 0 or "Bad arguments" in (out or "") or "Bad arguments" in (err or ""):
            raise AccessibilityAttackerError(f"adb settings {' '.join(args)} failed: {err or out}")

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
