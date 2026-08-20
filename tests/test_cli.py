from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone

from mobile_playbook import cli as cli_module
from mobile_playbook.cli import _load_env_file, _new_run_timestamp, _run, _run_all
from mobile_playbook.platforms.android.config import parse_config as parse_android_config
from mobile_playbook.platforms.ios.risks import get_risk, known_risks


def _no_risk_android_config():
    # An empty `risks: {}` mapping is falsy, so config.py's `item.get("risks") or {...}`
    # would silently fall back to enabling every known risk. Disable them explicitly
    # instead, so this config genuinely needs no device connection.
    from mobile_playbook.platforms.android.risks import known_risks

    return parse_android_config(
        {
            "device": {"appium_server_url": "http://127.0.0.1:4723"},
            "apps": [
                {
                    "id": "android_app",
                    "name": "Android App",
                    "package_name": "com.example.android",
                    "risks": {risk_id: {"enabled": False} for risk_id in known_risks()},
                }
            ],
        }
    )


def test_run_feature1_risk1_does_not_connect_appium(monkeypatch, global_config, tmp_path):
    app = global_config.apps[0]
    app.risks = {"ios-feature-01-risk-01": {"enabled": True}}

    def fail_connect(*args, **kwargs):
        raise AssertionError("Appium should not be used for ios-feature-01-risk-01")

    monkeypatch.setattr("mobile_playbook.platforms.ios.runner.IosPlatformRunner.connect_device", fail_connect)

    assert _run(global_config, {"ios-feature-01-risk-01"}, None, tmp_path / "reports") == 0


def test_run_isolates_one_apps_risk_failure_from_the_rest(monkeypatch, global_config, tmp_path):
    # Resolved dynamically via get_risk(), the same way IosPlatformRunner.run_test does —
    # not a direct `from ...feature1_risk1 import Feature1Risk1`, since the whole point of
    # the registry auto-discovery is that nothing needs to hardcode which module a risk
    # lives in.
    risk_class = type(get_risk("ios-feature-01-risk-01"))
    original_run = risk_class.run

    def flaky_run(self, app_config, global_cfg, device_client, report_writer):
        if app_config.id == "app_one":
            raise RuntimeError("simulated failure for app_one")
        return original_run(self, app_config, global_cfg, device_client, report_writer)

    monkeypatch.setattr(risk_class, "run", flaky_run)

    def fail_connect(*args, **kwargs):
        raise AssertionError("ios-feature-01-risk-01 does not require a device")

    monkeypatch.setattr("mobile_playbook.platforms.ios.runner.IosPlatformRunner.connect_device", fail_connect)

    first = global_config.apps[0]
    first.risks = {"ios-feature-01-risk-01": {"enabled": True}}
    second = replace(first, id="app_two", name="App Two")
    global_config.apps = [first, second]

    assert _run(global_config, {"ios-feature-01-risk-01"}, None, tmp_path / "reports") == 0

    run_dir = next((tmp_path / "reports").iterdir())
    failed_report = json.loads(
        (run_dir / "ios" / "app_one" / "ios-feature-01-risk-01" / "risk_execution_failed" / "report.json").read_text()
    )
    assert failed_report["final_status"] == "FAILED"
    assert "simulated failure for app_one" in failed_report["errors"][0]

    # app_two's own test still ran to completion despite app_one's failure aborting nothing
    assert (run_dir / "ios" / "app_two" / "ios-feature-01-risk-01" / "ipa_static_analysis" / "report.json").exists()


def test_run_filters_by_selected_app(monkeypatch, global_config, tmp_path):
    first = global_config.apps[0]
    first.risks = {"ios-feature-01-risk-01": {"enabled": True}}
    second = replace(first, id="app_two", name="App Two")
    global_config.apps = [first, second]

    def fail_connect(*args, **kwargs):
        raise AssertionError("Appium should not be used for ios-feature-01-risk-01")

    monkeypatch.setattr("mobile_playbook.platforms.ios.runner.IosPlatformRunner.connect_device", fail_connect)

    assert _run(global_config, {"ios-feature-01-risk-01"}, {"apptwo"}, tmp_path / "reports") == 0
    run_dirs = list((tmp_path / "reports").iterdir())
    assert len(run_dirs) == 1
    assert run_dirs[0].name.count("_") == 1
    assert run_dirs[0].name[:10].count("-") == 2
    assert (run_dirs[0] / "ios" / "app_two").exists()
    assert not (run_dirs[0] / "ios" / "app_one").exists()


def test_run_all_runs_both_platforms_into_separate_report_folders(monkeypatch, global_config, tmp_path):
    android_config = _no_risk_android_config()

    def fail_connect(*args, **kwargs):
        raise AssertionError("no enabled risks require a device; connect_device should not be called")

    monkeypatch.setattr("mobile_playbook.platforms.ios.runner.IosPlatformRunner.connect_device", fail_connect)
    monkeypatch.setattr("mobile_playbook.platforms.android.runner.AndroidPlatformRunner.connect_device", fail_connect)

    reports_dir = tmp_path / "reports"
    exit_code = _run_all(global_config, android_config, None, None, reports_dir)

    assert exit_code == 0
    run_dirs = list(reports_dir.iterdir())
    assert any((run_dir / "ios" / "app_one").exists() for run_dir in run_dirs)
    assert any((run_dir / "android").exists() for run_dir in run_dirs)


def test_run_all_isolates_one_platform_failure_from_the_other(monkeypatch, global_config, tmp_path):
    android_config = _no_risk_android_config()

    def boom(*args, **kwargs):
        raise RuntimeError("android side blew up")

    monkeypatch.setattr(cli_module, "_run_android", boom)

    reports_dir = tmp_path / "reports"
    exit_code = _run_all(global_config, android_config, None, None, reports_dir)

    assert exit_code == 1
    run_dirs = list(reports_dir.iterdir())
    assert any((run_dir / "ios" / "app_one").exists() for run_dir in run_dirs)


def test_ios_registry_only_exposes_current_risks():
    assert known_risks() == {"ios-feature-01-risk-01", "ios-feature-04-risk-01"}


def test_new_run_timestamp_is_sortable_and_collision_safe(tmp_path):
    now = datetime(2026, 8, 14, 13, 5, 6, tzinfo=timezone.utc)

    assert _new_run_timestamp(tmp_path, now) == "2026-08-14_13-05-06"
    (tmp_path / "2026-08-14_13-05-06").mkdir()

    assert _new_run_timestamp(tmp_path, now) == "2026-08-14_13-05-06-2"


def test_load_env_file_sets_missing_values_without_overriding_existing(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "# local secrets",
                "MOBSF_API_KEY=from-file",
                "QUOTED_VALUE=\"hello world\"",
                "EXISTING_VALUE=from-file",
            ]
        )
    )
    monkeypatch.delenv("MOBSF_API_KEY", raising=False)
    monkeypatch.delenv("QUOTED_VALUE", raising=False)
    monkeypatch.setenv("EXISTING_VALUE", "from-shell")

    _load_env_file(env_file)

    import os

    assert os.environ["MOBSF_API_KEY"] == "from-file"
    assert os.environ["QUOTED_VALUE"] == "hello world"
    assert os.environ["EXISTING_VALUE"] == "from-shell"
