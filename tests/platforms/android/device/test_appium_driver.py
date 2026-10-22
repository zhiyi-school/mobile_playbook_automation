from __future__ import annotations

from types import SimpleNamespace

import pytest

from mobile_playbook.platforms.android.device import appium_driver
from mobile_playbook.platforms.android.device.adb import AdbClient
from mobile_playbook.platforms.android.device.client import AndroidDeviceClient

pytestmark = pytest.mark.skipif(not appium_driver.appium_available(), reason="Appium-Python-Client is not installed")


@pytest.fixture
def created(monkeypatch):
    sessions: list[dict] = []

    def fake_remote(server_url, options):
        sessions.append({"server_url": server_url, **options.to_capabilities()})
        return SimpleNamespace(session_id="session")

    monkeypatch.setattr(appium_driver.webdriver, "Remote", fake_remote)
    return sessions


def make_client(adb_serial):
    device = SimpleNamespace(appium_server_url="http://127.0.0.1:4723", adb_serial=adb_serial)
    return AndroidDeviceClient(config=SimpleNamespace(device=device), adb=AdbClient(serial=adb_serial))


def test_the_configured_adb_serial_pins_the_appium_session(created):
    make_client("10.0.0.5:5555").make_driver()

    assert created[0]["appium:udid"] == "10.0.0.5:5555"


def test_without_an_adb_serial_appium_chooses_the_device(created):
    make_client(None).make_driver("com.example.app")

    assert "appium:udid" not in created[0]
    assert created[0]["appium:appPackage"] == "com.example.app"


def test_sessions_allow_a_slow_link_to_install_the_uiautomator2_server(created):
    make_client("10.11.1.3:5001").make_driver()

    assert created[0]["appium:uiautomator2ServerInstallTimeout"] == 300_000
    assert created[0]["appium:adbExecTimeout"] == 120_000


def test_without_extra_capabilities_a_session_has_only_the_defaults(created):
    make_client("10.0.0.5:5555").make_driver("com.example.app")

    assert created[0] == {
        "server_url": "http://127.0.0.1:4723",
        "platformName": "Android",
        "automationName": "UIAutomator2",
        "appium:automationName": "UiAutomator2",
        "appium:noReset": True,
        "appium:newCommandTimeout": 120,
        "appium:uiautomator2ServerInstallTimeout": 300_000,
        "appium:adbExecTimeout": 120_000,
        "appium:udid": "10.0.0.5:5555",
        "appium:appPackage": "com.example.app",
    }


def test_extra_capabilities_are_added_to_the_defaults(created):
    make_client("10.0.0.5:5555").make_driver(extra_capabilities={"appium:disableSuppressAccessibilityService": True})

    assert created[0]["appium:disableSuppressAccessibilityService"] is True
    assert created[0]["appium:noReset"] is True
    assert created[0]["appium:udid"] == "10.0.0.5:5555"


def test_an_extra_capability_is_applied_after_the_defaults(created):
    make_client(None).make_driver(extra_capabilities={"appium:newCommandTimeout": 300})

    assert created[0]["appium:newCommandTimeout"] == 300
