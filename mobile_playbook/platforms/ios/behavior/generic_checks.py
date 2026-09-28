from __future__ import annotations

import logging
from pathlib import Path

from mobile_playbook.platforms.ios.behavior.app_specific import run_app_specific_check
from mobile_playbook.platforms.ios.models import BehaviorResult


logger = logging.getLogger(__name__)

FOREGROUND_STATES = {3, 4}


def run_expected_behavior_checks(device_client, bundle_id: str, expected_behavior, report_dir: Path) -> BehaviorResult:
    report_dir = Path(report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    screenshot_path = report_dir / "launch.png"
    page_source_path = report_dir / "page_source.xml"
    errors: list[str] = []
    foreground_state = None
    logger.debug(
        "ios behavior[%s]: checks start (foreground_required=%s, source_contains=%s, source_not_contains=%s, app_specific=%s)",
        bundle_id,
        getattr(expected_behavior, "app_state_must_be_foreground", None),
        getattr(expected_behavior, "source_contains", None),
        getattr(expected_behavior, "source_not_contains", None),
        getattr(expected_behavior, "app_specific_check", None),
    )
    try:
        if expected_behavior.app_state_must_be_foreground:
            foreground_state = device_client.query_app_state(bundle_id)
            logger.debug("ios behavior[%s]: app state %s (foreground states %s)", bundle_id, foreground_state, sorted(FOREGROUND_STATES))
            if foreground_state not in FOREGROUND_STATES:
                errors.append(f"Expected foreground app state, found {foreground_state}")
        device_client.screenshot(screenshot_path)
        logger.debug("ios behavior[%s]: screenshot saved to %s", bundle_id, screenshot_path)
        source = device_client.page_source()
        page_source_path.write_text(source)
        logger.debug("ios behavior[%s]: page source (%s chars) saved to %s", bundle_id, len(source), page_source_path)
        for needle in expected_behavior.source_contains:
            if needle not in source:
                logger.debug("ios behavior[%s]: required text missing: %s", bundle_id, needle)
                errors.append(f"Page source missing required text: {needle}")
        for needle in expected_behavior.source_not_contains:
            if needle in source:
                logger.debug("ios behavior[%s]: forbidden text present: %s", bundle_id, needle)
                errors.append(f"Page source contained forbidden text: {needle}")
        app_specific = run_app_specific_check(expected_behavior.app_specific_check, getattr(device_client, "driver", None), report_dir)
        if app_specific and app_specific.get("status") != "PASS":
            errors.extend(app_specific.get("errors") or [f"App-specific check failed: {expected_behavior.app_specific_check}"])
        logger.debug("ios behavior[%s]: verdict %s with %s errors: %s", bundle_id, "PASS" if not errors else "BEHAVIOR_FAILED", len(errors), errors)
        return BehaviorResult(
            status="PASS" if not errors else "BEHAVIOR_FAILED",
            foreground_state=foreground_state,
            screenshot_path=screenshot_path,
            page_source_path=page_source_path,
            errors=errors,
            metadata={"app_specific": app_specific},
        )
    except Exception as exc:
        logger.debug("ios behavior[%s]: checks raised: %s", bundle_id, exc, exc_info=True)
        return BehaviorResult(
            status="BEHAVIOR_FAILED",
            foreground_state=foreground_state,
            screenshot_path=screenshot_path if screenshot_path.exists() else None,
            page_source_path=page_source_path if page_source_path.exists() else None,
            errors=[str(exc)],
        )
