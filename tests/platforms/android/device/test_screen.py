from __future__ import annotations

import pytest

from mobile_playbook.platforms.android.device import screen
from mobile_playbook.platforms.android.device.screen import DeviceLockedError, keep_screen_on_while_charging, wake_and_unlock


class PhoneAdb:
    """Fake adb for a phone whose screen and lock screen respond to wake and dismiss commands."""

    def __init__(self, awake=True, locked=False, has_pin=False, reports_keyguard=True):
        self.awake = awake
        self.locked = locked
        self.has_pin = has_pin
        self.reports_keyguard = reports_keyguard
        self.commands: list[list[str]] = []

    def run(self, args, timeout=None):
        self.commands.append(list(args))
        if args == ["shell", "dumpsys", "power"]:
            return 0, f"  mWakefulness={'Awake' if self.awake else 'Dozing'}\n", ""
        if args == ["shell", "dumpsys", "window"]:
            text = f"    isKeyguardShowing={str(self.locked).lower()}\n" if self.reports_keyguard else "WINDOW MANAGER\n"
            return 0, text, ""
        if args == ["shell", "input", "keyevent", "KEYCODE_WAKEUP"]:
            self.awake = True
        if args == ["shell", "wm", "dismiss-keyguard"] and not self.has_pin:
            self.locked = False
        return 0, "", ""


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(screen, "POLL_SECONDS", 0)


def test_an_awake_unlocked_phone_is_left_alone():
    adb = PhoneAdb()

    wake_and_unlock(adb)

    assert ["shell", "input", "keyevent", "KEYCODE_WAKEUP"] not in adb.commands
    assert ["shell", "wm", "dismiss-keyguard"] not in adb.commands


def test_a_sleeping_phone_without_a_pin_is_woken_and_unlocked():
    adb = PhoneAdb(awake=False, locked=True)

    wake_and_unlock(adb)

    assert adb.awake and not adb.locked
    assert adb.commands.index(["shell", "input", "keyevent", "KEYCODE_WAKEUP"]) < adb.commands.index(["shell", "wm", "dismiss-keyguard"])


def test_a_phone_with_a_pin_stops_the_test_with_a_clear_reason():
    with pytest.raises(DeviceLockedError, match="remove its PIN, password or pattern"):
        wake_and_unlock(PhoneAdb(awake=False, locked=True, has_pin=True), timeout=0.01)


def test_a_phone_that_does_not_report_its_lock_screen_is_only_woken():
    adb = PhoneAdb(awake=False, reports_keyguard=False)

    wake_and_unlock(adb)

    assert adb.awake
    assert ["shell", "wm", "dismiss-keyguard"] not in adb.commands


def test_keep_screen_on_sets_stay_on_for_every_power_source():
    adb = PhoneAdb()

    assert keep_screen_on_while_charging(adb) is True
    assert adb.commands == [["shell", "svc", "power", "stayon", "true"]]
