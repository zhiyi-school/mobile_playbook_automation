import json
import math
from types import SimpleNamespace

import pytest

from mobile_playbook.artifact_store import resolver
from mobile_playbook.orchestration.scan_runner import RunOptions, run_platform
from mobile_playbook.platforms.android.config import load_config
from mobile_playbook.platforms.android.device import install as install_module
from mobile_playbook.platforms.android.device.debugging_prompt import DebuggingAuthorizationError
from mobile_playbook.platforms.android.device.pool import AndroidDevicePool
from mobile_playbook.platforms.android.runner import AndroidPlatformRunner
from mobile_playbook.platforms.corellium.claims import ClaimBusy
from mobile_playbook.platforms.corellium.client import CorelliumClient, CorelliumError
from mobile_playbook.platforms.corellium.lifecycle import CorelliumLifecycle
from mobile_playbook.platforms.android.results import normalize_android_result
from mobile_playbook.reporting.report_writer import ReportWriter
from mobile_playbook.reporting.run_events import read_events
from mobile_playbook.reporting.run_manifest import read_manifest
from tests.platforms.android.baseline_fakes import (
    APK_NAME,
    FAST,
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
from tests.platforms.android.prompt_fakes import line, png

MUTATIONS = {"restore", "start", "stop", "unpause"}


def run(bench, tmp_path, *risks, runner=None):
    config = load_config(bench.config_path)
    selected = set(risks) if risks else None
    return run_platform(
        config, runner or bench.runner(), RunOptions(out_dir=tmp_path / "reports", selected_tests=selected),
        lambda out, ts: ReportWriter(out, ts, result_adapter=normalize_android_result, platform="android"),
    )


def events(run_dir, kind):
    found, _ = read_events(run_dir)
    return [event for event in found if event["type"] == kind]


def last_run_dir(tmp_path):
    return max((tmp_path / "reports").iterdir())


def installs(bench, serial):
    return sum(1 for target, args in bench.adb.commands if target == serial and args[:3] == ["shell", "pm", "install"])


def report(run_dir, risk_id):
    return json.loads((run_dir / "android/one" / risk_id / "synthetic/report.json").read_text())


def recover(bench):
    return CorelliumLifecycle(bench.api, bench.store, FAST).recover()


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


def compilation_checks(bench, serial):
    return [index for index, (target, args) in enumerate(bench.adb.commands)
            if target == serial and args == ["shell", "ps", "-A", "-o", "ARGS"]]


def install_commands(bench, serial):
    return [index for index, (target, args) in enumerate(bench.adb.commands)
            if target == serial and args[:3] == ["shell", "pm", "install"]]


@pytest.fixture
def dexopt_budget(monkeypatch):
    # raising=False lets these tests run, and fail on behavior, against code without the compilation wait.
    def set_budget(seconds=900.0):
        monkeypatch.setattr(install_module, "DEXOPT_POLL_SECONDS", 0.001, raising=False)
        monkeypatch.setattr(install_module, "DEXOPT_WAIT_SECONDS", seconds, raising=False)
    return set_budget


def test_an_install_on_a_restored_vm_waits_for_its_compilation_to_finish(bench, tmp_path, dexopt_budget, caplog):
    dexopt_budget()
    bench.adb.devices[ROOTED_SERIAL].compiling = 3
    caplog.set_level("INFO", logger=install_module.__name__)

    outcome = run(bench, tmp_path, ROOT)

    assert read_manifest(outcome.run_dir)["status"] == "completed"
    assert bench.risks[ROOT].used == [ROOTED_SERIAL]
    checks, install = compilation_checks(bench, ROOTED_SERIAL), install_commands(bench, ROOTED_SERIAL)
    assert len(checks) == 5 and len(install) == 1 and checks[-1] < install[0]
    assert "dex2oat32 on /data/app/~~a==/com.google.android.gms-b==/base.apk" in caplog.text
    assert bench.instance_state(ROOTED_VM) == "off"


def test_a_vm_that_never_finishes_compiling_fails_the_install_clearly_and_shuts_down(bench, tmp_path, dexopt_budget):
    dexopt_budget(0.05)
    bench.adb.devices[ROOTED_SERIAL].compiling = math.inf

    with pytest.raises(RuntimeError, match=r"rooted device \(instance rooted-vm\) was still compiling apps after \d+ s "
                                            r"\(dex2oat32 on /data/app/.*com\.google\.android\.gms.*\), so One_3\.2\.1\.apk "
                                            r"was not installed"):
        run(bench, tmp_path, ROOT)

    assert install_commands(bench, ROOTED_SERIAL) == []
    assert bench.risks[ROOT].used == []
    assert bench.actions(ROOTED_VM, "restore", "start", "stop") == ["restore", "start", "stop"]
    assert bench.store.pending() == []


def test_a_standalone_install_on_a_compiling_vm_reports_a_retryable_failure(bench, tmp_path, dexopt_budget):
    dexopt_budget(0.05)
    bench.adb.devices[ROOTED_SERIAL].compiling = math.inf
    config = load_config(bench.config_path)

    [(_, outcome)] = bench.runner().install_on_baseline(config, config.apps, "rooted", tmp_path / "install-work")

    assert (outcome.status, outcome.blocker_code, outcome.retryable) == ("failed", "app_install_failed", True)
    assert "still compiling apps" in outcome.detail
    assert install_commands(bench, ROOTED_SERIAL) == [] and bench.instance_state(ROOTED_VM) == "off"


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


def test_an_unknown_debugging_dialog_saves_diagnostics_fails_clearly_and_shuts_both_vms_down(bench, tmp_path):
    bench.adb.devices[ROOTED_SERIAL].connects_to = "unauthorized"
    bench.api.data["screens"][ROOTED_VM] = png(1080, 2400)
    unfamiliar = line(1, 300, "Allow", "Files", "to", "access", "photos?")
    runner = bench.runner(recognizer=SimpleNamespace(words=lambda image: unfamiliar))

    with pytest.raises(DebuggingAuthorizationError, match=f"instance {ROOTED_VM}: an unfamiliar screen"):
        run(bench, tmp_path, runner=runner)

    folder = last_run_dir(tmp_path) / "debug-authorization"
    details = json.loads(next(folder.glob("*.json")).read_text())
    assert (details["instance_id"], details["serial"]) == (ROOTED_VM, ROOTED_SERIAL)
    assert details["recognized_lines"] == ["Allow Files to access photos?"]
    assert (folder / details["screenshot"]).read_bytes() == png(1080, 2400)
    assert bench.actions(ROOTED_VM, "touch") == []
    for vm in (STOCK_VM, ROOTED_VM):
        assert bench.actions(vm, "restore", "start", "stop") == ["restore", "start", "stop"]
    assert bench.store.pending() == []
    assert bench.risks[PLAIN].used == bench.risks[ROOT].used == []
    assert read_manifest(last_run_dir(tmp_path))["status"] == "failed"
    cleanup = {event["role"]: event["status"] for event in events(last_run_dir(tmp_path), "device_cleanup")}
    assert cleanup == {"non_rooted": "off", "rooted": "off"}


def test_a_preparation_failure_shuts_down_the_vm_already_started_and_installs_nothing(bench, tmp_path):
    bench.api.data["failures"][("restore", ROOTED_VM)] = [CorelliumError("snapshot is locked")]

    with pytest.raises(CorelliumError, match="snapshot is locked"):
        run(bench, tmp_path)

    assert bench.actions(STOCK_VM, "restore", "start", "stop") == ["restore", "start", "stop"]
    assert bench.actions(ROOTED_VM, *MUTATIONS) == ["restore"]
    assert bench.instance_state(STOCK_VM) == bench.instance_state(ROOTED_VM) == "off"
    assert bench.store.pending() == []
    assert installs(bench, STOCK_SERIAL) == installs(bench, ROOTED_SERIAL) == 0
    assert events(last_run_dir(tmp_path), "device_connected") == []
    cleanup = {event["role"]: event["status"] for event in events(last_run_dir(tmp_path), "device_cleanup")}
    assert cleanup == {"non_rooted": "off", "rooted": "off"}


def test_a_preparation_failure_whose_shutdown_also_fails_stays_owned_until_recovery(bench, tmp_path):
    restore = bench.api.restore_snapshot

    def restore_or_lock(instance_id, snapshot_id):
        if instance_id != ROOTED_VM:
            return restore(instance_id, snapshot_id)
        bench.api.call("restore", instance_id)
        bench.stuck_off(STOCK_VM)
        raise CorelliumError("snapshot is locked")

    bench.api.restore_snapshot = restore_or_lock

    with pytest.raises(CorelliumError, match="snapshot is locked"):
        run(bench, tmp_path)

    assert [(row["instance_id"], row["phase"]) for row in bench.store.pending()] == [(STOCK_VM, "cleanup_pending")]
    assert bench.instance_state(ROOTED_VM) == "off"
    bench.api.data["stuck"].clear()
    assert recover(bench) == [{"instance_id": STOCK_VM, "status": "off"}]
    assert bench.instance_state(STOCK_VM) == "off" and bench.store.pending() == []


@pytest.mark.parametrize("step", ["restore", "connect", "install"])
def test_cancelling_before_the_device_pool_exists_shuts_down_every_claimed_vm(bench, tmp_path, step):
    answer = bench.adb._answer
    trigger = {"connect": ["connect", ROOTED_SERIAL], "install": ["shell", "pm", "install"]}.get(step)

    def interrupt(serial, args):
        if trigger and args[:len(trigger)] == trigger:
            raise KeyboardInterrupt
        return answer(serial, args)

    bench.adb._answer = interrupt
    if step == "restore":
        bench.api.data["failures"][("restore", ROOTED_VM)] = [KeyboardInterrupt()]

    with pytest.raises(KeyboardInterrupt):
        run(bench, tmp_path)

    assert bench.instance_state(STOCK_VM) == bench.instance_state(ROOTED_VM) == "off"
    assert bench.store.pending() == []
    assert bench.risks[PLAIN].used == bench.risks[ROOT].used == []
    assert read_manifest(last_run_dir(tmp_path))["status"] == "failed"


def test_a_failed_evidence_pull_fails_only_its_risk_and_both_vms_still_shut_down(bench, tmp_path):
    bench.risks[PLAIN].fail_with = RuntimeError("adb pull of the screen recording failed")
    states = []
    bench.risks[ROOT].on_run = lambda: states.append(bench.instance_state(ROOTED_VM))

    outcome = run(bench, tmp_path, PLAIN, ROOT)

    failed = report(outcome.run_dir, PLAIN)
    assert failed["final_status"] == "FAILED"
    assert "adb pull of the screen recording failed" in json.dumps(failed["errors"])
    assert (failed["metadata"]["device"]["role"], failed["metadata"]["device"]["instance_id"]) == ("non_rooted", STOCK_VM)
    assert bench.risks[ROOT].used == [ROOTED_SERIAL] and states == ["on"]
    assert (outcome.run_dir / f"android/one/{ROOT}/synthetic/evidence.txt").read_text() == f"ran on {ROOTED_SERIAL}\n"
    assert bench.instance_state(STOCK_VM) == bench.instance_state(ROOTED_VM) == "off"
    assert bench.store.pending() == []


def test_an_api_outage_at_shutdown_keeps_both_claims_pending_until_connectivity_returns(bench, tmp_path, monkeypatch):
    outage = []
    get_instance = bench.api.get_instance

    def maybe_unreachable(instance_id):
        if outage:
            bench.api.call("get", instance_id)
            raise CorelliumError("Corellium API unreachable", transient=True)
        return get_instance(instance_id)

    monkeypatch.setattr(bench.api, "get_instance", maybe_unreachable)
    bench.risks[ROOT].on_run = lambda: outage.append(True)

    outcome = run(bench, tmp_path, PLAIN, ROOT)

    cleanup = {event["role"]: event for event in events(outcome.run_dir, "device_cleanup")}
    assert {role: event["status"] for role, event in cleanup.items()} == {"non_rooted": "cleanup_pending",
                                                                          "rooted": "cleanup_pending"}
    assert all("unreachable" in event["error"] for event in cleanup.values())
    assert read_manifest(outcome.run_dir)["status"] == "completed"
    assert {item["verdict"] for item in json.loads((outcome.run_dir / "dashboard_results.json").read_text())} == {"Reduced Risk"}
    assert {row["instance_id"] for row in bench.store.pending()} == {STOCK_VM, ROOTED_VM}
    assert {item["status"] for item in recover(bench)} == {"cleanup_pending"}

    outage.clear()
    assert recover(bench) == [{"instance_id": ROOTED_VM, "status": "off"}, {"instance_id": STOCK_VM, "status": "off"}]
    assert bench.instance_state(STOCK_VM) == bench.instance_state(ROOTED_VM) == "off"
    assert bench.store.pending() == []
    assert read_manifest(run(bench, tmp_path, PLAIN).run_dir)["status"] == "completed"


def test_a_vm_left_pending_blocks_runs_that_need_it_until_recovery_but_not_other_runs(bench, tmp_path):
    bench.risks[ROOT].on_run = lambda: bench.stuck_off(ROOTED_VM)
    run(bench, tmp_path, PLAIN, ROOT)
    assert [row["instance_id"] for row in bench.store.pending()] == [ROOTED_VM]
    bench.risks[ROOT].on_run = None

    mark = len(bench.api.data["calls"])
    with pytest.raises(ClaimBusy, match="pending ownership"):
        run(bench, tmp_path, PLAIN, ROOT)
    assert not [call for call in bench.api.data["calls"][mark:] if call[0] in MUTATIONS]
    assert bench.risks[PLAIN].used == [STOCK_SERIAL]

    mark = len(bench.api.data["calls"])
    assert read_manifest(run(bench, tmp_path, PLAIN).run_dir)["status"] == "completed"
    assert {target for _, target in bench.api.data["calls"][mark:]} - {"all"} == {STOCK_VM}

    bench.api.data["stuck"].clear()
    assert recover(bench) == [{"instance_id": ROOTED_VM, "status": "off"}]
    outcome = run(bench, tmp_path, PLAIN, ROOT)
    assert read_manifest(outcome.run_dir)["status"] == "completed"
    assert bench.risks[ROOT].used == [ROOTED_SERIAL, ROOTED_SERIAL]
    assert bench.store.pending() == []


def test_an_assessment_after_a_standalone_install_restores_and_installs_its_own_build_again(bench, tmp_path):
    restore = bench.api.restore_snapshot

    def restore_wipes_apps(instance_id, snapshot_id):
        restore(instance_id, snapshot_id)
        serial = STOCK_SERIAL if instance_id == STOCK_VM else ROOTED_SERIAL
        bench.adb.devices[serial].packages.clear()

    bench.api.restore_snapshot = restore_wipes_apps
    config = load_config(bench.config_path)

    outcomes = bench.runner().install_on_baseline(config, config.apps, "non_rooted", tmp_path / "install-work")

    assert [outcome.status for _, outcome in outcomes] == ["installed"]
    assert bench.instance_state(STOCK_VM) == "off" and bench.actions(ROOTED_VM) == []
    outcome = run(bench, tmp_path, PLAIN)
    assert bench.actions(STOCK_VM, "restore", "start", "stop") == ["restore", "start", "stop"] * 2
    assert installs(bench, STOCK_SERIAL) == 2
    assert events(outcome.run_dir, "app_installed")[0]["artifact_sha256"] == read_manifest(outcome.run_dir)["artifacts"]["one"]
    assert bench.actions(ROOTED_VM) == []


def test_a_physical_device_run_never_contacts_corellium_or_claims_a_vm(tmp_path, monkeypatch):
    bench = BaselineBench(tmp_path, monkeypatch, risks=(PLAIN, ROOT))
    text = bench.config_path.read_text()
    device_block = text[:text.index("\nrunner:")]
    bench.config_path.write_text(text.replace(device_block, (
        "device:\n  provider: physical\n  adb_path: adb\n  adb_serial: \"phone-serial\"\n"
        "  appium_server_url: \"http://127.0.0.1:4723\"\n"
    )))
    bench.adb.add("phone-serial", state="device", build_type="user", packages={"com.example.one": ("3.2.1", "321")})

    def refuse(*args, **kwargs):
        pytest.fail("a physical-device run must not use the Corellium lifecycle")

    monkeypatch.setattr(CorelliumClient, "from_env", classmethod(refuse))
    monkeypatch.setattr(CorelliumLifecycle, "__init__", refuse)

    outcome = run(bench, tmp_path, runner=AndroidPlatformRunner())

    assert bench.risks[PLAIN].used == bench.risks[ROOT].used == ["phone-serial"]
    assert bench.api.data["calls"] == []
    assert events(outcome.run_dir, "device_claimed") == []
    assert read_manifest(outcome.run_dir)["status"] == "completed"
    assert "device" not in report(outcome.run_dir, PLAIN)["metadata"]
