import json
from types import SimpleNamespace

import pytest

from mobile_playbook.orchestration.scan_runner import RunOptions, run_platform
from mobile_playbook.platforms.android.config import load_config
from mobile_playbook.platforms.android.device.adb import AdbClient
from mobile_playbook.platforms.android.device.client import AndroidDeviceClient
from mobile_playbook.platforms.android.results import normalize_android_result
from mobile_playbook.platforms.android.runner import AndroidPlatformRunner
from mobile_playbook.reporting.report_writer import ReportWriter
from mobile_playbook.reporting.run_manifest import read_manifest
from tests.platforms.android.baseline_fakes import (
    PLAIN,
    ROOT,
    ROOTED_SERIAL,
    ROOTED_VM,
    STATIC,
    STOCK_SERIAL,
    STOCK_VM,
    BaselineBench,
)


def run(bench, tmp_path):
    return run_platform(
        load_config(bench.config_path), bench.runner(), RunOptions(out_dir=tmp_path / "reports"),
        lambda out, ts: ReportWriter(out, ts, result_adapter=normalize_android_result, platform="android"),
    )


def report(run_dir, risk_id):
    return json.loads((run_dir / "android/one" / risk_id / "synthetic/report.json").read_text())


@pytest.fixture
def bench(tmp_path, monkeypatch):
    bench = BaselineBench(tmp_path, monkeypatch, risks=(PLAIN, ROOT, STATIC))
    bench.declares_play_services = True
    bench.adb.devices[STOCK_SERIAL].play_services = False
    return bench


def test_a_device_risk_is_not_judged_on_a_vm_without_the_play_services_the_app_needs(bench, tmp_path):
    outcome = run(bench, tmp_path)

    blocked = report(outcome.run_dir, PLAIN)
    assert (blocked["final_status"], blocked["verdict"]) == ("FAILED", "Inconclusive")
    assert f"non_rooted device ({STOCK_SERIAL})" in blocked["errors"][0]
    assert blocked["metadata"]["device"]["instance_id"] == STOCK_VM
    assert bench.risks[PLAIN].used == []
    assert bench.risks[ROOT].used == [ROOTED_SERIAL]
    assert report(outcome.run_dir, ROOT)["final_status"] == "PASSED"
    assert bench.risks[STATIC].used == [None]
    assert read_manifest(outcome.run_dir)["status"] == "completed"
    assert bench.instance_state(STOCK_VM) == bench.instance_state(ROOTED_VM) == "off"


def test_an_app_set_to_work_without_play_services_is_tested_on_that_vm(bench, tmp_path):
    apps = tmp_path / "configs/split/android/apps.yaml"
    apps.write_text(apps.read_text().replace('package_name: "com.example.one"\n',
                                             'package_name: "com.example.one"\n    requires_play_services: false\n'))

    outcome = run(bench, tmp_path)

    assert bench.risks[PLAIN].used == [STOCK_SERIAL]
    assert report(outcome.run_dir, PLAIN)["final_status"] == "PASSED"


def test_an_app_whose_manifest_needs_no_play_services_is_tested_on_that_vm(bench, tmp_path):
    bench.declares_play_services = False

    run(bench, tmp_path)

    assert bench.risks[PLAIN].used == [STOCK_SERIAL]


@pytest.mark.parametrize("installed", [True, False])
def test_a_physical_phone_is_checked_the_same_way_before_its_screen_is_touched(adb_host, monkeypatch, installed):
    adb_host.add("phone-serial", state="device", play_services=installed)
    risk = SimpleNamespace(requires=["adb"], requires_device=True, runs=[], run=lambda *args: risk.runs.append(args))
    touched = []
    monkeypatch.setattr("mobile_playbook.platforms.android.runner.get_risk", lambda risk_id: risk)
    monkeypatch.setattr("mobile_playbook.platforms.android.runner.check_android_preflight",
                        lambda config, adb, requires: SimpleNamespace(ok=True, errors=[], warnings=[]))
    monkeypatch.setattr("mobile_playbook.platforms.android.runner.wake_and_unlock", touched.append)
    runner = AndroidPlatformRunner()
    failures = []
    monkeypatch.setattr(runner, "_failure_result_template", lambda *args: SimpleNamespace())
    monkeypatch.setattr(runner, "_record_failure", lambda result, writer, exc: failures.append(str(exc)))
    app = SimpleNamespace(id="one", name="One", package_name="com.example.one", requires_play_services=True)
    config = SimpleNamespace(runner=SimpleNamespace(auto_grant_permissions=False))

    runner.run_test(app, "risk", config, AndroidDeviceClient(None, AdbClient("adb", "phone-serial")), None)

    if installed:
        assert failures == [] and len(risk.runs) == 1 and len(touched) == 1
    else:
        assert risk.runs == [] and touched == []
        assert "test device (phone-serial)" in failures[0]
