from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from mobile_playbook.reporting.messages import clean_message
from mobile_playbook.reporting.status_mapper import Evidence, TestResult
from mobile_playbook.platforms.ios.models import RiskRunResult

CATEGORY_BY_RISK = {
    "ios-feature1-risk1": "static_analysis",
    "ios-feature5-risk1": "keyboard_security",
}

TEST_NAME_BY_RISK = {
    "ios-feature1-risk1": "IPA Static Analysis Exposure",
    "ios-feature5-risk1": "Custom Keyboard Keystroke Collection",
}

SEVERITY_BY_STATUS = {
    "RISK_EXISTS": "high",
    "KEYSTROKE_COLLECTION_NOT_OBSERVED": "low",
    "CUSTOM_KEYBOARD_NOT_AVAILABLE": "low",
    "IPA_ANALYSIS_COMPLETE": "info",
    "FAILED": "medium",
    "BEHAVIOR_FAILED": "medium",
    "INSTALL_FAILED": "medium",
    "LAUNCH_FAILED": "medium",
    "PAIRING_TIMEOUT": "medium",
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
    return result.final_status


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
