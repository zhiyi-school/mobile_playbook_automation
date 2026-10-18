import pytest

from mobile_playbook.orchestration.scan_runner import RunOptions, run_platform
from mobile_playbook.platforms.android import runner as runner_module
from mobile_playbook.platforms.android.config import load_config
from mobile_playbook.platforms.android.device.phone_lock import PhoneBusy, acquire_phone_lock, phone_lock_holder
from mobile_playbook.platforms.android.results import normalize_android_result
from mobile_playbook.platforms.android.runner import AndroidPlatformRunner
from mobile_playbook.reporting.report_writer import ReportWriter
from mobile_playbook.reporting.run_manifest import read_manifest
from tests.platforms.android.baseline_fakes import PLAIN, ROOT, STATIC, STOCK_SERIAL, BaselineBench

PHONE = "phone-serial"


def run(bench, tmp_path, *risks, runner=None):
    return run_platform(
        load_config(bench.config_path), runner or AndroidPlatformRunner(),
        RunOptions(out_dir=tmp_path / "reports", selected_tests=set(risks) or None),
        lambda out, ts: ReportWriter(out, ts, result_adapter=normalize_android_result, platform="android"),
    )


def holders_during(bench, *risk_ids):
    seen = []
    for risk_id in risk_ids:
        bench.risks[risk_id].on_run = lambda: seen.append(phone_lock_holder())
    return seen


@pytest.fixture
def phone_bench(tmp_path, monkeypatch):
    bench = BaselineBench(tmp_path, monkeypatch, risks=(PLAIN, ROOT, STATIC))
    text = bench.config_path.read_text()
    bench.config_path.write_text(text.replace(text[:text.index("\nrunner:")], (
        f"device:\n  provider: physical\n  adb_path: adb\n  adb_serial: \"{PHONE}\"\n"
        "  appium_server_url: \"http://127.0.0.1:4723\"\n"
    )))
    bench.adb.add(PHONE, state="device", build_type="user", packages={"com.example.one": ("3.2.1", "321")})
    return bench


def test_a_phone_run_holds_the_lock_for_every_risk_and_releases_it_at_the_end(phone_bench, tmp_path):
    seen = holders_during(phone_bench, PLAIN, ROOT)

    outcome = run(phone_bench, tmp_path)

    assert [holder["purpose"] for holder in seen] == [f"Android run {outcome.run_timestamp}"] * 2
    assert phone_bench.risks[PLAIN].used == phone_bench.risks[ROOT].used == [PHONE]
    assert phone_lock_holder() is None


def test_a_run_while_the_phone_is_in_use_fails_before_touching_it(phone_bench, tmp_path, monkeypatch):
    monkeypatch.setattr(runner_module, "open_device", lambda config: pytest.fail("the phone must not be opened"))
    other = acquire_phone_lock("Android install of one")
    try:
        with pytest.raises(PhoneBusy, match="in use by Android install of one"):
            run(phone_bench, tmp_path)
    finally:
        other.release()

    assert phone_bench.risks[PLAIN].used == []
    assert not [serial for serial, _ in phone_bench.adb.commands if serial == PHONE]
    assert read_manifest(max((tmp_path / "reports").iterdir()))["status"] == "failed"


def test_a_failed_connection_releases_the_phone(phone_bench, tmp_path, monkeypatch):
    def unreachable(config):
        raise RuntimeError("adb could not reach the phone")

    monkeypatch.setattr(runner_module, "open_device", unreachable)

    with pytest.raises(RuntimeError, match="could not reach the phone"):
        run(phone_bench, tmp_path)

    assert phone_lock_holder() is None


def test_a_cancelled_run_releases_the_phone(phone_bench, tmp_path):
    phone_bench.risks[PLAIN].fail_with = KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        run(phone_bench, tmp_path)

    assert phone_lock_holder() is None
    acquire_phone_lock("the next run").release()


def test_appium_recovery_reconnects_the_phone_without_contending_for_its_own_lock(phone_bench, tmp_path):
    phone_bench.appium_reachable = [True, False]
    seen = holders_during(phone_bench, ROOT)

    outcome = run(phone_bench, tmp_path, PLAIN, ROOT)

    assert phone_bench.risks[ROOT].used == [PHONE]
    assert seen[0]["purpose"] == f"Android run {outcome.run_timestamp}"
    assert read_manifest(outcome.run_dir)["status"] == "completed"
    assert phone_lock_holder() is None


def test_a_static_only_run_never_takes_the_phone(phone_bench, tmp_path):
    other = acquire_phone_lock("Android install of one")
    try:
        outcome = run(phone_bench, tmp_path, STATIC)
    finally:
        other.release()

    assert phone_bench.risks[STATIC].used == [None]
    assert read_manifest(outcome.run_dir)["status"] == "completed"


def test_corellium_baseline_runs_never_take_the_phone_lock(tmp_path, monkeypatch):
    bench = BaselineBench(tmp_path, monkeypatch, risks=(PLAIN,))
    seen = holders_during(bench, PLAIN)
    other = acquire_phone_lock("Android install of one")
    try:
        run(bench, tmp_path, PLAIN, runner=bench.runner())
    finally:
        other.release()

    assert bench.risks[PLAIN].used == [STOCK_SERIAL]
    assert seen[0]["purpose"] == "Android install of one"
