"""
Installs, switches and removes an attacker app's accessibility service over adb, for the feature-05 risks: reads and
restores the accessibility secure settings and checks whether Android has enabled and bound the service.
"""

from __future__ import annotations

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

ENABLED_SERVICES_KEY = "enabled_accessibility_services"
ACCESSIBILITY_ENABLED_KEY = "accessibility_enabled"
BOUND_SERVICES_RE = re.compile(r"^\s*Bound services:\{(.*)\}\s*$", re.MULTILINE)
POLL_SECONDS = 0.5
UNBIND_SECONDS = 5.0


class AccessibilityAttackerError(Exception):
    """The attacker app could not be installed or prepared, or its service settings could not be changed."""


@dataclass(frozen=True)
class ServiceApp:
    """An attacker app that provides an accessibility service: its package, version, intake APK, label and service."""

    package: str
    # Matches android:versionName in the app's manifest; a device with another version gets the intake APK installed.
    version: str
    apk_name: str
    # The service's name in Accessibility settings and in `dumpsys accessibility`.
    label: str
    service_class: str

    # Return the service's full component name, as Android writes it to the enabled-services setting.
    @property
    def component(self) -> str:
        return f"{self.package}/{self.service_class}"

    # Return the component name with the class relative to the package, the form Settings may write instead.
    @property
    def short_component(self) -> str:
        prefix = f"{self.package}."
        if not self.service_class.startswith(prefix):
            return self.component
        return f"{self.package}/.{self.service_class.removeprefix(prefix)}"

    # Return the APK to install: the configured path, resolved against the repository root, or the intake copy.
    def apk_path(self, configured: str | None = None) -> Path:
        if not configured:
            return android_intake_dir() / self.apk_name
        path = Path(configured)
        return path if path.is_absolute() else REPOSITORY_ROOT / path

    # Report whether a component list names this app's service, in either the full or the short form.
    def names_service(self, components: list[str]) -> bool:
        return any(component in (self.component, self.short_component) for component in components)


@dataclass(frozen=True)
class AccessibilitySettings:
    """The secure settings that turn accessibility services on, as read before a test; None means the key is unset."""

    enabled_services: str | None
    accessibility_enabled: str | None


# Split a colon-separated enabled-services value into its component names.
def enabled_components(value: str | None) -> list[str]:
    return [component for component in (value or "").split(":") if component]


class AccessibilityServiceControl:
    """Installs and removes an attacker app and turns its accessibility service on and off through secure settings."""

    # Keep the adb client, the APK to install when the device lacks the app's current version, and the app itself.
    def __init__(self, adb: AdbClient, apk: Path, app: ServiceApp):
        self.adb = adb
        self.apk = apk
        self.app = app

    # Install the attacker app unless the device already has this version, leaving its service off.
    def ensure_installed(self) -> None:
        installed = installed_version(self.adb, self.app.package)
        if installed is not None and installed.version_name == self.app.version:
            logger.debug("android accessibility attacker: %s version %s already installed", self.app.package, self.app.version)
            return
        if not self.apk.is_file():
            raise AccessibilityAttackerError(f"accessibility attacker APK not found at {self.apk}")
        installed_ok, reason = install_apks(self.adb, [self.apk], grant_permissions=False)
        logger.debug("android accessibility attacker: installed %s ok=%s (%s)", self.apk, installed_ok, reason)
        if not installed_ok:
            raise AccessibilityAttackerError(f"could not install the accessibility attacker app: {reason}")

    # Read the accessibility secure settings so they can be restored exactly after the test.
    def read_settings(self) -> AccessibilitySettings:
        return AccessibilitySettings(
            enabled_services=self._get_setting(ENABLED_SERVICES_KEY),
            accessibility_enabled=self._get_setting(ACCESSIBILITY_ENABLED_KEY),
        )

    # Add the attacker's service to the enabled services already recorded and turn accessibility on.
    def enable_via_secure_settings(self, before: AccessibilitySettings) -> None:
        components = enabled_components(before.enabled_services)
        if not self.app.names_service(components):
            components.append(self.app.component)
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
        return self.app.names_service(enabled_components(self._get_setting(ENABLED_SERVICES_KEY)))

    # Report whether Android has bound the attacker's service; the dump lists bound services by label, not component.
    def is_bound(self) -> bool:
        code, out, err = self.adb.run(["shell", "dumpsys", "accessibility"])
        if code != 0:
            raise AccessibilityAttackerError(f"adb dumpsys accessibility failed: {err or out}")
        label = self.app.label
        return any(f"label={label}," in match or f"label={label}]" in match for match in BOUND_SERVICES_RE.findall(out or ""))

    # Wait for Android to bind the service, which it does only while Appium leaves accessibility services running.
    def wait_until_bound(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            if self.is_bound():
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(POLL_SECONDS)

    # Uninstall the attacker app, removing its output with it, and raise unless the phone confirms it is gone.
    def uninstall(self) -> None:
        if not uninstall(self.adb, self.app.package):
            raise AccessibilityAttackerError(f"{self.app.package} is still installed")

    # Stop the attacker app; Android also drops a force-stopped app's service from the enabled list.
    def stop(self) -> None:
        self.adb.run(["shell", "am", "force-stop", self.app.package])

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


# Restore the recorded accessibility settings, stop the app and confirm its service is no longer enabled or bound,
# recording in cleanup what worked and why anything failed.
def turn_service_off(control: AccessibilityServiceControl, before: AccessibilitySettings | None, cleanup: dict) -> None:
    if before is not None:
        try:
            control.restore_settings(before)
            cleanup["settings_restored"] = True
        except Exception as exc:
            logger.debug("android accessibility: restoring settings failed: %s", exc, exc_info=True)
            cleanup["errors"].append(f"restoring settings failed: {exc}")
    else:
        cleanup["settings_restored"] = True
    # After restoring: Android drops a force-stopped app's service from the enabled list, which restoring would undo.
    try:
        control.stop()
    except Exception as exc:
        logger.debug("android accessibility: stopping the attacker failed: %s", exc, exc_info=True)
        cleanup["errors"].append(f"stopping the attacker failed: {exc}")
    deadline = time.monotonic() + UNBIND_SECONDS
    try:
        while True:
            cleanup["service_off"] = not control.is_enabled() and not control.is_bound()
            if cleanup["service_off"] or time.monotonic() >= deadline:
                break
            time.sleep(POLL_SECONDS)
        if not cleanup["service_off"]:
            cleanup["errors"].append("the service is still enabled or bound")
    except Exception as exc:
        logger.debug("android accessibility: checking the service failed: %s", exc, exc_info=True)
        cleanup["errors"].append(f"checking the service failed: {exc}")


# Uninstall the attacker app so it does not stay on the phone after the test, recording the outcome in cleanup.
def remove_attacker_app(control: AccessibilityServiceControl, cleanup: dict) -> None:
    try:
        control.uninstall()
        cleanup["attacker_uninstalled"] = True
    except Exception as exc:
        logger.debug("android accessibility: uninstalling the attacker failed: %s", exc, exc_info=True)
        cleanup["errors"].append(f"uninstalling the attacker failed: {exc}")
