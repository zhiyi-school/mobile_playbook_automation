from __future__ import annotations

from mobile_playbook.platforms.android.config import parse_config
from mobile_playbook.platforms.android.risks import known_risks
from mobile_playbook.platforms.android.runner import AndroidPlatformRunner


def test_android_config_accepts_legacy_package_list():
    config = parse_config(
        {
            "device": {"appium_server": "http://127.0.0.1:4723"},
            "apps": ["sg.parking.streetsmart"],
        }
    )

    assert config.device.appium_server_url == "http://127.0.0.1:4723"
    assert config.apps[0].package_name == "sg.parking.streetsmart"
    assert config.apps[0].risks["android-feature6-risk1"]["enabled"] is True
    assert config.apps[0].risks["android-feature1-risk2"]["enabled"] is True


def test_android_config_accepts_structured_apps():
    config = parse_config(
        {
            "apps": [
                {
                    "id": "parking",
                    "name": "Parking",
                    "package_name": "sg.parking.streetsmart",
                    "risks": {"android-feature6-risk1": {"enabled": True}},
                }
            ]
        }
    )

    assert config.apps[0].id == "parking"
    assert config.apps[0].name == "Parking"
    assert config.apps[0].risks == {"android-feature6-risk1": {"enabled": True}}


def test_android_registry_exposes_ported_risks():
    assert known_risks() == {"android-feature1-risk2", "android-feature6-risk1"}


def test_android_dry_run_filters_selected_apps():
    config = parse_config({"apps": ["sg.parking.streetsmart", "sg.gov.app.mol"]})

    lines = AndroidPlatformRunner().dry_run_lines(config, {"android-feature6-risk1"}, {"sggovappmol"})

    assert any("sg.gov.app.mol" in line for line in lines)
    assert not any("sg.parking.streetsmart" in line for line in lines)
