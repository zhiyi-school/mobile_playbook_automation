"""
Synthetic captured_events Room databases and a fake adb that serves an attacker app's private files to run-as.
"""

from __future__ import annotations

import shlex
import sqlite3
from pathlib import Path

from mobile_playbook.platforms.android.device.captured_events import DATABASE

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


class CapturedEventsAdb:
    """Fake adb serving an attacker app's private files to run-as and answering pm clear."""

    def __init__(self, package, files=None, debuggable=True, copy_fails=False, clear_output="Success", version=None):
        self.package = package
        self.files = dict(files or {})
        self.version = version
        self.debuggable = debuggable
        self.copy_fails = copy_fails
        self.clear_output = clear_output
        self.commands: list[list[str]] = []

    def run(self, args, timeout=None):
        self.commands.append(list(args))
        if args == ["shell", "run-as", self.package, "id", "-u"]:
            return (0, "10234\n", "") if self.debuggable else (1, "", f"run-as: package not debuggable: {self.package}")
        if args[:5] == ["shell", "run-as", self.package, "sh", "-c"]:
            command = shlex.split(shlex.split(args[5])[0])
            assert command[:2] == ["test", "-f"]
            return (0, "", "") if command[2] in self.files else (1, "", "")
        if args[:3] == ["shell", "dumpsys", "package"]:
            return (0, f"  versionCode=1\n  versionName={self.version}\n", "") if self.version else (0, "", "")
        if args == ["shell", "pm", "clear", self.package]:
            return 0, f"{self.clear_output}\n", ""
        return 0, "", ""

    def run_to_file(self, args, destination, timeout=None):
        self.commands.append(list(args))
        assert args[:5] == ["shell", "-T", "run-as", self.package, "cat"]
        if self.copy_fails:
            return 1, "cat: Permission denied"
        destination.write_bytes(self.files[args[5]])
        return 0, ""
