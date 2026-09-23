from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from mobile_playbook.reporting.messages import clean_message
from mobile_playbook.reporting.status_mapper import Evidence, TestResult
from mobile_playbook.platforms.ios.models import RiskRunResult

CATEGORY_BY_RISK = {
    "ios-feature-01-risk-01": "static_analysis",
    "ios-feature-02-risk-01": "network_security",
    "ios-feature-04-risk-01": "keyboard_security",
}

TEST_NAME_BY_RISK = {
    "ios-feature-01-risk-01": "IPA Static Analysis Exposure",
    "ios-feature-02-risk-01": "TLS Traffic Interception Exposure",
    "ios-feature-04-risk-01": "Custom Keyboard Keystroke Collection",
}

SEVERITY_BY_STATUS = {
    "RISK_EXISTS": "high",
    "KEYSTROKE_COLLECTION_NOT_OBSERVED": "low",
    "TRAFFIC_INTERCEPTION_NOT_OBSERVED": "low",
    "CUSTOM_KEYBOARD_NOT_AVAILABLE": "low",
    "IPA_ANALYSIS_COMPLETE": "info",
    "FAILED": "medium",
    "BEHAVIOR_FAILED": "medium",
    "INSTALL_FAILED": "medium",
    "LAUNCH_FAILED": "medium",
    "PAIRING_TIMEOUT": "medium",
    "PROXY_NOT_CONFIGURED": "medium",
    "PROXY_UNREACHABLE": "medium",
    "DEVICE_PROXY_SETUP_FAILED": "info",
    "CA_INSTALLATION_FAILED": "info",
    "CA_TRUST_FAILED": "info",
    "DEVICE_PROXY_RESTORE_FAILED": "info",
    "CAPTURE_PIPELINE_SILENT": "info",
    "CAPTURE_DATA_INVALID": "info",
    "CAPTURE_SOURCE_CHANGED": "info",
    "CAPTURE_SOURCE_UNAVAILABLE": "info",
}

CAPTURE_STATUS_SUMMARIES = {
    "DEVICE_PROXY_SETUP_FAILED": "The iPhone proxy could not be configured.",
    "CA_INSTALLATION_FAILED": "The Burp CA could not be installed on the iPhone.",
    "CA_TRUST_FAILED": "The Burp CA could not be fully trusted on the iPhone.",
    "DEVICE_PROXY_RESTORE_FAILED": "The original iPhone proxy settings could not be restored.",
    "CAPTURE_PIPELINE_SILENT": "No capture activity was observed; this could mean a broken pipeline, no app request, or a blocked TLS handshake.",
    "CAPTURE_DATA_INVALID": "New capture data existed but could not be parsed.",
    "CAPTURE_SOURCE_CHANGED": "The capture file was replaced or truncated during the test.",
    "CAPTURE_SOURCE_UNAVAILABLE": "The capture file could not be read.",
    "TRAFFIC_INTERCEPTION_NOT_OBSERVED": "Valid traffic was captured, but none matched the configured hosts.",
}


def normalize_ios_result(result: RiskRunResult) -> TestResult:
    return TestResult(
        run_timestamp=result.run_timestamp,
        platform="ios",
        app_id=result.app_id,
        app_name=result.app_name,
        package_or_bundle_id=result.original_bundle_id,
        test_id=result.risk_id,
        test_name=TEST_NAME_BY_RISK.get(result.risk_id, result.risk_id),
        category=CATEGORY_BY_RISK.get(result.risk_id, "ios"),
        status=result.final_status,
        verdict=result.verdict,
        severity=SEVERITY_BY_STATUS.get(result.final_status, "info"),
        summary=_summary(result),
        evidence=_evidence(result),
        started_at=result.timestamp_start,
        completed_at=result.timestamp_end,
        duration_seconds=_duration_seconds(result.timestamp_start, result.timestamp_end),
        report_path=f"ios/{result.app_id}/{result.risk_id}/{result.test_case_id}",
        raw={
            "feature_id": result.feature_id,
            "test_case_id": result.test_case_id,
            "test_case_type": result.test_case_type,
            "artifact_source": result.artifact_source,
        },
    )


def _summary(result: RiskRunResult) -> str:
    if result.errors:
        return "; ".join(clean_message(e) for e in result.errors[:2])
    if result.behavior_result and result.behavior_result.errors:
        return "; ".join(clean_message(e) for e in result.behavior_result.errors[:2])
    if result.artifact_result and result.artifact_result.errors:
        return "; ".join(clean_message(e) for e in result.artifact_result.errors[:2])
    if result.risk_id == "ios-feature-02-risk-01":
        return _traffic_interception_summary(result)
    return result.final_status


def _traffic_interception_summary(result: RiskRunResult) -> str:
    status_summary = CAPTURE_STATUS_SUMMARIES.get(result.final_status)
    if status_summary:
        return status_summary
    capture_summary = (result.launch_result or {}).get("capture_summary") or {}
    count = capture_summary.get("matched_count", 0)
    if not count:
        return result.final_status
    hosts = capture_summary.get("hosts") or []
    shown = ", ".join(hosts[:5])
    if len(hosts) > 5:
        shown += f", +{len(hosts) - 5} more"
    return f"{count} decrypted request(s) captured through Burp ({shown})" if shown else f"{count} decrypted request(s) captured through Burp"


def _evidence(result: RiskRunResult) -> list[Evidence]:
    paths: list[tuple[str, Path | None, str]] = [
        ("ipa", result.acquired_ipa, "Acquired IPA"),
        ("ipa", result.input_ipa, "Input IPA"),
    ]
    if result.behavior_result:
        paths.extend([
            ("screenshot", result.behavior_result.screenshot_path, "Behavior screenshot"),
            ("page_source", result.behavior_result.page_source_path, "Behavior page source"),
        ])
    if result.risk_id == "ios-feature-02-risk-01":
        capture_summary = (result.launch_result or {}).get("capture_summary") or {}
        evidence_path = capture_summary.get("evidence_path")
        if evidence_path:
            paths.append(("report", Path(evidence_path), "Burp capture results"))
    evidence = []
    seen = set()
    for kind, path, label in paths:
        if path is None:
            continue
        value = str(path)
        if value in seen:
            continue
        seen.add(value)
        evidence.append(Evidence(kind=kind, path=value, label=label))
    return evidence


def _duration_seconds(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    try:
        start_dt = datetime.fromisoformat(start)
        end_dt = datetime.fromisoformat(end)
    except ValueError:
        return None
    return round((end_dt - start_dt).total_seconds(), 3)
