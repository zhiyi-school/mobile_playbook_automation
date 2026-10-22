from __future__ import annotations

import json
import shlex
from types import SimpleNamespace

import pytest

from mobile_playbook.common.storage_paths import REPOSITORY_ROOT, android_intake_dir
from mobile_playbook.platforms.android.device import accessibility_attacker
from mobile_playbook.platforms.android.device.accessibility_attacker import (
    ATTACKER_APK_NAME,
    ATTACKER_PACKAGE,
    ATTACKER_VERSION,
    MAIN_ACTIVITY,
    OUTPUT_DIR,
    SERVICE_COMPONENT,
    SERVICE_SHORT_COMPONENT,
    AccessibilityAttacker,
    AccessibilityAttackerError,
    AccessibilitySettings,
    AttackEvent,
    attacker_apk_path,
)
from mobile_playbook.platforms.android.device.install import STAGING_DIR
from tests.platforms.android.install_helpers import answer_apk_copy, is_pm_install

OTHER_SERVICE = "com.example.reader/com.example.reader.ReaderService"
BOUND_DUMP = (
    "ACCESSIBILITY MANAGER (dumpsys accessibility)\n"
    "User state[\n"
    "     Bound services:{Service[label=Playbook Accessibility Test, feedbackType[FEEDBACK_GENERIC], capabilities=1, eventTypes=TYPES_ALL_MASK, notificationTimeout=0, requestA11yBtn=false]}\n"
    f"     Enabled services:{{{SERVICE_SHORT_COMPONENT}}}\n"
    "     Binding services:{}\n"
)
UNBOUND_DUMP = (
    "     Bound services:{}\n"
    "     Enabled services:{}\n"
    "     Binding services:{}\n"
    f"     installed services: {{{SERVICE_SHORT_COMPONENT}, label=Playbook Accessibility Test]}}\n"
)


class AttackerAdb:
    """Fake adb backed by the attacker app's output files and the device's secure settings."""

    def __init__(self, version=None, files=None, settings=None, on_command=None, install_result=(0, "Success", ""), run_as_only=False, dump="", put_output=None):
        self.version = version
        self.files = dict(files or {})
        self.settings = dict(settings or {})
        self.on_command = on_command
        self.install_result = install_result
        self.run_as_only = run_as_only
        self.dump = dump
        self.put_output = put_output
        self.commands: list[list[str]] = []
        self.copied: dict[str, int] = {}

    def run(self, args, timeout=None):
        self.commands.append(list(args))
        if self.on_command:
            self.on_command(self, args)
        if args[:3] == ["shell", "dumpsys", "package"]:
            return (0, f"  versionCode=1\n  versionName={self.version}\n", "") if self.version else (0, "", "")
        if args[:3] == ["shell", "dumpsys", "accessibility"]:
            return 0, self.dump, ""
        if copy := answer_apk_copy(args, self.copied):
            return copy
        if is_pm_install(args):
            return self.install_result
        if args[:4] == ["shell", "settings", "get", "secure"]:
            return 0, f"{self.settings.get(args[4], 'null')}\n", ""
        if args[:4] == ["shell", "settings", "put", "secure"]:
            if self.put_output is not None:
                return 0, self.put_output, ""
            if len(args) < 6:
                return 0, "Bad arguments\n", ""
            self.settings[args[4]] = shlex.split(args[5])[0] if shlex.split(args[5]) else ""
            return 0, "", ""
        if args[:4] == ["shell", "settings", "delete", "secure"]:
            self.settings.pop(args[4], None)
            return 0, "Deleted 1 rows\n", ""
        if args[:3] == ["shell", "rm", "-f"]:
            for path in args[3:]:
                self.files.pop(path.rsplit("/", 1)[1], None)
            return 0, "", ""
        if args[:2] == ["shell", "cat"] and self.run_as_only:
            return 1, "", "Permission denied"
        if args[:2] == ["shell", "cat"] or args[:4] == ["shell", "run-as", ATTACKER_PACKAGE, "cat"]:
            name = args[-1].rsplit("/", 1)[1]
            if name not in self.files:
                return 1, "", "No such file or directory"
            content = self.files[name]
            return 0, content if isinstance(content, str) else json.dumps(content), ""
        return 0, "", ""


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(accessibility_attacker, "POLL_SECONDS", 0)


def prepares_for(target):
    def answer(adb, args):
        if args[:3] == ["shell", "am", "start"]:
            adb.files["status.json"] = {"state": "prepared", "target_package": target, "service_connected": False}

    return answer


def attacker(adb, tmp_path):
    return AccessibilityAttacker(adb, tmp_path / "a.apk")


def test_the_default_apk_is_the_intake_copy():
    assert attacker_apk_path() == android_intake_dir() / ATTACKER_APK_NAME


def test_a_relative_configured_apk_resolves_against_the_repository_root(tmp_path):
    assert attacker_apk_path("artifacts/other.apk") == REPOSITORY_ROOT / "artifacts/other.apk"
    assert attacker_apk_path(str(tmp_path / "a.apk")) == tmp_path / "a.apk"


def test_the_current_attacker_app_is_not_reinstalled(tmp_path):
    adb = AttackerAdb(version=ATTACKER_VERSION)

    AccessibilityAttacker(adb, tmp_path / "missing.apk").ensure_installed()

    assert not any(is_pm_install(command) for command in adb.commands)


def test_a_missing_or_old_attacker_app_is_installed_without_granting_permissions(tmp_path):
    apk = tmp_path / "attacker.apk"
    apk.write_bytes(b"apk")
    adb = AttackerAdb(version="0")

    AccessibilityAttacker(adb, apk).ensure_installed()

    assert ["shell", "pm", "install", "-r", f"{STAGING_DIR}/0.apk"] in adb.commands


def test_an_absent_apk_is_reported_with_its_path(tmp_path):
    with pytest.raises(AccessibilityAttackerError, match="missing.apk"):
        AccessibilityAttacker(AttackerAdb(), tmp_path / "missing.apk").ensure_installed()


def test_a_failed_install_is_reported(tmp_path):
    apk = tmp_path / "attacker.apk"
    apk.write_bytes(b"apk")
    adb = AttackerAdb(install_result=(1, "Failure [INSTALL_FAILED_UPDATE_INCOMPATIBLE]", ""))

    with pytest.raises(AccessibilityAttackerError, match="INSTALL_FAILED_UPDATE_INCOMPATIBLE"):
        AccessibilityAttacker(adb, apk).ensure_installed()


def test_prepare_clears_old_output_and_targets_the_app_without_force_stopping(tmp_path):
    adb = AttackerAdb(on_command=prepares_for("com.example.bank"))

    status = attacker(adb, tmp_path).prepare("com.example.bank", timeout=1)

    assert status.state == "prepared"
    assert status.target_package == "com.example.bank"
    assert adb.commands[0] == ["shell", "rm", "-f", f"{OUTPUT_DIR}/status.json", f"{OUTPUT_DIR}/events.json"]
    assert adb.commands[1] == [
        "shell", "am", "start", "-n", MAIN_ACTIVITY,
        "--es", "target_package", "com.example.bank", "--ez", "reset", "true",
    ]


def test_prepare_does_not_accept_a_status_for_another_target(tmp_path):
    adb = AttackerAdb(on_command=prepares_for("com.example.other"))

    with pytest.raises(AccessibilityAttackerError, match="was not prepared for com.example.bank.*com.example.other"):
        attacker(adb, tmp_path).prepare("com.example.bank", timeout=0)


def test_prepare_does_not_accept_a_status_left_by_an_earlier_run(tmp_path):
    adb = AttackerAdb(files={"status.json": {"state": "prepared", "target_package": "com.example.bank"}})

    with pytest.raises(AccessibilityAttackerError, match="no status"):
        attacker(adb, tmp_path).prepare("com.example.bank", timeout=0)


def test_status_reports_the_service_and_its_counts(tmp_path):
    adb = AttackerAdb(files={"status.json": {
        "state": "attack_successful", "detail": "Found by text", "updated_at": "2026-10-22T02:11:03Z",
        "target_package": "com.example.bank", "service_connected": True,
        "scan_count": 4, "click_count": 2, "success_count": 2,
    }})

    status = attacker(adb, tmp_path).status()

    assert status.state == "attack_successful"
    assert status.service_connected is True
    assert (status.scan_count, status.click_count, status.success_count) == (4, 2, 2)


def test_a_status_without_a_state_or_with_partial_json_is_not_reported(tmp_path):
    assert attacker(AttackerAdb(files={"status.json": {"detail": "x"}}), tmp_path).status() is None
    assert attacker(AttackerAdb(files={"status.json": '{"state": "pre'}), tmp_path).status() is None


def test_a_null_target_and_a_non_boolean_connection_are_not_taken_as_set(tmp_path):
    adb = AttackerAdb(files={"status.json": {"state": "prepared", "target_package": None, "service_connected": "true"}})

    status = attacker(adb, tmp_path).status()

    assert status.target_package is None
    assert status.service_connected is False


def test_events_are_returned_in_order_with_their_click_and_match_fields(tmp_path):
    adb = AttackerAdb(files={"events.json": [
        {"timestamp": "t1", "event": "service_connected", "package": None, "message": "ready"},
        {"timestamp": "t2", "event": "click", "package": "com.example.bank", "message": "Clicked",
         "view_id": None, "class_name": "android.widget.Button", "text": "LOGOUT", "content_description": None},
        {"timestamp": "t3", "event": "attack_successful", "package": "com.example.bank", "message": "Found by resource ID",
         "strategy": "resource_id", "pattern": "btn_logout"},
        {"timestamp": "t4", "message": "no event name"},
    ]})

    events = attacker(adb, tmp_path).events()

    assert events == [
        AttackEvent(timestamp="t1", event="service_connected", package=None, message="ready"),
        AttackEvent(timestamp="t2", event="click", package="com.example.bank", message="Clicked",
                    class_name="android.widget.Button", text="LOGOUT"),
        AttackEvent(timestamp="t3", event="attack_successful", package="com.example.bank", message="Found by resource ID",
                    strategy="resource_id", pattern="btn_logout"),
    ]


def test_no_events_are_returned_when_the_file_is_absent(tmp_path):
    assert attacker(AttackerAdb(), tmp_path).events() == []


def test_output_is_read_with_run_as_when_shared_storage_is_not_readable(tmp_path):
    adb = AttackerAdb(files={"status.json": {"state": "prepared"}}, run_as_only=True)

    assert attacker(adb, tmp_path).status().state == "prepared"
    assert adb.commands[-1] == ["shell", "run-as", ATTACKER_PACKAGE, "cat", f"{OUTPUT_DIR}/status.json"]


def test_wait_for_state_names_the_last_state_seen_on_timeout(tmp_path):
    adb = AttackerAdb(files={"status.json": {"state": "prepared"}})

    with pytest.raises(AccessibilityAttackerError, match=r"\['service_connected'\].*last: prepared"):
        attacker(adb, tmp_path).wait_for_state({"service_connected"}, timeout=0)


def test_unset_settings_are_read_as_none(tmp_path):
    settings = attacker(AttackerAdb(settings={"accessibility_enabled": "0"}), tmp_path).read_settings()

    assert settings == AccessibilitySettings(enabled_services=None, accessibility_enabled="0")


def test_enabling_adds_the_service_to_the_services_already_enabled(tmp_path):
    adb = AttackerAdb(settings={"enabled_accessibility_services": OTHER_SERVICE, "accessibility_enabled": "1"})
    driver = attacker(adb, tmp_path)

    driver.enable_via_secure_settings(driver.read_settings())

    assert adb.settings["enabled_accessibility_services"] == f"{OTHER_SERVICE}:{SERVICE_COMPONENT}"
    assert adb.settings["accessibility_enabled"] == "1"
    assert driver.is_enabled()


def test_enabling_does_not_list_the_service_twice(tmp_path):
    adb = AttackerAdb(settings={"enabled_accessibility_services": SERVICE_SHORT_COMPONENT})
    driver = attacker(adb, tmp_path)

    driver.enable_via_secure_settings(driver.read_settings())

    assert adb.settings["enabled_accessibility_services"] == SERVICE_SHORT_COMPONENT


def test_restore_puts_back_the_other_services_and_the_previous_switch(tmp_path):
    adb = AttackerAdb(settings={"enabled_accessibility_services": OTHER_SERVICE, "accessibility_enabled": "1"})
    driver = attacker(adb, tmp_path)
    before = driver.read_settings()
    driver.enable_via_secure_settings(before)

    driver.restore_settings(before)

    assert adb.settings == {"enabled_accessibility_services": OTHER_SERVICE, "accessibility_enabled": "1"}
    assert not driver.is_enabled()


def test_restore_deletes_keys_that_were_unset(tmp_path):
    adb = AttackerAdb()
    driver = attacker(adb, tmp_path)
    before = driver.read_settings()
    driver.enable_via_secure_settings(before)

    driver.restore_settings(before)

    assert adb.settings == {}


def test_restore_writes_an_empty_value_as_an_empty_string(tmp_path):
    adb = AttackerAdb(settings={"enabled_accessibility_services": "", "accessibility_enabled": "0"})
    driver = attacker(adb, tmp_path)
    before = driver.read_settings()
    driver.enable_via_secure_settings(before)

    driver.restore_settings(before)

    assert adb.settings == {"enabled_accessibility_services": "", "accessibility_enabled": "0"}


def test_restore_fails_when_the_device_keeps_the_service(tmp_path):
    adb = AttackerAdb(settings={"enabled_accessibility_services": SERVICE_COMPONENT, "accessibility_enabled": "1"}, put_output="")
    before = AccessibilitySettings(enabled_services=OTHER_SERVICE, accessibility_enabled="1")

    with pytest.raises(AccessibilityAttackerError, match="were not restored"):
        attacker(adb, tmp_path).restore_settings(before)


def test_a_rejected_settings_command_is_reported(tmp_path):
    adb = AttackerAdb(put_output="Bad arguments\n")

    with pytest.raises(AccessibilityAttackerError, match="settings put secure enabled_accessibility_services"):
        attacker(adb, tmp_path).enable_via_secure_settings(AccessibilitySettings(enabled_services=None, accessibility_enabled=None))


def test_is_bound_reads_the_service_label_from_the_bound_services(tmp_path):
    assert attacker(AttackerAdb(dump=BOUND_DUMP), tmp_path).is_bound()
    assert not attacker(AttackerAdb(dump=UNBOUND_DUMP), tmp_path).is_bound()


def test_stop_force_stops_the_attacker_app(tmp_path):
    adb = AttackerAdb()

    attacker(adb, tmp_path).stop()

    assert adb.commands == [["shell", "am", "force-stop", ATTACKER_PACKAGE]]


def _config(apk):
    return SimpleNamespace(accessibility={"attacker_apk": str(apk)})


def test_preflight_passes_with_the_attacker_apk_in_place(tmp_path):
    from mobile_playbook.platforms.android.preflight import check_requirements

    apk = tmp_path / "attacker.apk"
    apk.write_bytes(b"apk")

    [(name, ok, message)] = check_requirements(["accessibility_attacker"], AttackerAdb(), _config(apk))

    assert (name, ok) == ("accessibility_attacker", True)
    assert str(apk) in message


def test_preflight_passes_when_the_device_already_has_the_attacker_app(tmp_path):
    from mobile_playbook.platforms.android.preflight import check_requirements

    [(_, ok, message)] = check_requirements(["accessibility_attacker"], AttackerAdb(version=ATTACKER_VERSION), _config(tmp_path / "missing.apk"))

    assert ok and "already installed" in message


@pytest.mark.parametrize("version", [None, "0"])
def test_preflight_fails_with_the_apk_path_when_missing_or_old(tmp_path, version):
    from mobile_playbook.platforms.android.preflight import check_requirements

    apk = tmp_path / "missing.apk"

    [(_, ok, message)] = check_requirements(["accessibility_attacker"], AttackerAdb(version=version), _config(apk))

    assert not ok
    assert str(apk) in message and "F5R1_Accessibility_Attacker.apk" in message


def test_preflight_without_a_device_checks_only_the_apk(tmp_path):
    from mobile_playbook.platforms.android.preflight import check_requirements

    [(_, ok, _message)] = check_requirements(["accessibility_attacker"], None, _config(tmp_path / "missing.apk"))

    assert not ok


def test_the_risk_requires_the_attacker_app():
    from mobile_playbook.platforms.android.risks import get_risk

    assert "accessibility_attacker" in get_risk("android-feature-05-risk-01").requires
