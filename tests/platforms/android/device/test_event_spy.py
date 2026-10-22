"""Checks Event Spy's description, its reads and messages through the shared reader, and its preflight requirement."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from mobile_playbook.platforms.android.device.accessibility_service import AccessibilityAttackerError
from mobile_playbook.platforms.android.device.event_spy import EVENT_SPY_APP, EVENT_SPY_PACKAGE, EventSpy
from mobile_playbook.platforms.android.preflight import check_requirements
from tests.platforms.android.captured_events_fakes import TARGET, CapturedEventsAdb, database_files


def fake_adb(files=None, **options):
    return CapturedEventsAdb(EVENT_SPY_PACKAGE, files, **options)


def spy(adb, tmp_path):
    return EventSpy(adb, tmp_path / "spy.apk")


def test_event_spy_is_described_by_its_manifest():
    assert EVENT_SPY_APP.component == "com.malicious.eventspy/com.malicious.eventspy.EventListenerAccessibilityService"
    assert EVENT_SPY_APP.short_component == "com.malicious.eventspy/.EventListenerAccessibilityService"
    assert (EVENT_SPY_APP.label, EVENT_SPY_APP.version) == ("Event Spy", "1.0")
    assert EVENT_SPY_APP.apk_path().name == "F5R2_Event_Capture_Attacker.apk"


def test_event_spy_reads_its_own_database(tmp_path):
    adb = fake_adb(database_files(tmp_path))

    events = spy(adb, tmp_path).events(TARGET)

    assert [event.event_type for event in events] == ["FOCUS", "TEXT_CHANGE"]
    assert ["shell", "-T", "run-as", EVENT_SPY_PACKAGE, "cat", "databases/event_database"] in adb.commands


def test_event_spy_errors_name_event_spy(tmp_path):
    with pytest.raises(AccessibilityAttackerError, match="Event Spy's database cannot be read with run-as"):
        spy(fake_adb(database_files(tmp_path), debuggable=False), tmp_path).events()
    with pytest.raises(AccessibilityAttackerError, match="could not clear Event Spy's data: Failed"):
        spy(fake_adb(clear_output="Failed"), tmp_path).clear()


def _config(apk):
    return SimpleNamespace(accessibility={"event_spy_apk": str(apk)})


def test_preflight_passes_with_the_event_spy_apk_in_place(tmp_path):
    apk = tmp_path / "spy.apk"
    apk.write_bytes(b"apk")

    [(name, ok, message)] = check_requirements(["event_spy_attacker"], fake_adb(), _config(apk))

    assert (name, ok) == ("event_spy_attacker", True)
    assert str(apk) in message


def test_preflight_passes_when_the_device_already_has_event_spy(tmp_path):
    [(_, ok, message)] = check_requirements(["event_spy_attacker"], fake_adb(version="1.0"), _config(tmp_path / "missing.apk"))

    assert ok and "already installed" in message


@pytest.mark.parametrize("version", [None, "0.9"])
def test_preflight_fails_with_the_apk_path_when_missing_or_old(tmp_path, version):
    apk = tmp_path / "missing.apk"

    [(_, ok, message)] = check_requirements(["event_spy_attacker"], fake_adb(version=version), _config(apk))

    assert not ok
    assert str(apk) in message and "F5R2_Event_Capture_Attacker.apk" in message


def test_preflight_defaults_to_the_intake_copy():
    [(_, ok, message)] = check_requirements(["event_spy_attacker"], None, SimpleNamespace(accessibility={}))

    assert str(EVENT_SPY_APP.apk_path()) in message
