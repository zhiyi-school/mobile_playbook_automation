"""Reads synthetic copies of Event Spy's Room database through a fake adb that serves the app's files to run-as."""

from __future__ import annotations

import shlex
import sqlite3
from pathlib import Path

import pytest

from mobile_playbook.platforms.android.device.accessibility_service import AccessibilityAttackerError
from mobile_playbook.platforms.android.device.event_spy import (
    DATABASE,
    EVENT_SPY_APP,
    EVENT_SPY_PACKAGE,
    CapturedEvent,
    EventSpy,
)

TARGET = "com.example.bank"
SCHEMA = (
    "CREATE TABLE captured_events (id INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL, timestamp INTEGER NOT NULL, "
    "eventType TEXT NOT NULL, packageName TEXT NOT NULL, activityName TEXT, viewId TEXT, text TEXT, beforeText TEXT, "
    "contentDescription TEXT, hint TEXT, details TEXT)"
)
ROWS = [
    (1000, "CLICK", "com.android.settings", "android.widget.Switch", None, None, None, None, None, "Clicked: Use Event Spy"),
    (2000, "FOCUS", TARGET, "android.widget.EditText", f"{TARGET}:id/card", None, None, None, "Card number", "Field focused: Card number"),
    (3000, "TEXT_CHANGE", TARGET, "android.widget.EditText", f"{TARGET}:id/card", "4111 1111 1111 1111", "", None, "Card number", "Input: 4111 1111 1111 1111"),
]


def insert(connection, rows):
    connection.executemany(
        "INSERT INTO captured_events (timestamp, eventType, packageName, activityName, viewId, text, beforeText, "
        "contentDescription, hint, details) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows,
    )
    connection.commit()


def database_files(folder: Path, rows=ROWS, in_wal=()) -> dict[str, bytes]:
    """Build the app's database files; rows in `in_wal` stay in the write-ahead log, as Room leaves them."""
    path = folder / "event_database"
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute(SCHEMA)
    insert(connection, rows)
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    connection.execute("PRAGMA wal_autocheckpoint=0")
    insert(connection, in_wal)
    files = {DATABASE: path.read_bytes()}
    wal = path.with_name("event_database-wal")
    if wal.exists() and wal.stat().st_size:
        files[f"{DATABASE}-wal"] = wal.read_bytes()
    connection.close()
    return files


class EventSpyAdb:
    """Fake adb serving Event Spy's private files to run-as and answering pm clear."""

    def __init__(self, files=None, debuggable=True, copy_fails=False, clear_output="Success"):
        self.files = dict(files or {})
        self.debuggable = debuggable
        self.copy_fails = copy_fails
        self.clear_output = clear_output
        self.commands: list[list[str]] = []

    def run(self, args, timeout=None):
        self.commands.append(list(args))
        if args == ["shell", "run-as", EVENT_SPY_PACKAGE, "id", "-u"]:
            return (0, "10234\n", "") if self.debuggable else (1, "", f"run-as: package not debuggable: {EVENT_SPY_PACKAGE}")
        if args[:5] == ["shell", "run-as", EVENT_SPY_PACKAGE, "sh", "-c"]:
            command = shlex.split(shlex.split(args[5])[0])
            assert command[:2] == ["test", "-f"]
            return (0, "", "") if command[2] in self.files else (1, "", "")
        if args == ["shell", "pm", "clear", EVENT_SPY_PACKAGE]:
            return 0, f"{self.clear_output}\n", ""
        return 0, "", ""

    def run_to_file(self, args, destination, timeout=None):
        self.commands.append(list(args))
        assert args[:5] == ["shell", "-T", "run-as", EVENT_SPY_PACKAGE, "cat"]
        if self.copy_fails:
            return 1, "cat: Permission denied"
        destination.write_bytes(self.files[args[5]])
        return 0, ""


def spy(adb, tmp_path):
    return EventSpy(adb, tmp_path / "spy.apk")


def test_event_spy_is_described_by_its_manifest():
    assert EVENT_SPY_APP.component == "com.malicious.eventspy/com.malicious.eventspy.EventListenerAccessibilityService"
    assert EVENT_SPY_APP.short_component == "com.malicious.eventspy/.EventListenerAccessibilityService"
    assert (EVENT_SPY_APP.label, EVENT_SPY_APP.version) == ("Event Spy", "1.0")
    assert EVENT_SPY_APP.apk_path().name == "F5R2_Event_Capture_Attacker.apk"


def test_events_are_read_in_order_with_every_column(tmp_path):
    adb = EventSpyAdb(database_files(tmp_path))

    events = spy(adb, tmp_path).events()

    assert [event.event_type for event in events] == ["CLICK", "FOCUS", "TEXT_CHANGE"]
    assert events[2] == CapturedEvent(3, 3000, "TEXT_CHANGE", TARGET, "android.widget.EditText", f"{TARGET}:id/card",
                                      "4111 1111 1111 1111", "", None, "Card number", "Input: 4111 1111 1111 1111")


def test_events_can_be_limited_to_one_package(tmp_path):
    adb = EventSpyAdb(database_files(tmp_path))

    events = spy(adb, tmp_path).events(TARGET)

    assert [event.package for event in events] == [TARGET, TARGET]


def test_rows_still_in_the_write_ahead_log_are_read(tmp_path):
    late = (4000, "CLICK", TARGET, "android.widget.Button", f"{TARGET}:id/pay", None, None, None, None, "Clicked: Pay")
    files = database_files(tmp_path, in_wal=[late])
    assert f"{DATABASE}-wal" in files

    events = spy(EventSpyAdb(files), tmp_path).events(TARGET)

    assert [event.details for event in events][-1] == "Clicked: Pay"


def test_no_database_means_nothing_was_captured(tmp_path):
    adb = EventSpyAdb()

    assert spy(adb, tmp_path).events() == []
    assert not any(command[:2] == ["shell", "-T"] for command in adb.commands)


def test_a_build_that_is_not_debuggable_is_reported(tmp_path):
    with pytest.raises(AccessibilityAttackerError, match="cannot be read with run-as: run-as: package not debuggable"):
        spy(EventSpyAdb(database_files(tmp_path), debuggable=False), tmp_path).events()


def test_a_failed_copy_is_reported(tmp_path):
    with pytest.raises(AccessibilityAttackerError, match="could not copy Event Spy's databases/event_database: cat: Permission denied"):
        spy(EventSpyAdb(database_files(tmp_path), copy_fails=True), tmp_path).events()


def test_a_file_that_is_not_a_database_is_reported(tmp_path):
    with pytest.raises(AccessibilityAttackerError, match="could not be read"):
        spy(EventSpyAdb({DATABASE: b"not a database" * 100}), tmp_path).events()


def test_clear_deletes_the_apps_data(tmp_path):
    adb = EventSpyAdb()

    spy(adb, tmp_path).clear()

    assert adb.commands == [["shell", "pm", "clear", EVENT_SPY_PACKAGE]]


def test_a_refused_clear_is_reported(tmp_path):
    with pytest.raises(AccessibilityAttackerError, match="could not clear Event Spy's data: Failed"):
        spy(EventSpyAdb(clear_output="Failed"), tmp_path).clear()
