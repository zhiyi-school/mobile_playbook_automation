"""Checks that the shared accessibility service control follows the app it is given, using a synthetic second app."""

from __future__ import annotations

import shlex

import pytest

from mobile_playbook.common.storage_paths import REPOSITORY_ROOT, android_intake_dir
from mobile_playbook.platforms.android.device.accessibility_service import (
    AccessibilityAttackerError,
    AccessibilityServiceControl,
    AccessibilitySettings,
    ServiceApp,
)
from tests.platforms.android.install_helpers import answer_apk_copy, is_pm_install

APP = ServiceApp(package="com.example.spy", version="1.0", apk_name="Example_Spy.apk", label="Example Spy",
                 service_class="com.example.spy.SpyService")
OTHER_SERVICE = "com.example.reader/com.example.reader.ReaderService"


class ServiceAdb:
    """Fake adb holding the device's secure settings, its accessibility dump and the installed app version."""

    def __init__(self, settings=None, dump="", version=None):
        self.settings = dict(settings or {})
        self.dump = dump
        self.version = version
        self.commands: list[list[str]] = []
        self.copied: dict[str, int] = {}

    def run(self, args, timeout=None):
        self.commands.append(list(args))
        if args[:3] == ["shell", "dumpsys", "package"]:
            return (0, f"  versionCode=1\n  versionName={self.version}\n", "") if self.version else (0, "", "")
        if copy := answer_apk_copy(args, self.copied):
            return copy
        if is_pm_install(args):
            return 0, "Success", ""
        if args[:3] == ["shell", "dumpsys", "accessibility"]:
            return 0, self.dump, ""
        if args[:4] == ["shell", "settings", "get", "secure"]:
            return 0, f"{self.settings.get(args[4], 'null')}\n", ""
        if args[:4] == ["shell", "settings", "put", "secure"]:
            self.settings[args[4]] = shlex.split(args[5])[0]
            return 0, "", ""
        return 0, "", ""


def control(adb, tmp_path):
    return AccessibilityServiceControl(adb, tmp_path / "spy.apk", APP)


def test_component_names_take_the_service_class_relative_to_the_package():
    assert APP.component == "com.example.spy/com.example.spy.SpyService"
    assert APP.short_component == "com.example.spy/.SpyService"


def test_a_service_class_outside_the_package_has_no_short_form():
    app = ServiceApp(package="com.example.spy", version="1", apk_name="a.apk", label="x", service_class="org.other.Service")

    assert app.short_component == app.component == "com.example.spy/org.other.Service"


def test_either_component_form_names_the_service():
    assert APP.names_service([OTHER_SERVICE, APP.short_component])
    assert APP.names_service([APP.component])
    assert not APP.names_service([OTHER_SERVICE])


def test_the_apk_defaults_to_the_apps_intake_copy(tmp_path):
    assert APP.apk_path() == android_intake_dir() / "Example_Spy.apk"
    assert APP.apk_path("artifacts/other.apk") == REPOSITORY_ROOT / "artifacts/other.apk"
    assert APP.apk_path(str(tmp_path / "a.apk")) == tmp_path / "a.apk"


def test_the_apps_current_version_is_not_reinstalled(tmp_path):
    adb = ServiceAdb(version="1.0")

    control(adb, tmp_path).ensure_installed()

    assert not any(is_pm_install(command) for command in adb.commands)


def test_another_version_of_the_app_is_replaced(tmp_path):
    (tmp_path / "spy.apk").write_bytes(b"apk")
    adb = ServiceAdb(version="0.9")

    control(adb, tmp_path).ensure_installed()

    assert any(is_pm_install(command) for command in adb.commands)


def test_enabling_adds_the_apps_service_and_is_enabled_reads_it_back(tmp_path):
    adb = ServiceAdb(settings={"enabled_accessibility_services": OTHER_SERVICE})
    service = control(adb, tmp_path)

    service.enable_via_secure_settings(service.read_settings())

    assert adb.settings == {"enabled_accessibility_services": f"{OTHER_SERVICE}:{APP.component}", "accessibility_enabled": "1"}
    assert service.is_enabled()


def test_restore_puts_back_the_recorded_settings(tmp_path):
    adb = ServiceAdb(settings={"enabled_accessibility_services": APP.component, "accessibility_enabled": "1"})

    control(adb, tmp_path).restore_settings(AccessibilitySettings(enabled_services=OTHER_SERVICE, accessibility_enabled="1"))

    assert adb.settings["enabled_accessibility_services"] == OTHER_SERVICE


def test_is_bound_looks_for_the_apps_own_label(tmp_path):
    bound = "     Bound services:{Service[label=Example Spy, capabilities=1]}\n"
    other = "     Bound services:{Service[label=Playbook Accessibility Test, capabilities=1]}\n"

    assert control(ServiceAdb(dump=bound), tmp_path).is_bound()
    assert not control(ServiceAdb(dump=other), tmp_path).is_bound()


def test_stop_and_uninstall_name_the_apps_package(tmp_path):
    adb = ServiceAdb()
    service = control(adb, tmp_path)

    service.stop()
    service.uninstall()

    assert adb.commands == [["shell", "am", "force-stop", APP.package], ["uninstall", APP.package]]


def test_an_app_the_phone_still_lists_after_uninstall_is_reported(tmp_path):
    class StillThere(ServiceAdb):
        def run(self, args, timeout=None):
            if args[:1] == ["uninstall"]:
                return 1, "Failure [DELETE_FAILED_INTERNAL_ERROR]", ""
            if args[:4] == ["shell", "pm", "list", "packages"]:
                return 0, f"package:{APP.package}\n", ""
            return super().run(args, timeout)

    with pytest.raises(AccessibilityAttackerError, match=f"{APP.package} is still installed"):
        control(StillThere(), tmp_path).uninstall()
