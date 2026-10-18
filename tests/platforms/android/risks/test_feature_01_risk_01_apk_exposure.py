from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from mobile_playbook.common.storage_paths import android_intake_dir
from mobile_playbook.platforms.android.models import AndroidAppConfig
from mobile_playbook.platforms.android.risks import get_risk
from mobile_playbook.platforms.android.risks.feature_01_risk_01_apk_exposure import AndroidApkExposureRisk
from mobile_playbook.reporting.report_writer import ReportWriter

RISK_ID = "android-feature-01-risk-01"
PACKAGE = "com.example.app"
GOOGLE_KEY = "AIzaSyABCDEFGHIJKLMNOPQRSTUVWXY123456789"
MANIFEST_TREE = f"""N: android=http://schemas.android.com/apk/res/android
  E: manifest (line=2)
    A: android:versionCode(0x0101021b)=(type 0x10)0x7
    A: android:versionName(0x0101021c)="1.2.3" (Raw: "1.2.3")
    A: package="{PACKAGE}" (Raw: "{PACKAGE}")
    E: uses-sdk (line=7)
      A: android:targetSdkVersion(0x01010270)=(type 0x10)0x22
    E: uses-permission (line=9)
      A: android:name(0x01010003)="android.permission.CAMERA" (Raw: "android.permission.CAMERA")
    E: application (line=12)
      A: android:debuggable(0x0101000f)=(type 0x12)0xffffffff
      A: android:usesCleartextTraffic(0x010104ec)=(type 0x12)0xffffffff
      E: activity (line=20)
        A: android:name(0x01010003)="{PACKAGE}.DeepLinkActivity" (Raw: "{PACKAGE}.DeepLinkActivity")
        A: android:exported(0x01010010)=(type 0x12)0xffffffff
"""


def write_apk(path: Path, entries: dict[str, bytes]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return path


APK_NAME = "Example_1.2.3.apk"


@pytest.fixture
def write_intake_apk(apk_metadata):
    def write(package=PACKAGE) -> Path:
        apk_metadata[APK_NAME] = {"package_name": package, "version_code": "7", "version_name": "1.2.3", "display_name": "Example"}
        return write_apk(
            android_intake_dir() / APK_NAME,
            {
                "AndroidManifest.xml": b"\x03\x00\x08\x00binary",
                "classes.dex": b"dex\n035\x00" + f"\x00{GOOGLE_KEY}\x00".encode(),
                "assets/app_config.json": b'{"password":"super-secret-password-value"}',
                "res/layout/main.xml": b"\x03\x00\x08\x00compiled",
                "lib/arm64-v8a/libnative.so": b"\x7fELF",
            },
        )

    return write


def make_configs(risk_override=None, apk_static_analysis=None, tools=None):
    app = AndroidAppConfig(id="example_app", name="Example App", package_name=PACKAGE, risks={RISK_ID: {"enabled": True, **(risk_override or {})}})
    config = SimpleNamespace(apk_static_analysis=apk_static_analysis or {}, tools=tools or {})
    return app, config


def stub_sdk_tools(monkeypatch, tree=MANIFEST_TREE):
    monkeypatch.setattr("mobile_playbook.platforms.android.static_analysis.inventory.manifest_tree", lambda apk: tree)


def run(app, config, tmp_path):
    writer = ReportWriter(tmp_path / "reports", "run1")
    result = AndroidApkExposureRisk().run(app, config, None, writer)
    report_dir = tmp_path / "reports" / "run1" / "android" / "example_app" / RISK_ID / "apk_static_analysis"
    return result, report_dir


def flag_ids(report_dir: Path) -> set[str]:
    critical = json.loads((report_dir / "critical_findings.json").read_text())
    return {flag["id"] for flag in critical["flags"]}


def test_the_risk_is_registered_and_needs_no_device():
    risk = get_risk(RISK_ID)

    assert isinstance(risk, AndroidApkExposureRisk)
    assert risk.requires_device is False
    assert risk.requires == []


def test_builtin_analysis_of_intake_apks_flags_manifest_and_bundled_secrets(monkeypatch, tmp_path, write_intake_apk):
    write_intake_apk()
    stub_sdk_tools(monkeypatch)
    app, config = make_configs()

    result, report_dir = run(app, config, tmp_path)

    assert result.final_status == "APK_ANALYSIS_COMPLETE"
    assert result.verdict == "At Risk"
    assert result.metadata["analysis_provider"] == "builtin"
    assert [apk["name"] for apk in result.metadata["apks"]] == [APK_NAME]
    assert result.metadata["base_apk"] == str(android_intake_dir() / APK_NAME)
    analysis = json.loads((report_dir / "apk_analysis.json").read_text())
    inventory = json.loads((report_dir / "package_inventory.json").read_text())
    assert analysis["package_name"] == PACKAGE
    assert analysis["manifest"]["status"] == "PARSED"
    assert inventory["counts"]["apks"] == 1
    assert inventory["native_libraries"] == ["libnative.so"]
    assert {
        "APK_PACKAGE_ANALYZABLE",
        "DANGEROUS_PERMISSIONS_REQUESTED",
        "EXPORTED_COMPONENTS",
        "DEBUGGABLE_BUILD",
        "CLEARTEXT_TRAFFIC_ALLOWED",
        "NATIVE_LIBRARIES_PRESENT",
        "CANDIDATE_CONFIG_RESOURCES",
        "SENSITIVE_INFORMATION_EXPOSURE",
    } <= flag_ids(report_dir)
    match_types = {(item["path"], item["match_type"]) for item in analysis["sensitive_information_findings"]}
    assert ("classes.dex", "GOOGLE_API_KEY") in match_types
    assert ("assets/app_config.json", "SENSITIVE_KEY_VALUE") in match_types
    critical_text = (report_dir / "critical_findings.json").read_text()
    assert "super-secret-password-value" not in critical_text
    assert GOOGLE_KEY not in critical_text
    assert "# android-feature-01-risk-01 Critical Findings" in (report_dir / "critical_findings.md").read_text()
    assert {item["label"] for item in result.evidence} == {"APK analysis summary", "APK package inventory", "Critical findings", "Critical findings (Markdown)"}
    assert json.loads((report_dir / "report.json").read_text())["final_status"] == "APK_ANALYSIS_COMPLETE"


def test_missing_intake_apks_are_reported_without_analysis(tmp_path):
    app, config = make_configs()

    result, report_dir = run(app, config, tmp_path)

    assert result.final_status == "APK_NOT_FOUND"
    assert result.verdict == "Inconclusive"
    assert "artifacts/intake/android/apks/, named <App_Name>_<version>.apk" in result.errors[0]
    assert not (report_dir / "apk_analysis.json").exists()


def test_an_apk_of_a_different_app_is_not_analyzed(monkeypatch, tmp_path, write_intake_apk):
    write_intake_apk(package="com.other.app")
    stub_sdk_tools(monkeypatch)
    app, config = make_configs()

    result, report_dir = run(app, config, tmp_path)

    assert result.final_status == "APK_NOT_FOUND"
    assert result.verdict == "Inconclusive"
    assert not (report_dir / "apk_analysis.json").exists()


def test_builtin_analysis_still_completes_when_the_manifest_cannot_be_dumped(monkeypatch, tmp_path, write_intake_apk):
    write_intake_apk()

    def no_tools(apk):
        raise RuntimeError("APK inspection needs aapt, aapt2 or apkanalyzer on PATH")

    monkeypatch.setattr("mobile_playbook.platforms.android.static_analysis.inventory.manifest_tree", no_tools)
    app, config = make_configs()

    result, report_dir = run(app, config, tmp_path)

    analysis = json.loads((report_dir / "apk_analysis.json").read_text())
    assert result.final_status == "APK_ANALYSIS_COMPLETE"
    assert analysis["manifest"]["status"] == "UNAVAILABLE"
    assert "DEBUGGABLE_BUILD" not in flag_ids(report_dir)
    assert "SENSITIVE_INFORMATION_EXPOSURE" in flag_ids(report_dir)


def test_mobsf_analysis_uses_the_tools_url_and_key_when_the_analyzer_sets_none(monkeypatch, tmp_path, write_intake_apk):
    write_intake_apk()
    stub_sdk_tools(monkeypatch)
    scans = []

    def fake_mobsf_scan(apk_path, analyzer_config):
        scans.append((apk_path.name, analyzer_config))
        return {
            "base_url": analyzer_config["mobsf_url"],
            "hash": "abc123",
            "scan_type": "apk",
            "file_name": apk_path.name,
            "report": {
                "app_name": "MobSF Demo",
                "package_name": PACKAGE,
                "version_name": "1.2.3",
                "version_code": "7",
                "target_sdk": "34",
                "permissions": {
                    "android.permission.READ_CONTACTS": {"status": "dangerous", "info": "read contacts"},
                    "android.permission.INTERNET": {"status": "normal", "info": "internet"},
                },
                "exported_count": {"exported_activities": 2, "exported_services": 0, "exported_receivers": 1, "exported_providers": 0},
                "manifest_analysis": {
                    "manifest_findings": [{"rule": "app_is_debuggable", "title": "Debug Enabled For App", "severity": "high", "description": "debuggable"}]
                },
                "files": ["classes.dex", "lib/arm64-v8a/libnative.so", "assets/app_config.json"],
            },
        }

    monkeypatch.setattr("mobile_playbook.platforms.static_analysis.mobsf_client.mobsf_scan", fake_mobsf_scan)
    app, config = make_configs(
        apk_static_analysis={"analyzer": {"provider": "mobsf", "fallback_to_builtin": False}},
        tools={"mobsf_url": "http://mobsf.test:8000", "mobsf_api_key": "tools-key"},
    )

    result, report_dir = run(app, config, tmp_path)

    analysis = json.loads((report_dir / "apk_analysis.json").read_text())
    assert result.final_status == "APK_ANALYSIS_COMPLETE"
    assert result.metadata["analysis_provider"] == "mobsf"
    assert scans[0][0] == APK_NAME
    assert scans[0][1]["mobsf_url"] == "http://mobsf.test:8000"
    assert scans[0][1]["api_key"] == "tools-key"
    assert analysis["manifest"]["dangerous_permissions"] == ["android.permission.READ_CONTACTS"]
    assert analysis["manifest"]["exported_counts"] == {"activity": 2, "service": 0, "receiver": 1, "provider": 0}
    assert {"MOBSF_STATIC_ANALYSIS_FINDINGS", "DANGEROUS_PERMISSIONS_REQUESTED", "EXPORTED_COMPONENTS", "NATIVE_LIBRARIES_PRESENT"} <= flag_ids(report_dir)
    assert json.loads((report_dir / "mobsf_report.json").read_text())["app_name"] == "MobSF Demo"
    assert "MobSF report" in {item["label"] for item in result.evidence}


def test_an_app_override_beats_the_tools_mobsf_settings(monkeypatch, tmp_path, write_intake_apk):
    write_intake_apk()
    stub_sdk_tools(monkeypatch)
    scans = []
    monkeypatch.setattr(
        "mobile_playbook.platforms.static_analysis.mobsf_client.mobsf_scan",
        lambda apk_path, analyzer_config: scans.append(analyzer_config) or {"base_url": "", "hash": "h", "scan_type": "apk", "file_name": "base.apk", "report": {}},
    )
    app, config = make_configs(
        risk_override={"analyzer": {"provider": "mobsf", "mobsf_url": "http://override:8000", "api_key": "app-key"}},
        tools={"mobsf_url": "http://mobsf.test:8000", "mobsf_api_key": "tools-key"},
    )

    run(app, config, tmp_path)

    assert (scans[0]["mobsf_url"], scans[0]["api_key"]) == ("http://override:8000", "app-key")


def test_a_failed_mobsf_scan_falls_back_to_the_builtin_scanner(monkeypatch, tmp_path, write_intake_apk):
    write_intake_apk()
    stub_sdk_tools(monkeypatch)

    def unreachable(apk_path, analyzer_config):
        raise RuntimeError("MobSF is not reachable")

    monkeypatch.setattr("mobile_playbook.platforms.static_analysis.mobsf_client.mobsf_scan", unreachable)
    app, config = make_configs(apk_static_analysis={"analyzer": {"provider": "mobsf", "api_key": "k", "fallback_to_builtin": True}})

    result, report_dir = run(app, config, tmp_path)

    analysis = json.loads((report_dir / "apk_analysis.json").read_text())
    assert result.final_status == "APK_ANALYSIS_COMPLETE"
    assert result.metadata["analysis_provider"] == "builtin"
    assert analysis["mobsf_fallback"]["used"] is True
    assert "MOBSF_FALLBACK_USED" in flag_ids(report_dir)
    assert not (report_dir / "mobsf_report.json").exists()


def test_a_failed_mobsf_scan_without_fallback_fails_the_run(monkeypatch, tmp_path, write_intake_apk):
    write_intake_apk()
    stub_sdk_tools(monkeypatch)

    def unreachable(apk_path, analyzer_config):
        raise RuntimeError("MobSF is not reachable")

    monkeypatch.setattr("mobile_playbook.platforms.static_analysis.mobsf_client.mobsf_scan", unreachable)
    app, config = make_configs(apk_static_analysis={"analyzer": {"provider": "mobsf", "api_key": "k", "fallback_to_builtin": False}})

    result, _ = run(app, config, tmp_path)

    assert result.final_status == "FAILED"
    assert result.verdict == "Inconclusive"
    assert result.errors == ["MobSF is not reachable"]


def test_builtin_analysis_includes_code_and_libraries_from_splits(monkeypatch, tmp_path, write_apk_set):
    def archive(entries):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as output:
            for name, data in entries.items():
                output.writestr(name, data)
        return buffer.getvalue()
    paths = write_apk_set(android_intake_dir() / PACKAGE, files={
        "base.apk": archive({"AndroidManifest.xml": b"manifest"}),
        "split_feature.apk": archive({"classes.dex": b"dex" + GOOGLE_KEY.encode(), "lib/arm64-v8a/libsplit.so": b"ELF"}),
    })
    stub_sdk_tools(monkeypatch)
    result, report_dir = run(*make_configs(), tmp_path)
    assert result.final_status == "APK_ANALYSIS_COMPLETE"
    assert [item["path"] for item in result.metadata["apks"]] == [str(path) for path in paths]
    inventory = json.loads((report_dir / "package_inventory.json").read_text())
    assert inventory["counts"]["apks"] == 2
    assert inventory["native_libraries"] == ["libsplit.so"]
    assert "SENSITIVE_INFORMATION_EXPOSURE" in flag_ids(report_dir)


def test_mobsf_keeps_split_set_unchanged_and_declares_base_only_scope(monkeypatch, tmp_path, write_apk_set):
    paths = write_apk_set(android_intake_dir() / PACKAGE)
    original = [path.read_bytes() for path in paths]
    scans = []
    def scan(path, config):
        scans.append(path)
        return {"base_url": "", "hash": "h", "scan_type": "apk", "file_name": path.name, "report": {}}
    monkeypatch.setattr("mobile_playbook.platforms.static_analysis.mobsf_client.mobsf_scan", scan)
    result, report_dir = run(*make_configs(apk_static_analysis={"analyzer": {"provider": "mobsf"}}), tmp_path)
    assert result.final_status == "APK_ANALYSIS_COMPLETE"
    assert scans == [paths[0]]
    assert [path.read_bytes() for path in paths] == original
    summary = json.loads((report_dir / "apk_analysis.json").read_text())
    assert summary["analysis_scope"] == "base_apk"
    assert any("only the base APK" in finding for finding in summary["findings"])
