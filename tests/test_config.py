from __future__ import annotations

import yaml
import pytest

from mobile_playbook.platforms.ios.config import ConfigError, effective_risk_config, load_config, validate_config
from tests.conftest import make_ipa


def test_config_parsing_and_validation(tmp_path, fake_ipa):
    cfg = {
        "device": {"udid": "u", "team_id": "t", "appium_server_url": "http://127.0.0.1:4723"},
        "apps": [{
            "id": "a",
            "name": "A",
            "bundle_id": "com.example.app",
            "artifact": {"source": "local_ipa", "ipa": str(fake_ipa)},
            "expected_behavior": {},
            "risks": {"ios-feature-01-risk-01": {"enabled": True}},
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
            "risks": {"ios-feature-04-risk-01": {"enabled": False}},
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
            "risks": {"ios-feature-01-risk-01": {"enabled": True}},
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
            "risks": {"ios-feature-01-risk-01": {"enabled": True}},
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
        "      ios-feature-01-risk-01:\n"
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


def test_config_include_sections_can_share_one_file(tmp_path, fake_ipa):
    (tmp_path / "device.yaml").write_text(yaml.safe_dump({
        "device": {"udid": "u", "team_id": "t", "appium_server_url": "http://127.0.0.1:4723"}
    }))
    (tmp_path / "risk_settings.yaml").write_text(yaml.safe_dump({
        "ipa_static_analysis": {"analyzer": {"provider": "mobsf"}},
        "keystroke_collection": {"collection": {"probe_text": "hello123"}},
    }))
    (tmp_path / "apps.yaml").write_text(yaml.safe_dump({
        "apps": [{
            "id": "a",
            "name": "A",
            "bundle_id": "com.example.app",
            "artifact": {"source": "local_ipa", "ipa": str(fake_ipa)},
            "expected_behavior": {},
            "risks": {"ios-feature-01-risk-01": {"enabled": True}},
        }]
    }))
    entry = {
        "include": {
            "device": "device.yaml",
            "ipa_static_analysis": "risk_settings.yaml",
            "keystroke_collection": "risk_settings.yaml",
            "apps": "apps.yaml",
        },
    }
    path = tmp_path / "ios.yaml"
    path.write_text(yaml.safe_dump(entry))

    loaded = load_config(path)

    # Two different include sections can point at the same combined file; each
    # pulls out only the top-level key matching its own section name.
    assert loaded.ipa_static_analysis == {"analyzer": {"provider": "mobsf"}}
    assert loaded.keystroke_collection == {"collection": {"probe_text": "hello123"}}


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
    app.risks = {"ios-feature-01-risk-01": {"enabled": True}}
    validate_config(global_config, dry_run=True)


def test_config_allows_feature_04_risk_01_without_extra_cases(global_config):
    app = global_config.apps[0]
    app.risks = {
        "ios-feature-04-risk-01": {
            "enabled": True,
            "keyboard_app": {"bundle_id": "com.example.keyboard"},
            "collection": {"port": 0, "probe_text": "hello123"},
        }
    }
    validate_config(global_config, dry_run=True)


def test_config_auto_fills_feature_04_risk_01_keyboard_bundle_id(global_config, fake_ipa):
    app = global_config.apps[0]
    app.risks = {
        "ios-feature-04-risk-01": {
            "enabled": True,
            "keyboard_app": {"ipa": str(fake_ipa)},
            "collection": {"port": 0, "probe_text": "hello123"},
        }
    }
    validate_config(global_config, dry_run=True)
    assert app.risks["ios-feature-04-risk-01"]["keyboard_app"]["bundle_id"] == "com.example.app"


def test_effective_risk_config_merges_app_override_onto_global_defaults(tmp_path, fake_ipa):
    cfg = {
        "device": {"udid": "u", "team_id": "t", "appium_server_url": "http://127.0.0.1:4723"},
        "ipa_static_analysis": {"analyzer": {"provider": "mobsf", "mobsf_url": "http://global:8000"}},
        "apps": [{
            "id": "a",
            "name": "A",
            "bundle_id": "com.example.app",
            "artifact": {"source": "local_ipa", "ipa": str(fake_ipa)},
            "expected_behavior": {},
            "risks": {"ios-feature-01-risk-01": {"enabled": True, "analyzer": {"provider": "package_scanner"}}},
        }],
    }
    path = tmp_path / "ios.yaml"
    path.write_text(yaml.safe_dump(cfg))

    loaded = load_config(path)

    effective = effective_risk_config(loaded, "ios-feature-01-risk-01", loaded.apps[0].risks["ios-feature-01-risk-01"])
    assert effective["analyzer"]["provider"] == "package_scanner"
    assert effective["analyzer"]["mobsf_url"] == "http://global:8000"


def test_effective_risk_config_deep_merges_nested_override_without_losing_siblings(tmp_path, fake_ipa):
    cfg = {
        "device": {"udid": "u", "team_id": "t", "appium_server_url": "http://127.0.0.1:4723"},
        "keystroke_collection": {
            "keyboard_app": {"bundle_id": "com.example.keyboard"},
            "collection": {
                "probe_text": "hello123",
                "auto_navigation": {"accessibility_ids": [], "max_steps": 4},
            },
        },
        "apps": [{
            "id": "a",
            "name": "A",
            "bundle_id": "com.example.app",
            "artifact": {"source": "local_ipa", "ipa": str(fake_ipa)},
            "expected_behavior": {},
            "risks": {
                "ios-feature-04-risk-01": {
                    "enabled": True,
                    "collection": {"auto_navigation": {"accessibility_ids": ["btn_select_carpark"]}},
                }
            },
        }],
    }
    path = tmp_path / "ios.yaml"
    path.write_text(yaml.safe_dump(cfg))

    loaded = load_config(path)

    effective = effective_risk_config(loaded, "ios-feature-04-risk-01", loaded.apps[0].risks["ios-feature-04-risk-01"])
    # per-app override applies at the exact nested field it targets...
    assert effective["collection"]["auto_navigation"]["accessibility_ids"] == ["btn_select_carpark"]
    # ...while sibling fields (not mentioned in the override) still fall back to the global default
    assert effective["collection"]["auto_navigation"]["max_steps"] == 4
    assert effective["collection"]["probe_text"] == "hello123"
    assert effective["keyboard_app"]["bundle_id"] == "com.example.keyboard"


def test_config_auto_fills_keyboard_bundle_id_from_global_keystroke_collection_section(tmp_path, fake_ipa):
    keyboard_ipa = make_ipa(tmp_path / "keyboard.ipa", bundle_id="com.example.keyboard.inferred")
    cfg = {
        "device": {"udid": "u", "team_id": "t", "appium_server_url": "http://127.0.0.1:4723"},
        "keystroke_collection": {
            "keyboard_app": {"ipa": str(keyboard_ipa)},
            "collection": {"probe_text": "hello123"},
        },
        "apps": [{
            "id": "a",
            "name": "A",
            "bundle_id": "com.example.app",
            "artifact": {"source": "local_ipa", "ipa": str(fake_ipa)},
            "expected_behavior": {},
            "risks": {"ios-feature-04-risk-01": {"enabled": True}},
        }],
    }
    path = tmp_path / "ios.yaml"
    path.write_text(yaml.safe_dump(cfg))

    loaded = load_config(path)

    # The keyboard host app is shared by every app under test, so its bundle ID is
    # inferred once onto the global section rather than repeated per app.
    assert loaded.keystroke_collection["keyboard_app"]["bundle_id"] == "com.example.keyboard.inferred"




def _screen_capture_config(tmp_path, fake_ipa, screen_capture: dict, app_risk: dict | None = None) -> dict:
    return {
        "device": {"udid": "u", "team_id": "t", "appium_server_url": "http://127.0.0.1:4723"},
        "screen_capture": screen_capture,
        "apps": [{
            "id": "a",
            "name": "A",
            "bundle_id": "com.example.app",
            "artifact": {"source": "local_ipa", "ipa": str(fake_ipa)},
            "expected_behavior": {},
            "risks": {"ios-feature-03-risk-01": {"enabled": True, **(app_risk or {})}},
        }],
    }


def _load(tmp_path, cfg: dict, dry_run: bool = False):
    path = tmp_path / "ios.yaml"
    path.write_text(yaml.safe_dump(cfg))
    return load_config(path, dry_run=dry_run)


def test_screen_capture_requires_a_recorder_bundle_id_or_ipa(tmp_path, fake_ipa):
    with pytest.raises(ConfigError) as error:
        _load(tmp_path, _screen_capture_config(tmp_path, fake_ipa, {"recorder_app": {}}))

    assert "apps[a].risks.ios-feature-03-risk-01.recorder_app.bundle_id or ipa is required" in error.value.errors


def test_screen_capture_recorder_ipa_must_exist_unless_dry_run(tmp_path, fake_ipa):
    cfg = _screen_capture_config(tmp_path, fake_ipa, {"recorder_app": {"ipa": str(tmp_path / "missing.ipa")}})

    with pytest.raises(ConfigError) as error:
        _load(tmp_path, cfg)
    assert any("recorder_app.ipa does not exist" in message for message in error.value.errors)
    _load(tmp_path, cfg, dry_run=True)


@pytest.mark.parametrize("capture, expected", [
    ({"capture_window_seconds": 0}, "capture.capture_window_seconds must be greater than 0"),
    ({"capture_window_seconds": "soon"}, "capture.capture_window_seconds must be greater than 0"),
    ({"ocr_provider": "tesseract"}, "capture.ocr_provider must be vision or none"),
])
def test_screen_capture_validates_the_capture_block(tmp_path, fake_ipa, capture, expected):
    cfg = _screen_capture_config(tmp_path, fake_ipa, {"recorder_app": {"bundle_id": "com.example.recorder"}, "capture": capture})

    with pytest.raises(ConfigError) as error:
        _load(tmp_path, cfg)

    assert f"apps[a].risks.ios-feature-03-risk-01.{expected}" in error.value.errors


def test_screen_capture_app_override_merges_onto_global_defaults(tmp_path, fake_ipa):
    cfg = _screen_capture_config(
        tmp_path,
        fake_ipa,
        {"recorder_app": {"bundle_id": "com.example.recorder"}, "capture": {"capture_window_seconds": 12, "ocr_provider": "vision"}},
        {"capture": {"capture_window_seconds": 20}},
    )

    loaded = _load(tmp_path, cfg)

    effective = effective_risk_config(loaded, "ios-feature-03-risk-01", loaded.apps[0].risks["ios-feature-03-risk-01"])
    assert effective["capture"] == {"capture_window_seconds": 20, "ocr_provider": "vision"}
    assert effective["recorder_app"]["bundle_id"] == "com.example.recorder"


def test_screen_capture_fills_the_recorder_bundle_id_from_its_ipa(tmp_path, fake_ipa):
    recorder_ipa = make_ipa(tmp_path / "recorder.ipa", bundle_id="com.example.recorder.inferred")

    loaded = _load(tmp_path, _screen_capture_config(tmp_path, fake_ipa, {"recorder_app": {"ipa": str(recorder_ipa)}}))

    assert loaded.screen_capture["recorder_app"]["bundle_id"] == "com.example.recorder.inferred"


def test_screen_capture_fills_a_per_app_recorder_bundle_id_from_its_ipa(tmp_path, fake_ipa):
    recorder_ipa = make_ipa(tmp_path / "recorder.ipa", bundle_id="com.example.recorder.app")

    loaded = _load(tmp_path, _screen_capture_config(tmp_path, fake_ipa, {}, {"recorder_app": {"ipa": str(recorder_ipa)}}))

    assert loaded.apps[0].risks["ios-feature-03-risk-01"]["recorder_app"]["bundle_id"] == "com.example.recorder.app"
