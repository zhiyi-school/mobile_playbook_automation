from __future__ import annotations

import pytest

from mobile_playbook.api import config_editor as ce
from tests.conftest import make_ipa


@pytest.fixture
def config_root(tmp_path, monkeypatch):
    ipa = tmp_path / "intake/ios/ipas/App.ipa"
    make_ipa(ipa, bundle_id="com.example.app")

    (tmp_path / "configs/split/ios").mkdir(parents=True)
    (tmp_path / "configs/split/android").mkdir(parents=True)

    (tmp_path / "configs/split/ios/templates.yaml").write_text(
        "x-local-ipa-artifact: &local_ipa_artifact\n"
        "  source: \"local_ipa\"\n"
        "  expected_bundle_id: \"\"\n"
    )
    (tmp_path / "configs/split/ios/apps.yaml").write_text(
        "# The iOS app roster.\n"
        "apps:\n"
        "  - name: \"App One\"\n"
        "    bundle_id: \"com.example.app\"\n"
        "    test_bundle_id: \"com.example.app\"\n"
        "    artifact:\n"
        "      <<: *local_ipa_artifact\n"
        f"      ipa: \"{ipa.as_posix()}\"\n"
        "    risks:\n"
        "      ios-feature-01-risk-01:\n"
        "        enabled: true\n"
    )
    (tmp_path / "configs/split/ios/ipa_static_analysis.yaml").write_text(
        "# Global settings for ios-feature-01-risk-01.\n"
        "ipa_static_analysis:\n"
        "  analyzer:\n"
        "    provider: \"mobsf\"\n"
        "    # comment on a nested field\n"
        "    mobsf_url: \"http://127.0.0.1:8000\"\n"
        "  sensitive_scan:\n"
        "    reveal_values: true\n"
    )
    (tmp_path / "configs/split/ios/keystroke_collection.yaml").write_text("keystroke_collection: {}\n")
    (tmp_path / "configs/ios.yaml").write_text(
        "# Working local iOS config.\n"
        "device:\n"
        "  udid: \"UDID123\"\n"
        "  # which team signs WDA\n"
        "  team_id: \"TEAM\"\n"
        "  appium_server_url: \"http://127.0.0.1:4723\"\n"
        "\n"
        "runner:\n"
        "  work_dir: \"work/ios\"\n"
        "\n"
        "include:\n"
        "  ipa_static_analysis: split/ios/ipa_static_analysis.yaml\n"
        "  keystroke_collection: split/ios/keystroke_collection.yaml\n"
        "  apps:\n"
        "    - split/ios/templates.yaml\n"
        "    - split/ios/apps.yaml\n"
    )

    (tmp_path / "configs/split/android/apps.yaml").write_text(
        "apps:\n"
        "  - id: \"one\"\n"
        "    name: \"One\"\n"
        "    package_name: \"com.example.one\"\n"
        "    risks:\n"
        "      android-feature-06-risk-01:\n"
        "        enabled: true\n"
    )
    (tmp_path / "configs/split/android/repackaging.yaml").write_text("repackaging: {}\n")
    (tmp_path / "configs/split/android/screen_capture.yaml").write_text("screen_capture: {}\n")
    (tmp_path / "configs/android.yaml").write_text(
        "device:\n"
        "  appium_server_url: \"http://127.0.0.1:4723\"\n"
        "\n"
        "runner:\n"
        "  work_dir: \"work/android\"\n"
        "\n"
        "include:\n"
        "  repackaging: split/android/repackaging.yaml\n"
        "  screen_capture: split/android/screen_capture.yaml\n"
        "  apps: split/android/apps.yaml\n"
    )

    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_ios_add_edit_delete_app_preserves_untouched_entries(config_root):
    original_apps_text = (config_root / "configs/split/ios/apps.yaml").read_text()
    ipa = config_root / "intake/ios/ipas/App.ipa"

    added = ce.add_ios_app({"name": "App Two", "artifact": {"source": "local_ipa", "ipa": str(ipa)}})
    assert added["id"] == "app_two"
    assert len(ce.list_ios_apps()) == 2

    edited = ce.edit_ios_app("app_one", {"test_bundle_id": "com.example.app.updated"})
    assert edited["test_bundle_id"] == "com.example.app.updated"
    assert edited["artifact"]["expected_bundle_id"] == "com.example.app"  # template value survives the expand

    ce.delete_ios_app(added["id"])
    assert len(ce.list_ios_apps()) == 1
    assert (config_root / "configs/split/ios/apps.yaml").read_text() != original_apps_text  # app_one was edited


def test_ios_edit_reverts_file_on_invalid_config(config_root):
    original = (config_root / "configs/split/ios/apps.yaml").read_text()
    with pytest.raises(Exception):
        ce.edit_ios_app("app_one", {"artifact": {"ipa": "does/not/exist.ipa"}})
    assert (config_root / "configs/split/ios/apps.yaml").read_text() == original


def test_ios_add_app_duplicate_id_rejected(config_root):
    with pytest.raises(Exception):
        ce.add_ios_app({"name": "App One"})


def test_ios_delete_unknown_app_raises(config_root):
    with pytest.raises(Exception):
        ce.delete_ios_app("does-not-exist")


def test_put_section_preserves_comments_and_untouched_keys(config_root):
    ce.put_section("ios", "device", {"platform_version": "18.0"})
    text = (config_root / "configs/ios.yaml").read_text()
    assert "# which team signs WDA" in text
    assert 'team_id: "TEAM"' in text
    assert "platform_version" in text
    updated = ce.get_section("ios", "device")
    assert updated["udid"] == "UDID123"
    assert updated["platform_version"] == "18.0"


def test_put_risk_settings_preserves_comments_and_untouched_keys(config_root):
    ce.put_risk_settings("ios", "ios-feature-01-risk-01", {"sensitive_scan": {"reveal_values": False}})
    text = (config_root / "configs/split/ios/ipa_static_analysis.yaml").read_text()
    assert "# comment on a nested field" in text
    assert 'provider: "mobsf"' in text
    settings = ce.get_risk_settings("ios", "ios-feature-01-risk-01")
    assert settings["sensitive_scan"]["reveal_values"] is False
    assert settings["analyzer"]["provider"] == "mobsf"


def test_unknown_risk_settings_id_raises(config_root):
    with pytest.raises(Exception):
        ce.get_risk_settings("ios", "ios-does-not-exist")


def test_android_add_edit_delete_app(config_root):
    added = ce.add_android_app({"name": "Two", "package_name": "com.example.two"})
    assert added["id"] == "com_example_two"
    assert len(ce.list_android_apps()) == 2

    edited = ce.edit_android_app(added["id"], {"risks": {"android-feature-01-risk-02": {"enabled": True}}})
    assert edited["risks"]["android-feature-01-risk-02"]["enabled"] is True
    assert edited["package_name"] == "com.example.two"

    ce.delete_android_app(added["id"])
    assert len(ce.list_android_apps()) == 1


def test_android_edit_preserves_other_untouched_entries(config_root):
    text_before = (config_root / "configs/split/android/apps.yaml").read_text()
    ce.edit_android_app("one", {"risks": {"android-feature-01-risk-02": {"enabled": True}}})
    text_after = (config_root / "configs/split/android/apps.yaml").read_text()
    assert 'name: "One"' in text_after
    assert text_before != text_after
