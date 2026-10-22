"""
Drives the feature-05 attacker apps that save what their accessibility service sees to a Room captured_events table:
clears what they have captured and reads their database over adb, which their debuggable builds expose to run-as.
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
)
from mobile_playbook.platforms.android.device.run_as import RunAsShell, RunAsUnavailable, run_as_shell

logger = logging.getLogger(__name__)

# Relative to the app's data directory, where run-as starts.
DATABASE = "databases/event_database"
COPY_TIMEOUT = 60
EVENTS_QUERY = (
    "SELECT id, timestamp, eventType, packageName, activityName, viewId, text, beforeText, contentDescription, hint, "
    "details FROM captured_events"
)


@dataclass(frozen=True)
class CapturedEvent:
    """One row of an attacker app's captured_events table: something its service saw in an app."""

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


class CapturedEventsApp(AccessibilityServiceControl):
    """Installs, switches, clears and reads an attacker app that saves its captured events to a Room database."""

    # Delete everything the app has captured, as its Clear Logs button does; pm clear also stops the app.
    def clear(self) -> None:
        code, out, err = self.adb.run(["shell", "pm", "clear", self.app.package])
        if code != 0 or "Success" not in (out or ""):
            raise AccessibilityAttackerError(f"could not clear {self.app.label}'s data: {(err or out or '').strip()}")

    # Return the captured events in the order they were saved, only the package's when one is given; an absent
    # database means nothing was captured.
    def events(self, package: str | None = None) -> list[CapturedEvent]:
        try:
            shell = run_as_shell(self.adb, self.app.package)
        except RunAsUnavailable as exc:
            raise AccessibilityAttackerError(f"{self.app.label}'s database cannot be read with run-as: {exc}") from exc
        with tempfile.TemporaryDirectory(prefix="captured_events_") as folder:
            local = Path(folder) / "event_database"
            if not self._copy(shell, DATABASE, local):
                return []
            # Room saves new rows to the write-ahead log first; SQLite applies it when the copy is opened.
            self._copy(shell, f"{DATABASE}-wal", local.with_name("event_database-wal"))
            events = self._read_events(local, package)
        logger.debug("android captured events: %s event(s) from %s for %s", len(events), self.app.package, package or "all packages")
        return events

    # Copy one database file out of the app's data directory, returning False when the app has not created it.
    def _copy(self, shell: RunAsShell, path: str, destination: Path) -> bool:
        code, _, _ = shell.run(f"test -f {shlex.quote(path)}")
        if code != 0:
            return False
        code, err = self.adb.run_to_file(["shell", "-T", "run-as", self.app.package, "cat", path], destination, timeout=COPY_TIMEOUT)
        if code != 0:
            raise AccessibilityAttackerError(f"could not copy {self.app.label}'s {path}: {err or f'exit {code}'}")
        return True

    # Read the captured events from a local copy of the app's database.
    def _read_events(self, database: Path, package: str | None) -> list[CapturedEvent]:
        query, parameters = (f"{EVENTS_QUERY} WHERE packageName = ? ORDER BY id", (package,)) if package else (f"{EVENTS_QUERY} ORDER BY id", ())
        try:
            connection = sqlite3.connect(database)
            try:
                rows = connection.execute(query, parameters).fetchall()
            finally:
                connection.close()
        except sqlite3.DatabaseError as exc:
            raise AccessibilityAttackerError(f"{self.app.label}'s database could not be read: {exc}") from exc
        return [CapturedEvent(*row) for row in rows]
