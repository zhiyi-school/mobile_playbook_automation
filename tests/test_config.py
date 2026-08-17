from __future__ import annotations

import yaml
import pytest

from mobile_playbook.platforms.ios.config import ConfigError, load_config, validate_config


def test_config_parsing_and_validation(tmp_path, fake_ipa):
    cfg = {
        "device": {"udid": "u", "team_id": "t", "appium_server_url": "http://127.0.0.1:4723"},
        "apps": [{
            "id": "a",
            "name": "A",
            "bundle_id": "com.example.app",
            "artifact": {"source": "local_ipa", "ipa": str(fake_ipa)},
            "expected_behavior": {},
            "risks": {"ios-feature1-risk1": {"enabled": True}},
        }],
    }
    path = tmp_path / "apps.yaml"
    path.write_text(yaml.safe_dump(cfg))
    loaded = load_config(path)
    assert loaded.apps[0].test_bundle_id == "com.example.app"


def test_config_auto_fills_bundle_id_from_local_ipa(tmp_path, fake_ipa):
    cfg = {
        "device": {"udid": "u", "team_id": "t", "appium_server_url": "http://127.0.0.1:4723"},
        "apps": [{
            "id": "a",
            "name": "A",
            "bundle_id": "",
            "artifact": {"source": "local_ipa", "ipa": str(fake_ipa)},
            "expected_behavior": {},
            "risks": {"ios-feature5-risk1": {"enabled": False}},
        }],
    }
    path = tmp_path / "apps.yaml"
    path.write_text(yaml.safe_dump(cfg))
    loaded = load_config(path)
    assert loaded.apps[0].bundle_id == "com.example.app"
    assert loaded.apps[0].test_bundle_id == "com.example.app"
    assert loaded.apps[0].artifact["expected_bundle_id"] == "com.example.app"


def test_config_parses_current_risks_without_signing_section(tmp_path, fake_ipa):
    cfg = {
        "device": {"udid": "u", "team_id": "t", "appium_server_url": "http://127.0.0.1:4723"},
        "apps": [{
            "id": "a",
            "name": "A",
            "bundle_id": "com.example.app",
            "artifact": {"source": "local_ipa", "ipa": str(fake_ipa)},
            "expected_behavior": {},
            "risks": {"ios-feature1-risk1": {"enabled": True}},
        }],
    }
    path = tmp_path / "apps.yaml"
    path.write_text(yaml.safe_dump(cfg))

    loaded = load_config(path)

    assert loaded.apps[0].id == "a"


def test_config_loads_split_section_includes(tmp_path, fake_ipa):
    (tmp_path / "device.yaml").write_text(yaml.safe_dump({
        "device": {"udid": "u", "team_id": "t", "appium_server_url": "http://127.0.0.1:4723"}
    }))
    (tmp_path / "runner.yaml").write_text(yaml.safe_dump({
        "runner": {"launch_wait_seconds": 9, "permission_alerts": {"enabled": False}}
    }))
    (tmp_path / "apps.yaml").write_text(yaml.safe_dump({
        "apps": [{
            "id": "a",
            "name": "A",
            "bundle_id": "com.example.app",
            "artifact": {"source": "local_ipa", "ipa": str(fake_ipa)},
            "expected_behavior": {},
            "risks": {"ios-feature1-risk1": {"enabled": True}},
        }]
    }))
    entry = {
        "include": {
            "device": "device.yaml",
            "runner": "runner.yaml",
            "apps": "apps.yaml",
        },
        "runner": {"stop_on_first_failure": True},
    }
    path = tmp_path / "ios.yaml"
    path.write_text(yaml.safe_dump(entry))

    loaded = load_config(path)

    assert loaded.device.udid == "u"
    assert loaded.runner.launch_wait_seconds == 9
    assert loaded.runner.stop_on_first_failure is True
    assert loaded.runner.permission_alerts == {"enabled": False}
    assert loaded.apps[0].id == "a"


def test_config_loads_split_section_include_list_with_shared_anchors(tmp_path, fake_ipa):
    (tmp_path / "device.yaml").write_text(yaml.safe_dump({
        "device": {"udid": "u", "team_id": "t", "appium_server_url": "http://127.0.0.1:4723"}
    }))
    (tmp_path / "templates.yaml").write_text(
        "x-local-ipa-artifact: &local_ipa_artifact\n"
        "  source: \"local_ipa\"\n"
        "  expected_bundle_id: \"\"\n"
    )
    (tmp_path / "apps.yaml").write_text(
        "apps:\n"
        "  - id: \"a\"\n"
        "    name: \"A\"\n"
        "    bundle_id: \"com.example.app\"\n"
        f"    artifact:\n"
        f"      <<: *local_ipa_artifact\n"
        f"      ipa: \"{fake_ipa}\"\n"
        "    expected_behavior: {}\n"
        "    risks:\n"
        "      ios-feature1-risk1:\n"
        "        enabled: true\n"
    )
    entry = {
        "include": {
            "device": "device.yaml",
            "apps": ["templates.yaml", "apps.yaml"],
        },
    }
    path = tmp_path / "ios.yaml"
    path.write_text(yaml.safe_dump(entry))

    loaded = load_config(path)

    assert loaded.apps[0].id == "a"
    # "source" came only from the templates.yaml anchor merged via `<<: *local_ipa_artifact`,
    # proving the cross-file alias in apps.yaml resolved against the earlier file's anchor.
    assert loaded.apps[0].artifact["source"] == "local_ipa"
    assert loaded.apps[0].artifact["ipa"] == str(fake_ipa)


def test_config_include_list_rejects_empty_list(tmp_path):
    entry = {
        "device": {"udid": "u", "team_id": "t", "appium_server_url": "http://127.0.0.1:4723"},
        "include": {"apps": []},
    }
    path = tmp_path / "ios.yaml"
    path.write_text(yaml.safe_dump(entry))

    with pytest.raises(ConfigError, match="apps"):
        load_config(path)


def test_config_allows_feature1_risk1_without_extra_cases(global_config):
    app = global_config.apps[0]
    app.risks = {"ios-feature1-risk1": {"enabled": True}}
    validate_config(global_config, dry_run=True)


def test_config_allows_feature5_risk1_without_extra_cases(global_config):
    app = global_config.apps[0]
    app.risks = {
        "ios-feature5-risk1": {
            "enabled": True,
            "keyboard_app": {"bundle_id": "com.example.keyboard"},
            "collection": {"port": 0, "probe_text": "hello123"},
        }
    }
    validate_config(global_config, dry_run=True)


def test_config_auto_fills_feature5_risk1_keyboard_bundle_id(global_config, fake_ipa):
    app = global_config.apps[0]
    app.risks = {
        "ios-feature5-risk1": {
            "enabled": True,
            "keyboard_app": {"ipa": str(fake_ipa)},
            "collection": {"port": 0, "probe_text": "hello123"},
        }
    }
    validate_config(global_config, dry_run=True)
    assert app.risks["ios-feature5-risk1"]["keyboard_app"]["bundle_id"] == "com.example.app"


