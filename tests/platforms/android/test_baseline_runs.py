import json

import pytest

from mobile_playbook.artifact_store import resolver
from mobile_playbook.orchestration.scan_runner import RunOptions, run_platform
from mobile_playbook.platforms.android.config import load_config
from mobile_playbook.platforms.android.device.pool import AndroidDevicePool
from mobile_playbook.platforms.android.results import normalize_android_result
from mobile_playbook.reporting.report_writer import ReportWriter
from mobile_playbook.reporting.run_events import read_events
from mobile_playbook.reporting.run_manifest import read_manifest
from tests.platforms.android.baseline_fakes import (
    APK_NAME,
    PACKAGE,
    PLAIN,
    ROOT,
    ROOTED_SERIAL,
    ROOTED_VM,
    STATIC,
    STOCK_SERIAL,
    STOCK_VM,
    BaselineBench,
)


def run(bench, tmp_path, *risks):
    config = load_config(bench.config_path)
    selected = set(risks) if risks else None
    return run_platform(
        config, bench.runner(), RunOptions(out_dir=tmp_path / "reports", selected_tests=selected),
        lambda out, ts: ReportWriter(out, ts, result_adapter=normalize_android_result, platform="android"),
    )


def events(run_dir, kind):
    found, _ = read_events(run_dir)
    return [event for event in found if event["type"] == kind]


def last_run_dir(tmp_path):
    return max((tmp_path / "reports").iterdir())


@pytest.fixture
def bench(tmp_path, monkeypatch):
    return BaselineBench(tmp_path, monkeypatch, risks=(PLAIN, ROOT, STATIC))


def test_a_mixed_run_restores_each_vm_once_installs_on_both_and_confirms_both_off(bench, tmp_path):
    outcome = run(bench, tmp_path)

    assert bench.risks[PLAIN].used == [STOCK_SERIAL]
    assert bench.risks[ROOT].used == [ROOTED_SERIAL]
    for vm in (STOCK_VM, ROOTED_VM):
        assert bench.actions(vm, "restore") == ["restore"]
        assert bench.actions(vm, "restore", "start", "stop")[:2] == ["restore", "start"]
        assert bench.instance_state(vm) == "off"
    assert bench.store.pending() == []

    installed = events(outcome.run_dir, "app_installed")
    assert [(event["role"], event["instance_id"], event["version"], event["build_number"]) for event in installed] == [
        ("non_rooted", STOCK_VM, "3.2.1", "321"), ("rooted", ROOTED_VM, "3.2.1", "321")]
    sha = installed[0]["artifact_sha256"]
    assert installed[1]["artifact_sha256"] == sha and installed[0]["apk"] == APK_NAME
    assert read_manifest(outcome.run_dir)["artifacts"] == {"one": sha}
    assert read_manifest(outcome.run_dir)["status"] == "completed"
    cleanup = {event["role"]: event["status"] for event in events(outcome.run_dir, "device_cleanup")}
    assert cleanup == {"non_rooted": "off", "rooted": "off"}


def test_every_build_is_installed_after_restoration_and_before_the_first_risk(bench, tmp_path):
    order = []
    original = bench.adb._answer

    def watch(serial, args):
        if args[:3] == ["shell", "pm", "install"]:
            order.append(("install", serial, bench.instance_state(STOCK_VM if serial == STOCK_SERIAL else ROOTED_VM)))
        return original(serial, args)

    bench.adb._answer = watch
    bench.risks[PLAIN].on_run = lambda: order.append(("risk", PLAIN))
    bench.risks[ROOT].on_run = lambda: order.append(("risk", ROOT))

    run(bench, tmp_path, ROOT, PLAIN)

    assert order == [("install", STOCK_SERIAL, "on"), ("install", ROOTED_SERIAL, "on"), ("risk", PLAIN), ("risk", ROOT)]


def test_results_and_evidence_name_their_role_instance_baseline_and_build(bench, tmp_path):
    outcome = run(bench, tmp_path)

    for risk_id, role, vm in ((PLAIN, "non_rooted", STOCK_VM), (ROOT, "rooted", ROOTED_VM)):
        report = json.loads((outcome.run_dir / "android/one" / risk_id / "synthetic/report.json").read_text())
        device = report["metadata"]["device"]
        assert (device["role"], device["instance_id"], device["baseline_id"]) == (role, vm, f"baseline-{vm}")
        assert (device["version"], device["build_number"]) == ("3.2.1", "321")
        assert device["artifact_sha256"] == read_manifest(outcome.run_dir)["artifacts"]["one"]
        assert (outcome.run_dir / "android/one" / risk_id / "synthetic/evidence.txt").is_file()
    static = json.loads((outcome.run_dir / f"android/one/{STATIC}/synthetic/report.json").read_text())
    assert "device" not in static["metadata"]


def test_a_non_rooted_only_run_never_touches_the_rooted_vm(bench, tmp_path):
    run(bench, tmp_path, PLAIN)

    assert bench.actions(ROOTED_VM) == []
    assert bench.actions(STOCK_VM, "restore", "start", "stop") == ["restore", "start", "stop"]
    assert bench.risks[ROOT].used == []


def test_a_rooted_only_run_never_touches_the_non_rooted_vm(bench, tmp_path):
    run(bench, tmp_path, ROOT)

    assert bench.actions(STOCK_VM) == []
    assert bench.actions(ROOTED_VM, "restore", "start", "stop") == ["restore", "start", "stop"]


def test_a_static_only_run_claims_restores_starts_and_installs_nothing(bench, tmp_path):
    outcome = run(bench, tmp_path, STATIC)

    assert bench.api.data["calls"] == []
    assert bench.store.pending() == []
    assert not any(args[:3] == ["shell", "pm", "install"] for _, args in bench.adb.commands)
    assert events(outcome.run_dir, "device_claimed") == []
    assert bench.risks[STATIC].used == [None]


def test_a_missing_apk_fails_before_any_vm_is_claimed_or_changed(bench, tmp_path):
    bench.apk.unlink()

    with pytest.raises(RuntimeError, match="No APK of com.example.one"):
        run(bench, tmp_path, PLAIN)

    assert bench.api.data["calls"] == []
    assert bench.store.pending() == []
    assert read_manifest(last_run_dir(tmp_path))["status"] == "failed"


def test_an_unavailable_non_rooted_vm_is_a_clear_blocker_and_the_rooted_vm_never_substitutes(bench, tmp_path):
    del bench.api.data["instances"][STOCK_VM]

    with pytest.raises(Exception, match="missing or ambiguous"):
        run(bench, tmp_path, PLAIN)

    assert bench.actions(ROOTED_VM, "restore", "start", "stop") == []
    assert bench.store.pending() == []
    assert bench.risks[PLAIN].used == []


def test_a_run_needing_an_unconfigured_role_fails_without_touching_any_vm(tmp_path, monkeypatch):
    bench = BaselineBench(tmp_path, monkeypatch, risks=(PLAIN, ROOT), roles=("non_rooted",))

    with pytest.raises(RuntimeError, match="needs the rooted device"):
        run(bench, tmp_path)

    assert bench.api.data["calls"] == []


def test_a_build_that_reports_another_version_fails_setup_and_shuts_both_vms_down(bench, tmp_path):
    bench.report_version("3.2.0", "320")

    with pytest.raises(RuntimeError, match="not the intended 3.2.1"):
        run(bench, tmp_path, PLAIN, ROOT)

    for vm in (STOCK_VM, ROOTED_VM):
        assert bench.instance_state(vm) == "off"
    assert bench.store.pending() == []
    assert bench.risks[PLAIN].used == []
    cleanup = events(last_run_dir(tmp_path), "device_cleanup")
    assert sorted(event["status"] for event in cleanup) == ["off", "off"]


def test_a_connection_failure_after_preparation_still_shuts_down_every_claimed_vm(bench, tmp_path):
    bench.adb.devices[ROOTED_SERIAL].connects_to = "absent"

    with pytest.raises(RuntimeError, match=ROOTED_SERIAL):
        run(bench, tmp_path)

    for vm in (STOCK_VM, ROOTED_VM):
        assert bench.actions(vm, "restore", "start", "stop") == ["restore", "start", "stop"]
    assert bench.store.pending() == []


def test_one_shutdown_failure_leaves_the_other_vm_off_and_keeps_its_own_ownership_pending(bench, tmp_path):
    bench.risks[ROOT].on_run = lambda: bench.stuck_off(ROOTED_VM)

    outcome = run(bench, tmp_path)

    assert bench.instance_state(STOCK_VM) == "off"
    assert [row["instance_id"] for row in bench.store.pending()] == [ROOTED_VM]
    cleanup = {event["role"]: event for event in events(outcome.run_dir, "device_cleanup")}
    assert cleanup["non_rooted"]["status"] == "off"
    assert cleanup["rooted"]["status"] == "cleanup_pending" and cleanup["rooted"]["error"]
    assert read_manifest(outcome.run_dir)["status"] == "completed"
    verdicts = json.loads((outcome.run_dir / "dashboard_results.json").read_text())
    assert {item["verdict"] for item in verdicts} == {"Reduced Risk"}


def test_closing_sessions_that_fails_never_stops_the_shutdown(bench, tmp_path, monkeypatch):
    def broken_release(self):
        raise RuntimeError("evidence pull and session close failed")

    monkeypatch.setattr(AndroidDevicePool, "release", broken_release)

    run(bench, tmp_path)

    for vm in (STOCK_VM, ROOTED_VM):
        assert bench.instance_state(vm) == "off"
    assert bench.store.pending() == []


def test_cancelling_mid_run_shuts_both_vms_down_before_the_interrupt_propagates(bench, tmp_path):
    bench.risks[PLAIN].fail_with = KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        run(bench, tmp_path)

    for vm in (STOCK_VM, ROOTED_VM):
        assert bench.instance_state(vm) == "off"
    assert bench.store.pending() == []
    assert read_manifest(last_run_dir(tmp_path))["status"] == "failed"


def test_appium_recovery_reconnects_the_same_instances_without_restoring_or_reinstalling(bench, tmp_path):
    bench.appium_reachable = [True, False]

    run(bench, tmp_path, PLAIN, ROOT)

    for vm in (STOCK_VM, ROOTED_VM):
        assert bench.actions(vm, "restore") == ["restore"]
        assert bench.actions(vm, "start") == ["start"]
    installs = [serial for serial, args in bench.adb.commands if args[:3] == ["shell", "pm", "install"]]
    assert installs == [STOCK_SERIAL, ROOTED_SERIAL]
    assert bench.risks[ROOT].used == [ROOTED_SERIAL]
    assert len(events(last_run_dir(tmp_path), "appium_recovery")) == 1


def test_a_baseline_run_reads_the_installed_version_from_every_role(bench, tmp_path):
    outcome = run(bench, tmp_path)

    assert read_manifest(outcome.run_dir)["artifact_versions"] == {"one": "3.2.1"}
    assert [event["version"] for event in events(outcome.run_dir, "app_version")] == ["3.2.1"]
    assert events(outcome.run_dir, "app_version_changed") == []
    assert PACKAGE in bench.adb.devices[ROOTED_SERIAL].packages


def test_the_recorded_artifact_is_the_installed_apk_even_when_an_older_pulled_copy_exists(bench, tmp_path, monkeypatch):
    stale = tmp_path / "repackaging/com_example_one/original/base.apk"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"an older build pulled by an earlier run")
    monkeypatch.setattr(resolver, "ANDROID_WORKFLOW_APK_DIR", tmp_path / "repackaging")

    outcome = run(bench, tmp_path, PLAIN)

    installed = events(outcome.run_dir, "app_installed")[0]["artifact_sha256"]
    assert read_manifest(outcome.run_dir)["artifacts"] == {"one": installed}
