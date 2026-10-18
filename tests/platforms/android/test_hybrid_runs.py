import json

import pytest

from mobile_playbook.orchestration.scan_runner import RunOptions, run_platform
from mobile_playbook.platforms.android.config import load_config
from mobile_playbook.platforms.android.device import phone_role
from mobile_playbook.platforms.android.device.adb_connection import AdbConnectionError
from mobile_playbook.platforms.android.device.phone_lock import PhoneBusy, acquire_phone_lock, phone_lock_holder
from mobile_playbook.platforms.android.results import normalize_android_result
from mobile_playbook.platforms.corellium.client import CorelliumError
from mobile_playbook.reporting.report_writer import ReportWriter
from mobile_playbook.reporting.run_events import read_events
from mobile_playbook.reporting.run_manifest import read_manifest
from tests.platforms.android.baseline_fakes import (
    PACKAGE,
    PHONE_SERIAL,
    PLAIN,
    ROOT,
    ROOTED_SERIAL,
    ROOTED_VM,
    STATIC,
    STOCK_VM,
    BaselineBench,
)

MUTATIONS = {"restore", "start", "stop", "unpause"}


@pytest.fixture
def bench(tmp_path, monkeypatch):
    bench = BaselineBench(tmp_path, monkeypatch, risks=(PLAIN, ROOT, STATIC), physical_roles=("non_rooted",))
    bench.phone.packages["com.example.personal"] = ("1.0", "1")
    return bench


def run(bench, tmp_path, *risks):
    return run_platform(
        load_config(bench.config_path), bench.runner(),
        RunOptions(out_dir=tmp_path / "reports", selected_tests=set(risks) or None),
        lambda out, ts: ReportWriter(out, ts, result_adapter=normalize_android_result, platform="android"),
    )


def events(run_dir, kind=None):
    found, _ = read_events(run_dir)
    return [event for event in found if kind is None or event["type"] == kind]


def last_run_dir(tmp_path):
    return max((tmp_path / "reports").iterdir())


def report(run_dir, risk_id):
    return json.loads((run_dir / "android/one" / risk_id / "synthetic/report.json").read_text())


def phone_commands(bench, *prefix):
    return [args for serial, args in bench.adb.commands if serial == PHONE_SERIAL and args[:len(prefix)] == list(prefix)]


def test_a_mixed_run_tests_on_the_phone_and_the_rooted_vm_and_cleans_both_up(bench, tmp_path):
    outcome = run(bench, tmp_path)

    assert bench.risks[PLAIN].used == [PHONE_SERIAL]
    assert bench.risks[ROOT].used == [ROOTED_SERIAL]
    assert bench.actions(ROOTED_VM, *MUTATIONS) == ["restore", "start", "stop"]
    assert bench.actions(STOCK_VM) == []
    order = [event["type"] for event in events(outcome.run_dir) if event["type"].startswith("device_")]
    assert order[:2] == ["device_connected", "device_claimed"]
    phone, vm = report(outcome.run_dir, PLAIN)["metadata"]["device"], report(outcome.run_dir, ROOT)["metadata"]["device"]
    assert (phone["device_type"], phone["serial"], phone["role"]) == ("physical_phone", PHONE_SERIAL, "non_rooted")
    assert "instance_id" not in phone
    assert (vm["device_type"], vm["serial"], vm["instance_id"]) == ("corellium_vm", ROOTED_SERIAL, ROOTED_VM)
    assert phone["artifact_sha256"] == vm["artifact_sha256"] == read_manifest(outcome.run_dir)["artifacts"]["one"]
    installed = {event["device_type"]: event["serial"] for event in events(outcome.run_dir, "app_installed")}
    assert installed == {"physical_phone": PHONE_SERIAL, "corellium_vm": ROOTED_SERIAL}
    cleanup = {event["role"]: event for event in events(outcome.run_dir, "device_cleanup")}
    assert (cleanup["non_rooted"]["status"], cleanup["non_rooted"]["removed"]) == ("cleaned", [PACKAGE])
    assert cleanup["rooted"]["status"] == "off"
    assert set(bench.phone.packages) == {"com.example.personal"}
    assert bench.phone.state == "device" and phone_commands(bench, "disconnect") == []
    assert bench.adb.sent(None, "disconnect", PHONE_SERIAL) == 0
    assert bench.instance_state(ROOTED_VM) == "off" and bench.store.pending() == []
    assert phone_lock_holder() is None and read_manifest(outcome.run_dir)["status"] == "completed"


def test_a_non_rooted_only_run_uses_only_the_phone_and_never_contacts_corellium(bench, tmp_path):
    seen = []
    bench.risks[PLAIN].on_run = lambda: seen.append(phone_lock_holder()["purpose"])

    outcome = run(bench, tmp_path, PLAIN)

    assert bench.api.data["calls"] == []
    assert bench.risks[PLAIN].used == [PHONE_SERIAL]
    assert len(seen) == 1 and seen[0].startswith("Android run android-")
    assert [event["device_type"] for event in events(outcome.run_dir, "device_connected")] == ["physical_phone"]
    assert phone_lock_holder() is None
    assert set(bench.phone.packages) == {"com.example.personal"}


def test_a_rooted_only_run_never_touches_or_locks_the_phone(bench, tmp_path):
    seen = []
    bench.risks[ROOT].on_run = lambda: seen.append(phone_lock_holder())

    run(bench, tmp_path, ROOT)

    assert phone_commands(bench) == [] and seen == [None]
    assert bench.risks[ROOT].used == [ROOTED_SERIAL]


def test_a_copy_already_on_the_phone_is_uninstalled_before_the_intended_build_and_gone_afterwards(bench, tmp_path):
    bench.phone.packages[PACKAGE] = ("3.0.0", "300")
    versions = []
    bench.risks[PLAIN].on_run = lambda: versions.append(bench.phone.packages.get(PACKAGE))

    outcome = run(bench, tmp_path, PLAIN)

    uninstall_then_install = [args[0] if args[0] == "uninstall" else " ".join(args[:3]) for args in phone_commands(bench)
                              if args[:1] == ["uninstall"] or args[:3] == ["shell", "pm", "install"]]
    assert uninstall_then_install[:2] == ["uninstall", "shell pm install"]
    assert versions == [("3.2.1", "321")]
    assert PACKAGE not in bench.phone.packages
    assert events(outcome.run_dir, "device_cleanup")[0]["removed"] == [PACKAGE]


def test_packages_a_risk_installs_are_removed_and_the_phones_own_apps_are_kept(bench, tmp_path):
    bench.risks[PLAIN].on_run = lambda: bench.phone.packages.update({"io.appium.uiautomator2.server": ("1", "1"),
                                                                     "com.example.capturehelper": ("1", "1")})

    outcome = run(bench, tmp_path, PLAIN)

    assert set(bench.phone.packages) == {"com.example.personal"}
    assert events(outcome.run_dir, "device_cleanup")[0]["removed"] == [
        "com.example.capturehelper", PACKAGE, "io.appium.uiautomator2.server"]


@pytest.mark.parametrize(("change", "message"), [
    ({"state": "absent"}, "Check that the phone is connected"),
    ({"build_type": "userdebug"}, "rooted, but a non-rooted device was expected"),
    ({"state": "unauthorized"}, "tap Allow on its USB debugging prompt"),
])
def test_an_unusable_phone_fails_the_run_before_any_vm_is_touched(bench, tmp_path, change, message):
    for key, value in change.items():
        setattr(bench.phone, key, value)

    with pytest.raises(AdbConnectionError, match=message):
        run(bench, tmp_path)

    assert not [call for call in bench.api.data["calls"] if call[0] in MUTATIONS] and bench.store.pending() == []
    assert phone_lock_holder() is None
    assert read_manifest(last_run_dir(tmp_path))["status"] == "failed"


def test_a_phone_in_use_elsewhere_fails_the_run_before_any_vm_is_touched(bench, tmp_path):
    other = acquire_phone_lock("Android install of one")
    try:
        with pytest.raises(PhoneBusy, match="in use by Android install of one"):
            run(bench, tmp_path)
    finally:
        other.release()

    assert not [call for call in bench.api.data["calls"] if call[0] in MUTATIONS]
    assert phone_commands(bench) == []


def test_a_vm_preparation_failure_leaves_the_phone_as_it_was_and_the_vm_off(bench, tmp_path):
    bench.api.data["failures"][("restore", ROOTED_VM)] = [CorelliumError("snapshot is locked")]

    with pytest.raises(CorelliumError, match="snapshot is locked"):
        run(bench, tmp_path)

    assert set(bench.phone.packages) == {"com.example.personal"} and phone_commands(bench, "uninstall") == []
    assert bench.instance_state(ROOTED_VM) == "off" and bench.store.pending() == []
    cleanup = {event["role"]: event for event in events(last_run_dir(tmp_path), "device_cleanup")}
    assert (cleanup["non_rooted"]["status"], cleanup["non_rooted"]["removed"]) == ("cleaned", [])
    assert phone_lock_holder() is None


def test_a_package_the_phone_refuses_to_remove_is_reported_and_the_vm_still_shuts_down(bench, tmp_path):
    bench.phone.uninstall_fails.add(PACKAGE)

    outcome = run(bench, tmp_path)

    cleanup = {event["role"]: event for event in events(outcome.run_dir, "device_cleanup")}
    assert (cleanup["non_rooted"]["status"], cleanup["non_rooted"]["left"]) == ("cleanup_incomplete", [PACKAGE])
    assert cleanup["rooted"]["status"] == "off" and bench.instance_state(ROOTED_VM) == "off"
    assert read_manifest(outcome.run_dir)["status"] == "completed"
    assert phone_lock_holder() is None


def test_cancelling_mid_run_cleans_the_phone_and_shuts_the_vm_down(bench, tmp_path):
    bench.risks[ROOT].fail_with = KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        run(bench, tmp_path)

    assert set(bench.phone.packages) == {"com.example.personal"}
    assert bench.instance_state(ROOTED_VM) == "off" and bench.store.pending() == []
    assert phone_lock_holder() is None


def test_appium_recovery_reconnects_the_phone_by_serial_and_the_vm_without_restoring(bench, tmp_path, caplog):
    bench.appium_reachable = [True, False]

    with caplog.at_level("INFO", logger="mobile_playbook.platforms.android.runner"):
        run(bench, tmp_path, PLAIN, ROOT)

    assert f"reconnected the non_rooted device to the phone {PHONE_SERIAL}." in caplog.text
    assert f"reconnected the rooted device to Corellium instance {ROOTED_VM}." in caplog.text

    assert bench.actions(ROOTED_VM, "restore") == ["restore"]
    assert bench.adb.sent(None, "connect", PHONE_SERIAL) == 0
    assert bench.risks[ROOT].used == [ROOTED_SERIAL]
    assert len(phone_commands(bench, "shell", "pm", "install")) == 1


def test_a_wifi_phone_is_resolved_with_the_rooted_vms_address_excluded(bench, tmp_path, monkeypatch):
    bench.config_path.write_text(bench.config_path.read_text().replace(f'adb_serial: "{PHONE_SERIAL}"', 'adb_serial: "wifi"'))
    seen = []
    monkeypatch.setattr(phone_role, "connect_over_wifi", lambda adb_path, exclude: seen.append(set(exclude)) or PHONE_SERIAL)

    run(bench, tmp_path, PLAIN)

    assert ROOTED_SERIAL in seen[0]
    assert bench.risks[PLAIN].used == [PHONE_SERIAL]


def test_only_the_vm_is_checked_for_app_compilation_before_installing(bench, tmp_path):
    run(bench, tmp_path)

    assert phone_commands(bench, "shell", "ps", "-A", "-o", "ARGS") == []
    assert bench.adb.sent(ROOTED_SERIAL, "shell", "ps", "-A", "-o", "ARGS") >= 1
