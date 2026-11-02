from __future__ import annotations

import pytest

from mobile_playbook.platforms.android.device.settings_ui import SettingsUiError, tap_element, text_matcher
from tests.platforms.android.settings_ui_fakes import Element, python_pattern


class Adb:
    def __init__(self, result=(0, "", "")):
        self.result = result
        self.commands: list[list[str]] = []

    def run(self, args, timeout=None):
        self.commands.append(list(args))
        return self.result


def test_the_text_matcher_has_no_backslashes_for_appiums_parser():
    assert "\\" not in text_matcher(("Grant access", "Don't allow", 'Say "yes"'))


def test_the_text_matcher_matches_whole_texts_ignoring_case_and_escapes_quotes():
    selector = text_matcher(("Grant access", 'Say "yes" (1)'))
    pattern = python_pattern(selector)

    assert selector.startswith('new UiSelector().textMatches("(?i)(')
    assert pattern.fullmatch("GRANT ACCESS")
    assert pattern.fullmatch('say "YES" (1)')
    assert not pattern.fullmatch("Grant access now")


def test_tap_reads_the_element_centre_from_its_bounds():
    adb = Adb()
    tap_element(adb, Element())

    tap = adb.commands[-1]
    assert tap[:3] == ["shell", "input", "tap"]
    assert tap[3].isdigit() and tap[4].isdigit()


def test_tap_fails_when_the_bounds_cannot_be_read():
    class NoBounds:
        def get_attribute(self, name):
            return None

    with pytest.raises(SettingsUiError, match="could not read the element bounds"):
        tap_element(Adb(), NoBounds())


def test_tap_fails_when_adb_cannot_tap():
    with pytest.raises(SettingsUiError, match="input tap failed: device offline"):
        tap_element(Adb((1, "", "device offline")), Element())
