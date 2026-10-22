"""Reads synthetic copies of an attacker app's captured_events database through a fake adb serving its files to run-as."""

from __future__ import annotations

import pytest

from mobile_playbook.platforms.android.device.accessibility_service import AccessibilityAttackerError, ServiceApp
from mobile_playbook.platforms.android.device.captured_events import DATABASE, CapturedEvent, CapturedEventsApp
from tests.platforms.android.captured_events_fakes import TARGET, CapturedEventsAdb, database_files

ATTACKER = ServiceApp(package="com.example.attacker", version="1.0", apk_name="attacker.apk", label="Attacker", service_class="com.example.attacker.Service")


def fake_adb(files=None, **options):
    return CapturedEventsAdb(ATTACKER.package, files, **options)


def attacker(adb, tmp_path):
    return CapturedEventsApp(adb, tmp_path / "attacker.apk", ATTACKER)


def test_events_are_read_in_order_with_every_column(tmp_path):
    adb = fake_adb(database_files(tmp_path))

    events = attacker(adb, tmp_path).events()

    assert [event.event_type for event in events] == ["CLICK", "FOCUS", "TEXT_CHANGE"]
    assert events[2] == CapturedEvent(3, 3000, "TEXT_CHANGE", TARGET, "android.widget.EditText", f"{TARGET}:id/card",
                                      "4111 1111 1111 1111", "", None, "Card number", "Input: 4111 1111 1111 1111")


def test_events_can_be_limited_to_one_package(tmp_path):
    adb = fake_adb(database_files(tmp_path))

    events = attacker(adb, tmp_path).events(TARGET)

    assert [event.package for event in events] == [TARGET, TARGET]


def test_rows_still_in_the_write_ahead_log_are_read(tmp_path):
    late = (4000, "CLICK", TARGET, "android.widget.Button", f"{TARGET}:id/pay", None, None, None, None, "Clicked: Pay")
    files = database_files(tmp_path, in_wal=[late])
    assert f"{DATABASE}-wal" in files

    events = attacker(fake_adb(files), tmp_path).events(TARGET)

    assert [event.details for event in events][-1] == "Clicked: Pay"


def test_no_database_means_nothing_was_captured(tmp_path):
    adb = fake_adb()

    assert attacker(adb, tmp_path).events() == []
    assert not any(command[:2] == ["shell", "-T"] for command in adb.commands)


def test_a_build_that_is_not_debuggable_is_reported(tmp_path):
    with pytest.raises(AccessibilityAttackerError, match="Attacker's database cannot be read with run-as: run-as: package not debuggable"):
        attacker(fake_adb(database_files(tmp_path), debuggable=False), tmp_path).events()


def test_a_failed_copy_is_reported(tmp_path):
    with pytest.raises(AccessibilityAttackerError, match="could not copy Attacker's databases/event_database: cat: Permission denied"):
        attacker(fake_adb(database_files(tmp_path), copy_fails=True), tmp_path).events()


def test_a_file_that_is_not_a_database_is_reported(tmp_path):
    with pytest.raises(AccessibilityAttackerError, match="Attacker's database could not be read"):
        attacker(fake_adb({DATABASE: b"not a database" * 100}), tmp_path).events()


def test_clear_deletes_the_apps_data(tmp_path):
    adb = fake_adb()

    attacker(adb, tmp_path).clear()

    assert adb.commands == [["shell", "pm", "clear", ATTACKER.package]]


def test_a_refused_clear_is_reported(tmp_path):
    with pytest.raises(AccessibilityAttackerError, match="could not clear Attacker's data: Failed"):
        attacker(fake_adb(clear_output="Failed"), tmp_path).clear()
