from __future__ import annotations

from types import SimpleNamespace

import pytest

from mobile_playbook.platforms.android.runner import AndroidPlatformRunner


class FakeAdb:
    def __init__(self, output):
        self.output = output
        self.commands: list[list[str]] = []

    def run(self, args, timeout=None):
        self.commands.append(list(args))
        return 0, self.output, ""


def test_the_installed_version_is_read_for_the_apps_package():
    adb = FakeAdb("    versionCode=321 minSdk=24 targetSdk=34\n    versionName=3.2.1\n")
    app = SimpleNamespace(id="example_app", package_name="com.example.one")

    assert AndroidPlatformRunner().installed_app_version(app, SimpleNamespace(adb=adb)) == ("3.2.1", "321")
    assert adb.commands == [["shell", "dumpsys", "package", "com.example.one"]]


def test_no_version_is_returned_when_the_device_reports_none():
    app = SimpleNamespace(id="example_app", package_name="com.example.one")

    assert AndroidPlatformRunner().installed_app_version(app, SimpleNamespace(adb=FakeAdb(""))) is None


class FakeRisk:
    requires: list[str] = []

    def __init__(self, requires_device=True):
        self.requires_device = requires_device
        self.runs = 0

    def run(self, app, config, device_client, report_writer):
        self.runs += 1


def run_one(monkeypatch, risk, unlock):
    monkeypatch.setattr("mobile_playbook.platforms.android.runner.get_risk", lambda test_id: risk)
    monkeypatch.setattr(
        "mobile_playbook.platforms.android.runner.check_android_preflight",
        lambda config, adb, requires: SimpleNamespace(ok=True, errors=[], warnings=[]),
    )
    monkeypatch.setattr("mobile_playbook.platforms.android.runner.wake_and_unlock", unlock)
    recorded = []
    runner = AndroidPlatformRunner()
    monkeypatch.setattr(runner, "_failure_result_template", lambda *a: SimpleNamespace(errors=[]))
    monkeypatch.setattr(runner, "_record_failure", lambda result, writer, exc: recorded.append(str(exc)))
    app = SimpleNamespace(id="example_app", package_name="com.example.one")
    config = SimpleNamespace(runner=SimpleNamespace(auto_grant_permissions=False))
    runner.run_test(app, "risk_id", config, SimpleNamespace(adb="ADB"), None)
    return recorded


def test_the_device_is_woken_and_unlocked_before_each_test(monkeypatch):
    unlocked = []
    risk = FakeRisk()

    run_one(monkeypatch, risk, unlocked.append)

    assert unlocked == ["ADB"]
    assert risk.runs == 1


def test_a_locked_device_fails_the_test_without_running_it(monkeypatch):
    risk = FakeRisk()

    def locked(adb):
        raise RuntimeError("the device is still locked after waking it")

    recorded = run_one(monkeypatch, risk, locked)

    assert risk.runs == 0
    assert recorded == ["the device is still locked after waking it"]


def test_a_risk_without_a_device_does_not_touch_the_screen(monkeypatch):
    risk = FakeRisk(requires_device=False)

    run_one(monkeypatch, risk, lambda adb: pytest.fail("no device to unlock"))

    assert risk.runs == 1


@pytest.mark.parametrize("keep_screen_on", [True, False])
def test_keep_screen_on_is_applied_when_connecting_only_if_configured(monkeypatch, tmp_path, keep_screen_on):
    monkeypatch.setattr(
        "mobile_playbook.platforms.android.runner.ensure_appium_running",
        lambda *a: SimpleNamespace(status="ALREADY_RUNNING", error=None, log_tail=None),
    )
    monkeypatch.setattr("mobile_playbook.platforms.android.runner.appium_start_message", lambda *a: None)
    monkeypatch.setattr("mobile_playbook.platforms.android.runner.AndroidDeviceClient.connect", lambda self: self)
    applied = []
    monkeypatch.setattr("mobile_playbook.platforms.android.runner.keep_screen_on_while_charging", lambda adb: applied.append(adb) or True)
    config = SimpleNamespace(
        device=SimpleNamespace(appium_server_url="http://127.0.0.1:4723", adb_path="adb", adb_serial=None, appium_auto_start={}),
        runner=SimpleNamespace(keep_screen_on=keep_screen_on),
    )

    AndroidPlatformRunner().connect_device(config, tmp_path)

    assert len(applied) == (1 if keep_screen_on else 0)
