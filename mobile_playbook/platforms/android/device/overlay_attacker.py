"""
Drives the overlay attacker app used for android-feature-04-risk-01 over adb: installs it, starts a fresh session for
a target app, reads what its overlay recorded, and stops it.
"""

from __future__ import annotations

import json
import logging
import shlex
import time
from dataclasses import dataclass
from pathlib import Path

from mobile_playbook.common.storage_paths import REPOSITORY_ROOT, android_intake_dir
from mobile_playbook.platforms.android.device.adb import AdbClient
from mobile_playbook.platforms.android.device.app_version import installed_version
from mobile_playbook.platforms.android.device.install import install_apks

logger = logging.getLogger(__name__)

ATTACKER_PACKAGE = "com.mobileplaybook.overlay"
# Matches android:versionName in the attacker app's manifest; bump both together so devices get the new build.
ATTACKER_VERSION = "1"
ATTACKER_APK_NAME = "F4_Overlay_Attacker.apk"
MAIN_ACTIVITY = f"{ATTACKER_PACKAGE}/.MainActivity"
OUTPUT_DIR = f"/sdcard/Android/data/{ATTACKER_PACKAGE}/files"
STATUS_FILE = "status.json"
CAPTURED_FILE = "captured.json"
USERNAME_FIELD_ID = f"{ATTACKER_PACKAGE}:id/overlay_username"
PASSWORD_FIELD_ID = f"{ATTACKER_PACKAGE}:id/overlay_password"
SIGN_IN_BUTTON_ID = f"{ATTACKER_PACKAGE}:id/overlay_sign_in"
TITLE_ID = f"{ATTACKER_PACKAGE}:id/overlay_title"
STARTED_STATES = frozenset({"permission_missing", "service_started"})
POLL_SECONDS = 0.5


class OverlayAttackerError(Exception):
    """The attacker app could not be installed, or did not answer in time."""


@dataclass(frozen=True)
class OverlayStatus:
    """The attacker app's last reported state and how often it has added its overlay."""

    state: str
    detail: str
    updated_at: str | None
    overlay_shown_count: int
    last_overlay_shown_at: str | None


@dataclass(frozen=True)
class CapturedCredential:
    """One submission typed into the overlay's sign-in form."""

    timestamp: str
    username: str
    password: str


# Return the attacker APK's place in the Android intake folder.
def default_attacker_apk() -> Path:
    return android_intake_dir() / ATTACKER_APK_NAME


# Return the attacker APK to install: the configured path, resolved against the repository root, or the intake copy.
def attacker_apk_path(configured: str | None = None) -> Path:
    if not configured:
        return default_attacker_apk()
    path = Path(configured)
    return path if path.is_absolute() else REPOSITORY_ROOT / path


class OverlayAttacker:
    """Installs, starts, reads and stops the overlay attacker app over adb."""

    # Keep the adb client and the APK to install when the device lacks the current attacker app.
    def __init__(self, adb: AdbClient, apk: Path):
        self.adb = adb
        self.apk = apk

    # Install the attacker app unless the device already has this version, leaving its overlay permission alone.
    def ensure_installed(self) -> None:
        installed = installed_version(self.adb, ATTACKER_PACKAGE)
        if installed is not None and installed.version_name == ATTACKER_VERSION:
            logger.debug("android overlay attacker: version %s already installed", ATTACKER_VERSION)
            return
        if not self.apk.is_file():
            raise OverlayAttackerError(f"overlay attacker APK not found at {self.apk}")
        # Display over other apps is granted through Settings by the test itself, as the playbook does.
        installed_ok, reason = install_apks(self.adb, [self.apk], grant_permissions=False)
        logger.debug("android overlay attacker: installed %s ok=%s (%s)", self.apk, installed_ok, reason)
        if not installed_ok:
            raise OverlayAttackerError(f"could not install the overlay attacker app: {reason}")

    # Start a fresh attacker session that titles its overlay after the target, clearing earlier output, and return
    # its first state: permission_missing until Display over other apps is allowed, then service_started.
    def start(self, target_label: str, timeout: float = 15) -> OverlayStatus:
        # Removed first so a status left by an earlier session cannot satisfy the wait below.
        self.adb.run(["shell", "rm", "-f", f"{OUTPUT_DIR}/{STATUS_FILE}", f"{OUTPUT_DIR}/{CAPTURED_FILE}"])
        # adb joins shell arguments into one command line, so the label is quoted to stay a single argument.
        self.adb.run([
            "shell", "am", "start", "-S", "-n", MAIN_ACTIVITY,
            "--es", "target_label", shlex.quote(target_label), "--ez", "reset", "true",
        ])
        return self.wait_for_state(STARTED_STATES, timeout)

    # Return the attacker app's current status, or None when it has not written one.
    def status(self) -> OverlayStatus | None:
        data = self._read_json(STATUS_FILE)
        if not isinstance(data, dict) or not isinstance(data.get("state"), str):
            return None
        return OverlayStatus(
            state=data["state"],
            detail=str(data.get("detail") or ""),
            updated_at=data.get("updated_at"),
            overlay_shown_count=int(data.get("overlay_shown_count") or 0),
            last_overlay_shown_at=data.get("last_overlay_shown_at"),
        )

    # Return the submissions the overlay recorded, oldest first; an absent file means nothing was typed.
    def captured(self) -> list[CapturedCredential]:
        data = self._read_json(CAPTURED_FILE)
        if not isinstance(data, list):
            return []
        records = [
            CapturedCredential(timestamp=str(item.get("timestamp") or ""), username=str(item.get("username") or ""), password=str(item.get("password") or ""))
            for item in data
            if isinstance(item, dict)
        ]
        logger.debug("android overlay attacker: %s captured record(s)", len(records))
        return records

    # Poll the status until its state is one of the expected ones, and return it.
    def wait_for_state(self, states: set[str] | frozenset[str], timeout: float) -> OverlayStatus:
        deadline = time.monotonic() + timeout
        while True:
            status = self.status()
            if status is not None and status.state in states:
                return status
            if time.monotonic() >= deadline:
                seen = status.state if status else "no status"
                raise OverlayAttackerError(f"the overlay attacker app did not reach {sorted(states)} within {timeout:g}s (last: {seen})")
            time.sleep(POLL_SECONDS)

    # Stop the attacker app, which also removes any overlay it is showing.
    def stop(self) -> None:
        self.adb.run(["shell", "am", "force-stop", ATTACKER_PACKAGE])

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
                logger.debug("android overlay attacker: %s is not valid JSON yet", name)
        return None
