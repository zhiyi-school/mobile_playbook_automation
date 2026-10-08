from __future__ import annotations

from types import SimpleNamespace

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
