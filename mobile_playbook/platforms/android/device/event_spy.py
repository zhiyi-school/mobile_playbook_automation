"""
Drives Event Spy, the accessibility attacker app used for android-feature-05-risk-02, over adb: clears what it has
captured and reads the events its service saved to its Room database, which its debuggable build exposes to run-as.
"""

from __future__ import annotations

import logging
import shlex
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path

from mobile_playbook.platforms.android.device.accessibility_service import (
    AccessibilityAttackerError,
    AccessibilityServiceControl,
    ServiceApp,
)
from mobile_playbook.platforms.android.device.adb import AdbClient
from mobile_playbook.platforms.android.device.run_as import RunAsShell, RunAsUnavailable, run_as_shell

logger = logging.getLogger(__name__)

EVENT_SPY_PACKAGE = "com.malicious.eventspy"
EVENT_SPY_APP = ServiceApp(
    package=EVENT_SPY_PACKAGE, version="1.0", apk_name="F5R2_Event_Capture_Attacker.apk", label="Event Spy",
    service_class=f"{EVENT_SPY_PACKAGE}.EventListenerAccessibilityService",
)
# Relative to the app's data directory, where run-as starts.
DATABASE = "databases/event_database"
COPY_TIMEOUT = 60
EVENTS_QUERY = (
    "SELECT id, timestamp, eventType, packageName, activityName, viewId, text, beforeText, contentDescription, hint, "
    "details FROM captured_events"
)


@dataclass(frozen=True)
class CapturedEvent:
    """One row of Event Spy's captured_events table: a focus, text change or click it saw in an app."""

    id: int
    # Milliseconds since the epoch on the phone's clock.
    timestamp: int
    event_type: str
    package: str
    activity: str | None
    view_id: str | None
    text: str | None
    before_text: str | None
    content_description: str | None
    hint: str | None
    details: str | None


class EventSpy(AccessibilityServiceControl):
    """Installs, switches, clears and reads Event Spy, the playbook's accessibility event capture app."""

    # Keep the adb client and the APK to install when the device lacks the current Event Spy build.
    def __init__(self, adb: AdbClient, apk: Path):
        super().__init__(adb, apk, EVENT_SPY_APP)

    # Delete everything Event Spy has captured, as its Clear Logs button does; pm clear also stops the app.
    def clear(self) -> None:
        code, out, err = self.adb.run(["shell", "pm", "clear", EVENT_SPY_PACKAGE])
        if code != 0 or "Success" not in (out or ""):
            raise AccessibilityAttackerError(f"could not clear Event Spy's data: {(err or out or '').strip()}")

    # Return the captured events in the order they were saved, only the package's when one is given; an absent
    # database means nothing was captured.
    def events(self, package: str | None = None) -> list[CapturedEvent]:
        try:
            shell = run_as_shell(self.adb, EVENT_SPY_PACKAGE)
        except RunAsUnavailable as exc:
            raise AccessibilityAttackerError(f"Event Spy's database cannot be read with run-as: {exc}") from exc
        with tempfile.TemporaryDirectory(prefix="event_spy_") as folder:
            local = Path(folder) / "event_database"
            if not self._copy(shell, DATABASE, local):
                return []
            # Room saves new rows to the write-ahead log first; SQLite applies it when the copy is opened.
            self._copy(shell, f"{DATABASE}-wal", local.with_name("event_database-wal"))
            events = _read_events(local, package)
        logger.debug("android event spy: %s event(s) for %s", len(events), package or "all packages")
        return events

    # Copy one database file out of the app's data directory, returning False when the app has not created it.
    def _copy(self, shell: RunAsShell, path: str, destination: Path) -> bool:
        code, _, _ = shell.run(f"test -f {shlex.quote(path)}")
        if code != 0:
            return False
        code, err = self.adb.run_to_file(["shell", "-T", "run-as", EVENT_SPY_PACKAGE, "cat", path], destination, timeout=COPY_TIMEOUT)
        if code != 0:
            raise AccessibilityAttackerError(f"could not copy Event Spy's {path}: {err or f'exit {code}'}")
        return True


# Read the captured events from a local copy of Event Spy's database.
def _read_events(database: Path, package: str | None) -> list[CapturedEvent]:
    query, parameters = (f"{EVENTS_QUERY} WHERE packageName = ? ORDER BY id", (package,)) if package else (f"{EVENTS_QUERY} ORDER BY id", ())
    try:
        connection = sqlite3.connect(database)
        try:
            rows = connection.execute(query, parameters).fetchall()
        finally:
            connection.close()
    except sqlite3.DatabaseError as exc:
        raise AccessibilityAttackerError(f"Event Spy's database could not be read: {exc}") from exc
    return [CapturedEvent(*row) for row in rows]
