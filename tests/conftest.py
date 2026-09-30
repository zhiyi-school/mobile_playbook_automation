from __future__ import annotations

import os
import plistlib
import zipfile
from pathlib import Path

import pytest

from mobile_playbook.common import storage_paths
from mobile_playbook.platforms.ios.models import (
    AppConfig,
    DeviceConfig,
    ExpectedBehaviorConfig,
    GlobalConfig,
    InstallResult,
    RunnerConfig,
)


def make_ipa(path: Path, bundle_id: str = "com.example.app", executable: bytes = b"HELLO TEST_PATTERN_000 WORLD", extra_files: dict[str, bytes] | None = None, display_name: str = "Example") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    info = {
        "CFBundleIdentifier": bundle_id,
        "CFBundleExecutable": "AppExec",
        "CFBundleDisplayName": display_name,
    }
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("Payload/Example.app/Info.plist", plistlib.dumps(info))
        zf.writestr("Payload/Example.app/AppExec", executable)
        for name, data in (extra_files or {}).items():
            zf.writestr(f"Payload/Example.app/{name}", data)
    return path


def touch_future(path: Path) -> None:
    future = os.path.getmtime(path) + 10
    os.utime(path, (future, future))


# Points config resolution at the test's temp directory so no test reads or writes the real configs/.
@pytest.fixture(autouse=True)
def isolated_config_root(tmp_path, monkeypatch):
    monkeypatch.setattr(storage_paths, "CONFIG_ROOT", tmp_path / "configs")


# Builds a synthetic iOS and Android config tree for the config editor tests.
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
        "\n"
        "x-default-launch-check: &default_launch_check\n"
        "  app_state_must_be_foreground: true\n"
        "  source_contains: []\n"
        "  source_not_contains:\n"
        "    - \"Tamper detected\"\n"
        "    - \"Integrity check failed\"\n"
        "  app_specific_check: null\n"
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
        "  updated_wda_bundle_id: \"UDID_WDA\"\n"
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

    monkeypatch.setenv("INTAKE_DIR", str(tmp_path / "intake"))
    return tmp_path


@pytest.fixture
def fake_ipa(tmp_path):
    return make_ipa(tmp_path / "app.ipa")


@pytest.fixture
def global_config(tmp_path, fake_ipa):
    app = AppConfig(
        id="app_one",
        name="App One",
        bundle_id="com.example.app",
        test_bundle_id="com.example.app.test",
        artifact={"source": "local_ipa", "ipa": str(fake_ipa), "expected_bundle_id": "com.example.app"},
        expected_behavior=ExpectedBehaviorConfig(source_contains=["Login"], source_not_contains=["Tamper detected"]),
        risks={"ios-feature-01-risk-01": {"enabled": True}},
    )
    return GlobalConfig(
        device=DeviceConfig("udid", "TEAM", "http://127.0.0.1:4723"),
        runner=RunnerConfig(launch_wait_seconds=0, work_dir=tmp_path / "work"),
        apps=[app],
        config_path=tmp_path / "apps.yaml",
    )


class MockDevice:
    def __init__(self):
        self.installed: set[str] = set()
        self.removed: list[str] = []
        self.install_status = "INSTALLED"
        self.source = "<App><Text>Login</Text></App>"
        self.text_entries: list[dict[str, object]] = []
        self.taps: list[str] = []
        self.button_taps: list[dict[str, object]] = []
        self.permission_alert_results: list[dict[str, object]] = [{"status": "NO_ALERT"}]
        self.permission_alert_calls: list[dict[str, object]] = []
        self.keyboard_selection_results: dict[str, object] = {"status": "SELECTED"}
        self.keyboard_selection_calls: list[dict[str, object]] = []
        self.typed_text: list[dict[str, object]] = []
        self.opened_urls: list[dict[str, object]] = []

    def is_installed(self, bundle_id: str) -> bool:
        return bundle_id in self.installed

    def remove_app(self, bundle_id: str) -> bool:
        self.removed.append(bundle_id)
        self.installed.discard(bundle_id)
        return True

    def install_app(self, ipa_path: Path, timeout_ms: int) -> InstallResult:
        if self.install_status == "INSTALLED":
            self.installed.add("com.example.app.test")
            return InstallResult(status="INSTALLED", ipa_path=ipa_path)
        return InstallResult(status="INSTALL_FAILED", ipa_path=ipa_path, errors=["install failed"])

    def launch_app(self, bundle_id: str) -> dict:
        return {"ok": True}

    def open_url(self, url: str, bundle_id: str = "com.apple.mobilesafari") -> dict:
        self.opened_urls.append({"url": url, "bundle_id": bundle_id})
        return {"ok": True}

    def handle_permission_alerts(self, config: dict | None = None) -> list[dict]:
        self.permission_alert_calls.append(config or {})
        return self.permission_alert_results

    def ensure_keyboard_selected(self, config: dict | None = None) -> dict:
        self.keyboard_selection_calls.append(config or {})
        return self.keyboard_selection_results

    def tap_text_field(self, selector: dict | None = None) -> dict:
        return {"tapped": True, "selector": selector or {"auto": True}}

    def tap_first_button_matching(
        self,
        label_contains: list[str] | None = None,
        exclude_label_contains: list[str] | None = None,
        allow_any: bool = False,
    ) -> dict:
        tap = {
            "label": (label_contains or ["Login"])[0],
            "label_contains": label_contains or [],
            "matched_by": "label_contains",
            "allow_any": allow_any,
        }
        self.button_taps.append(tap)
        return tap

    def set_text_by_accessibility_id(self, accessibility_id: str, text: str, clear_first: bool = True) -> dict:
        entry = {"accessibility_id": accessibility_id, "text": text, "clear_first": clear_first}
        self.text_entries.append(entry)
        return entry

    def tap_by_accessibility_id(self, accessibility_id: str) -> dict:
        self.taps.append(accessibility_id)
        return {"accessibility_id": accessibility_id, "tapped": True}

    def type_text(self, text: str, config: dict | None = None) -> dict:
        result = {"text": text, "config": config or {}}
        self.typed_text.append(result)
        return result

    def query_app_state(self, bundle_id: str) -> int:
        return 4

    def screenshot(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"png")

    def page_source(self) -> str:
        return self.source

