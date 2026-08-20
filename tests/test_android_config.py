from __future__ import annotations

import json
from types import SimpleNamespace

from mobile_playbook.platforms.android.config import parse_config
from mobile_playbook.platforms.android.results import normalize_android_result
from mobile_playbook.platforms.android.risks import get_risk, known_risks
from mobile_playbook.platforms.android.runner import AndroidPlatformRunner
from mobile_playbook.reporting.report_writer import ReportWriter


def test_android_config_accepts_legacy_package_list():
    config = parse_config(
        {
            "device": {"appium_server": "http://127.0.0.1:4723"},
            "apps": ["sg.parking.streetsmart"],
        }
    )

    assert config.device.appium_server_url == "http://127.0.0.1:4723"
    assert config.apps[0].package_name == "sg.parking.streetsmart"
    assert config.apps[0].risks["android-feature-06-risk-01"]["enabled"] is True
    assert config.apps[0].risks["android-feature-01-risk-02"]["enabled"] is True


def test_android_config_respects_an_explicit_repackaging_work_dir():
    config = parse_config({"repackaging": {"work_dir": "custom/repackaging/dir"}})

    assert config.repackaging["work_dir"] == "custom/repackaging/dir"


def test_android_config_falls_back_to_default_repackaging_work_dir_when_unset():
    config = parse_config({})

    assert config.repackaging["work_dir"] == "work/android/repackaging"


def test_android_config_accepts_structured_apps():
    config = parse_config(
        {
            "apps": [
                {
                    "id": "parking",
                    "name": "Parking",
                    "package_name": "sg.parking.streetsmart",
                    "risks": {"android-feature-06-risk-01": {"enabled": True}},
                }
            ]
        }
    )

    assert config.apps[0].id == "parking"
    assert config.apps[0].name == "Parking"
    assert config.apps[0].risks == {"android-feature-06-risk-01": {"enabled": True}}


def test_android_registry_exposes_ported_risks():
    assert known_risks() == {"android-feature-01-risk-02", "android-feature-06-risk-01"}


def test_android_dry_run_filters_selected_apps():
    config = parse_config({"apps": ["sg.parking.streetsmart", "sg.gov.app.mol"]})

    lines = AndroidPlatformRunner().dry_run_lines(config, {"android-feature-06-risk-01"}, {"sggovappmol"})

    assert any("sg.gov.app.mol" in line for line in lines)
    assert not any("sg.parking.streetsmart" in line for line in lines)


def test_run_test_records_failure_without_raising(monkeypatch, tmp_path):
    # Resolved dynamically via get_risk(), the same way AndroidPlatformRunner.run_test
    # does — not a direct import of a specific risk module.
    risk_class = type(get_risk("android-feature-06-risk-01"))

    def flaky_run(self, app_config, global_config, device_client, report_writer):
        raise RuntimeError("adb exploded")

    monkeypatch.setattr(risk_class, "requires", [])
    monkeypatch.setattr(risk_class, "run", flaky_run)

    app = SimpleNamespace(id="parking", name="Parking", package_name="sg.parking.streetsmart")
    config = SimpleNamespace(runner=SimpleNamespace(auto_grant_permissions=False))
    device_client = SimpleNamespace(adb=None)
    writer = ReportWriter(tmp_path, "run1", result_adapter=normalize_android_result, platform="android")

    # must not raise — a single flaky risk cannot be allowed to abort the whole run
    AndroidPlatformRunner().run_test(app, "android-feature-06-risk-01", config, device_client, writer)

    report_dir = tmp_path / "run1" / "android" / "parking" / "android-feature-06-risk-01" / "screen_capture"
    result = json.loads((report_dir / "report.json").read_text())
    assert result["final_status"] == "FAILED"
    assert "adb exploded" in result["errors"][0]
