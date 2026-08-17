from __future__ import annotations

from mobile_playbook.reporting.status_mapper import Evidence, TestResult
from mobile_playbook.platforms.android.models import AndroidRiskRunResult


def normalize_android_result(result: AndroidRiskRunResult) -> TestResult:
    return TestResult(
        run_timestamp=result.run_timestamp,
        platform="android",
        app_id=result.app_id,
        app_name=result.app_name,
        package_or_bundle_id=result.package_name,
        test_id=result.risk_id,
        test_name=result.risk_id,
        category=result.test_case_id,
        status=result.final_status,
        severity="info",
        summary="; ".join(result.errors[:2]),
        evidence=[Evidence(kind=item.get("kind", "file"), path=item.get("path", ""), label=item.get("label", "")) for item in result.evidence],
        started_at=result.timestamp_start,
        completed_at=result.timestamp_end,
        raw=result.to_dict(),
    )
