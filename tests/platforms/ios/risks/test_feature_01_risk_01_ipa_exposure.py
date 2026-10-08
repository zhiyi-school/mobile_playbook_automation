from __future__ import annotations

import json
import plistlib

from mobile_playbook.platforms.ios.models import BinaryInspectionResult
from mobile_playbook.reporting.report_writer import ReportWriter
from mobile_playbook.platforms.ios.static_analysis.critical_findings import critical_markdown
from mobile_playbook.platforms.ios.risks.feature_01_risk_01_ipa_exposure import Feature01Risk01
from mobile_playbook.platforms.ios.risks.registry import get_risk, list_risks
from tests.conftest import make_ipa


def test_feature_01_risk_01_registry():
    assert get_risk("ios-feature-01-risk-01").risk_id == "ios-feature-01-risk-01"
    assert any(risk["risk_id"] == "ios-feature-01-risk-01" for risk in list_risks())


def test_feature_01_risk_01_static_ipa_analysis(monkeypatch, global_config, tmp_path):
    monkeypatch.setattr(
        "mobile_playbook.platforms.ios.risks.feature_01_risk_01_ipa_exposure.inspect_main_executable",
        lambda app_dir: BinaryInspectionResult("MUTABLE_AS_PROVIDED", executable_path=app_dir / "AppExec"),
    )
    app = global_config.apps[0]
    app.risks = {"ios-feature-01-risk-01": {"enabled": True}}
    app.artifact["workspace_dir"] = str(tmp_path / "acquired")
    writer = ReportWriter(tmp_path / "reports", "run1")

    result = Feature01Risk01().run(app, global_config, None, writer)[0]
    writer.write_summary()

    assert result.final_status == "IPA_ANALYSIS_COMPLETE"
    assert result.verdict == "At Risk"
    report_dir = tmp_path / "reports" / "run1" / "ios" / "app_one" / "ios-feature-01-risk-01" / "ipa_static_analysis"
    analysis = json.loads((report_dir / "ipa_analysis.json").read_text())
    inventory = json.loads((report_dir / "package_inventory.json").read_text())
    critical = json.loads((report_dir / "critical_findings.json").read_text())
    assert analysis["bundle_id"] == "com.example.app"
    assert inventory["counts"]["files"] >= 2
    assert critical["highest_severity"] == "HIGH"
    assert critical["flags"][0]["id"] == "IPA_PACKAGE_ANALYZABLE"
    assert (report_dir / "critical_findings.md").exists()
    assert "IPA package can be acquired" in result.errors[0]


def test_feature_01_risk_01_flags_masked_sensitive_information(monkeypatch, global_config, tmp_path):
    monkeypatch.setattr(
        "mobile_playbook.platforms.ios.risks.feature_01_risk_01_ipa_exposure.inspect_main_executable",
        lambda app_dir: BinaryInspectionResult("MUTABLE_AS_PROVIDED", executable_path=app_dir / "AppExec"),
    )
    ipa = make_ipa(
        tmp_path / "sensitive.ipa",
        extra_files={
            "GoogleService-Info.plist": plistlib.dumps({"API_KEY": "AIzaSyABCDEFGHIJKLMNOPQRSTUVWXY123456789"}),
            "Config.json": b'{"password":"super-secret-password-value"}',
        },
    )
    app = global_config.apps[0]
    app.risks = {"ios-feature-01-risk-01": {"enabled": True}}
    app.artifact = {"source": "local_ipa", "ipa": str(ipa), "expected_bundle_id": "com.example.app", "workspace_dir": str(tmp_path / "acquired")}
    writer = ReportWriter(tmp_path / "reports", "run1")

    result = Feature01Risk01().run(app, global_config, None, writer)[0]

    report_dir = tmp_path / "reports" / "run1" / "ios" / "app_one" / "ios-feature-01-risk-01" / "ipa_static_analysis"
    critical = json.loads((report_dir / "critical_findings.json").read_text())
    sensitive = next(flag for flag in critical["flags"] if flag["id"] == "SENSITIVE_INFORMATION_EXPOSURE")
    assert result.final_status == "IPA_ANALYSIS_COMPLETE"
    assert sensitive["severity"] == "HIGH"
    assert any("AIz" in evidence or "supe" in evidence for evidence in sensitive["evidence"])
    assert "super-secret-password-value" not in json.dumps(critical)


def test_feature_01_risk_01_treats_api_keys_and_credentials_as_high(monkeypatch, global_config, tmp_path):
    monkeypatch.setattr(
        "mobile_playbook.platforms.ios.risks.feature_01_risk_01_ipa_exposure.inspect_main_executable",
        lambda app_dir: BinaryInspectionResult("MUTABLE_AS_PROVIDED", executable_path=app_dir / "AppExec"),
    )
    ipa = make_ipa(
        tmp_path / "sensitive.ipa",
        extra_files={
            "GoogleService-Info.plist": plistlib.dumps({"API_KEY": "AIzaSyABCDEFGHIJKLMNOPQRSTUVWXY123456789"}),
            "Config.json": b'{"credential":"service-account-value"}',
        },
    )
    app = global_config.apps[0]
    app.risks = {"ios-feature-01-risk-01": {"enabled": True, "sensitive_scan": {"reveal_values": True}}}
    app.artifact = {"source": "local_ipa", "ipa": str(ipa), "expected_bundle_id": "com.example.app", "workspace_dir": str(tmp_path / "acquired")}
    writer = ReportWriter(tmp_path / "reports", "run1")

    result = Feature01Risk01().run(app, global_config, None, writer)[0]

    report_dir = tmp_path / "reports" / "run1" / "ios" / "app_one" / "ios-feature-01-risk-01" / "ipa_static_analysis"
    analysis = json.loads((report_dir / "ipa_analysis.json").read_text())
    sensitive_findings = analysis["sensitive_information_findings"]
    google_key = next(item for item in sensitive_findings if item["match_type"] == "GOOGLE_API_KEY")
    credential = next(item for item in sensitive_findings if item.get("context") == "credential")
    assert result.final_status == "IPA_ANALYSIS_COMPLETE"
    assert google_key["severity"] == "HIGH"
    assert credential["severity"] == "HIGH"


def test_feature_01_risk_01_can_test_google_api_key_external_reuse(monkeypatch, global_config, tmp_path):
    monkeypatch.setattr(
        "mobile_playbook.platforms.ios.risks.feature_01_risk_01_ipa_exposure.inspect_main_executable",
        lambda app_dir: BinaryInspectionResult("MUTABLE_AS_PROVIDED", executable_path=app_dir / "AppExec"),
    )

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self, size=-1):
            return b'{"status":"OK","results":[{"formatted_address":"Singapore"}]}'

    monkeypatch.setattr("mobile_playbook.platforms.static_analysis.sensitive_findings.urllib.request.urlopen", lambda request, timeout: FakeResponse())
    ipa = make_ipa(
        tmp_path / "sensitive.ipa",
        extra_files={"GoogleService-Info.plist": plistlib.dumps({"API_KEY": "AIzaSyABCDEFGHIJKLMNOPQRSTUVWXY123456789"})},
    )
    app = global_config.apps[0]
    app.risks = {
        "ios-feature-01-risk-01": {
            "enabled": True,
            "sensitive_scan": {"reveal_values": True},
            "api_key_reuse_test": {"enabled": True, "timeout_seconds": 1},
        }
    }
    app.artifact = {"source": "local_ipa", "ipa": str(ipa), "expected_bundle_id": "com.example.app", "workspace_dir": str(tmp_path / "acquired")}
    writer = ReportWriter(tmp_path / "reports", "run1")

    result = Feature01Risk01().run(app, global_config, None, writer)[0]

    report_dir = tmp_path / "reports" / "run1" / "ios" / "app_one" / "ios-feature-01-risk-01" / "ipa_static_analysis"
    analysis = json.loads((report_dir / "ipa_analysis.json").read_text())
    critical = json.loads((report_dir / "critical_findings.json").read_text())
    reuse = next(flag for flag in critical["flags"] if flag["id"] == "GOOGLE_API_KEY_REUSE_TEST")
    assert result.final_status == "IPA_ANALYSIS_COMPLETE"
    assert analysis["api_key_reuse_test"]["results"][0]["status"] == "REUSABLE_FROM_WORKSTATION"
    assert reuse["severity"] == "HIGH"
    assert "REUSABLE_FROM_WORKSTATION" in reuse["evidence"][0]


def test_feature_01_risk_01_can_reveal_sensitive_information_when_configured(monkeypatch, global_config, tmp_path):
    monkeypatch.setattr(
        "mobile_playbook.platforms.ios.risks.feature_01_risk_01_ipa_exposure.inspect_main_executable",
        lambda app_dir: BinaryInspectionResult("MUTABLE_AS_PROVIDED", executable_path=app_dir / "AppExec"),
    )
    ipa = make_ipa(
        tmp_path / "sensitive.ipa",
        extra_files={"Config.json": b'{"password":"super-secret-password-value"}'},
    )
    app = global_config.apps[0]
    app.risks = {"ios-feature-01-risk-01": {"enabled": True, "sensitive_scan": {"reveal_values": True}}}
    app.artifact = {"source": "local_ipa", "ipa": str(ipa), "expected_bundle_id": "com.example.app", "workspace_dir": str(tmp_path / "acquired")}
    writer = ReportWriter(tmp_path / "reports", "run1")

    result = Feature01Risk01().run(app, global_config, None, writer)[0]

    report_dir = tmp_path / "reports" / "run1" / "ios" / "app_one" / "ios-feature-01-risk-01" / "ipa_static_analysis"
    critical_text = (report_dir / "critical_findings.json").read_text()
    markdown_text = (report_dir / "critical_findings.md").read_text()
    assert result.final_status == "IPA_ANALYSIS_COMPLETE"
    assert "super-secret-password-value" in critical_text
    assert "super-secret-password-value" in markdown_text


def test_feature_01_risk_01_uses_mobsf_when_configured(monkeypatch, global_config, tmp_path):
    monkeypatch.setattr(
        "mobile_playbook.platforms.ios.risks.feature_01_risk_01_ipa_exposure.inspect_main_executable",
        lambda app_dir: BinaryInspectionResult("PROTECTED_OR_ENCRYPTED_BINARY", cryptid=1, executable_path=app_dir / "AppExec"),
    )

    def fake_mobsf_scan(ipa_path, analyzer_config):
        return {
            "base_url": "http://127.0.0.1:8000",
            "hash": "abc123",
            "scan_type": "ipa",
            "file_name": ipa_path.name,
            "report": {
                "app_name": "MobSF Demo",
                "bundle_id": "com.example.app",
                "version": "1.2.3",
                "permissions": {"NSCameraUsageDescription": {"status": "warning"}},
                "code_analysis": {
                    "insecure_storage": {
                        "severity": "high",
                        "title": "Insecure storage API usage",
                        "file": "Payload/Example.app/AppExec",
                    }
                },
            },
        }

    monkeypatch.setattr("mobile_playbook.platforms.static_analysis.mobsf_client.mobsf_scan", fake_mobsf_scan)
    app = global_config.apps[0]
    app.risks = {"ios-feature-01-risk-01": {"enabled": True, "analyzer": {"provider": "mobsf", "api_key": "test-key"}}}
    app.artifact["workspace_dir"] = str(tmp_path / "acquired")
    writer = ReportWriter(tmp_path / "reports", "run1")

    result = Feature01Risk01().run(app, global_config, None, writer)[0]

    report_dir = tmp_path / "reports" / "run1" / "ios" / "app_one" / "ios-feature-01-risk-01" / "ipa_static_analysis"
    analysis = json.loads((report_dir / "ipa_analysis.json").read_text())
    critical = json.loads((report_dir / "critical_findings.json").read_text())
    raw_mobsf = json.loads((report_dir / "mobsf_report.json").read_text())
    assert result.final_status == "IPA_ANALYSIS_COMPLETE"
    assert result.launch_result["analysis_provider"] == "mobsf"
    assert analysis["analysis_provider"] == "mobsf"
    assert analysis["mobsf"]["hash"] == "abc123"
    assert raw_mobsf["app_name"] == "MobSF Demo"
    assert any(flag["id"] == "MOBSF_STATIC_ANALYSIS_FINDINGS" for flag in critical["flags"])


def test_feature_01_risk_01_falls_back_to_builtin_when_mobsf_fails(monkeypatch, global_config, tmp_path):
    monkeypatch.setattr(
        "mobile_playbook.platforms.ios.risks.feature_01_risk_01_ipa_exposure.inspect_main_executable",
        lambda app_dir: BinaryInspectionResult("MUTABLE_AS_PROVIDED", executable_path=app_dir / "AppExec"),
    )

    def fake_mobsf_scan(ipa_path, analyzer_config):
        raise RuntimeError("MobSF is not reachable")

    monkeypatch.setattr("mobile_playbook.platforms.static_analysis.mobsf_client.mobsf_scan", fake_mobsf_scan)
    app = global_config.apps[0]
    app.risks = {"ios-feature-01-risk-01": {"enabled": True, "analyzer": {"provider": "mobsf", "api_key": "test-key", "fallback_to_builtin": True}}}
    app.artifact["workspace_dir"] = str(tmp_path / "acquired")
    writer = ReportWriter(tmp_path / "reports", "run1")

    result = Feature01Risk01().run(app, global_config, None, writer)[0]

    report_dir = tmp_path / "reports" / "run1" / "ios" / "app_one" / "ios-feature-01-risk-01" / "ipa_static_analysis"
    analysis = json.loads((report_dir / "ipa_analysis.json").read_text())
    critical = json.loads((report_dir / "critical_findings.json").read_text())
    assert result.final_status == "IPA_ANALYSIS_COMPLETE"
    assert result.launch_result["analysis_provider"] == "builtin"
    assert analysis["analysis_provider"] == "builtin"
    assert analysis["mobsf_fallback"]["used"] is True
    assert not (report_dir / "mobsf_report.json").exists()
    assert any(flag["id"] == "MOBSF_FALLBACK_USED" for flag in critical["flags"])


def test_feature_01_risk_01_critical_markdown_orders_findings_and_evidence_by_severity():
    report = {
        "app": {"display_name": "Demo", "bundle_id": "com.example.demo", "version": "1.0", "build": "1"},
        "highest_severity": "HIGH",
        "flag_count": 3,
        "flags": [
            {"severity": "LOW", "title": "Low finding", "evidence": ["LOW low evidence"]},
            {
                "severity": "HIGH",
                "title": "High finding",
                "evidence": ["LOW low evidence", "HIGH high evidence", "MEDIUM medium evidence"],
            },
            {"severity": "MEDIUM", "title": "Medium finding", "evidence": ["MEDIUM medium evidence"]},
        ],
    }

    markdown = critical_markdown(report)

    assert markdown.index("| HIGH | High finding") < markdown.index("| MEDIUM | Medium finding")
    assert markdown.index("| MEDIUM | Medium finding") < markdown.index("| LOW | Low finding")
    assert markdown.index("HIGH high evidence") < markdown.index("MEDIUM medium evidence")
    assert markdown.index("MEDIUM medium evidence") < markdown.index("LOW low evidence")


def test_feature_01_risk_01_requires_ipa_artifact(global_config, tmp_path):
    app = global_config.apps[0]
    app.artifact = {"source": "installed_app_reference"}
    writer = ReportWriter(tmp_path / "reports", "run1")

    result = Feature01Risk01().run(app, global_config, None, writer)[0]

    assert result.final_status == "ARTIFACT_REQUIRED"
    assert result.verdict == "Inconclusive"
