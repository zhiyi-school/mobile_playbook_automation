"""
Converts Android risk run results into the shared test result model.
"""

from __future__ import annotations

import logging
from datetime import datetime

from mobile_playbook.reporting.status_mapper import Evidence, TestResult
from mobile_playbook.platforms.android.models import AndroidRiskRunResult

logger = logging.getLogger(__name__)


# Map an Android risk run result, its evidence and timings onto a platform-neutral TestResult.
def normalize_android_result(result: AndroidRiskRunResult) -> TestResult:
    logger.debug("android results: normalizing %s/%s (%s) status=%s verdict=%s evidence=%s", result.app_id, result.risk_id, result.test_case_id, result.final_status, result.verdict, len(result.evidence))
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
        verdict=result.verdict,
        severity="info",
        summary="; ".join(result.errors[:2]) or result.final_status,
        evidence=[
            Evidence(kind=item.get("kind", "file"), path=item.get("path", ""), label=item.get("label", ""))
            for item in result.evidence
        ],
        started_at=result.timestamp_start,
        completed_at=result.timestamp_end,
        duration_seconds=_duration_seconds(result.timestamp_start, result.timestamp_end),
        report_path=f"android/{result.app_id}/{result.risk_id}/{result.test_case_id}",
        raw={
            "test_case_id": result.test_case_id,
            "test_case_type": result.test_case_type,
            "artifact_source": result.artifact_source,
            "metadata": result.metadata,
        },
    )


# Return the seconds between two ISO timestamps, or None when either is missing or unparseable.
def _duration_seconds(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    try:
        return round((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds(), 3)
    except ValueError as exc:
        logger.debug("android results: unparseable timestamps start=%r end=%r: %s", start, end, exc)
        return None
