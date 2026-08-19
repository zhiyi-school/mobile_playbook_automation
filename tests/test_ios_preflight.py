from __future__ import annotations

from types import SimpleNamespace

import pytest

from mobile_playbook.platforms.ios.preflight import _parse_connected_udids, check_ios_preflight
from mobile_playbook.platforms.ios.runner import IosPlatformRunner

XCTRACE_OUTPUT = """\
== Devices ==
user's MacBook Air (06316618-98B3-5149-9423-BB729F8163A5)
CSEC's iPhone (iOS 17.6) (00008120-0001110834E1A01E)

== Simulators ==
iPhone 16 Simulator (26.5) (F3A1191D-9506-4E83-832F-8035F19748CE)
"""


def _config(udid: str = "00008120-0001110834E1A01E"):
    device = SimpleNamespace(udid=udid, team_id="TEAM", appium_server_url="http://127.0.0.1:4723")
    return SimpleNamespace(device=device)


def test_parse_connected_udids_only_includes_online_devices_section():
    udids = _parse_connected_udids(XCTRACE_OUTPUT)

    assert udids == {"06316618-98B3-5149-9423-BB729F8163A5", "00008120-0001110834E1A01E"}
    assert "F3A1191D-9506-4E83-832F-8035F19748CE" not in udids  # simulator, not a device


def test_check_ios_preflight_passes_when_udid_is_connected(monkeypatch):
    monkeypatch.setattr(
        "mobile_playbook.platforms.ios.preflight.connected_device_udids",
        lambda: {"00008120-0001110834E1A01E"},
    )
    monkeypatch.setattr("mobile_playbook.platforms.ios.preflight._tcp_reachable", lambda url: True)

    result = check_ios_preflight(_config())

    assert result.ok
    assert result.errors == []


def test_check_ios_preflight_gives_a_one_line_error_for_a_disconnected_device(monkeypatch):
    monkeypatch.setattr(
        "mobile_playbook.platforms.ios.preflight.connected_device_udids",
        lambda: {"00008150-000E03321493401C"},  # a different device is connected instead
    )

    result = check_ios_preflight(_config("00008120-0001110834E1A01E"))

    assert not result.ok
    assert len(result.errors) == 1
    assert "00008120-0001110834E1A01E" in result.errors[0]
    assert "xcrun xctrace list devices" in result.errors[0]


def test_check_ios_preflight_skips_connectivity_check_when_xctrace_is_unusable(monkeypatch):
    # An empty set means we couldn't determine connected devices at all (xcrun missing,
    # timed out, etc.) — this must not be treated as "zero devices connected".
    monkeypatch.setattr("mobile_playbook.platforms.ios.preflight.connected_device_udids", lambda: set())
    monkeypatch.setattr("mobile_playbook.platforms.ios.preflight._tcp_reachable", lambda url: True)

    result = check_ios_preflight(_config())

    assert result.ok


def test_check_ios_preflight_gives_a_one_line_error_when_appium_is_unreachable(monkeypatch):
    monkeypatch.setattr(
        "mobile_playbook.platforms.ios.preflight.connected_device_udids",
        lambda: {"00008120-0001110834E1A01E"},
    )
    monkeypatch.setattr("mobile_playbook.platforms.ios.preflight._tcp_reachable", lambda url: False)

    result = check_ios_preflight(_config())

    assert not result.ok
    assert len(result.errors) == 1
    assert result.errors[0] == "appium: Appium server not reachable at http://127.0.0.1:4723. Start it with 'appium'."


def test_connect_device_raises_a_clean_error_without_opening_an_appium_session(monkeypatch):
    monkeypatch.setattr(
        "mobile_playbook.platforms.ios.preflight.connected_device_udids",
        lambda: {"some-other-device"},
    )

    def fail_if_called(*args, **kwargs):
        raise AssertionError("AppiumDeviceClient.connect should not be called when preflight fails")

    monkeypatch.setattr("mobile_playbook.platforms.ios.runner.AppiumDeviceClient.connect", fail_if_called)

    with pytest.raises(RuntimeError, match="00008120-0001110834E1A01E"):
        IosPlatformRunner().connect_device(_config())
