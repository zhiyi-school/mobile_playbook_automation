from __future__ import annotations

from pathlib import Path

from mobile_playbook.platforms.ios.behavior.app_specific import run_app_specific_check
from mobile_playbook.platforms.ios.models import BehaviorResult


FOREGROUND_STATES = {3, 4}


def run_expected_behavior_checks(device_client, bundle_id: str, expected_behavior, report_dir: Path) -> BehaviorResult:
    report_dir = Path(report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    screenshot_path = report_dir / "launch.png"
    page_source_path = report_dir / "page_source.xml"
    errors: list[str] = []
    foreground_state = None
    try:
        if expected_behavior.app_state_must_be_foreground:
            foreground_state = device_client.query_app_state(bundle_id)
            if foreground_state not in FOREGROUND_STATES:
                errors.append(f"Expected foreground app state, found {foreground_state}")
        device_client.screenshot(screenshot_path)
        source = device_client.page_source()
        page_source_path.write_text(source)
        for needle in expected_behavior.source_contains:
            if needle not in source:
                errors.append(f"Page source missing required text: {needle}")
        for needle in expected_behavior.source_not_contains:
            if needle in source:
                errors.append(f"Page source contained forbidden text: {needle}")
        app_specific = run_app_specific_check(expected_behavior.app_specific_check, getattr(device_client, "driver", None), report_dir)
        if app_specific and app_specific.get("status") != "PASS":
            errors.extend(app_specific.get("errors") or [f"App-specific check failed: {expected_behavior.app_specific_check}"])
        return BehaviorResult(
            status="PASS" if not errors else "BEHAVIOR_FAILED",
            foreground_state=foreground_state,
            screenshot_path=screenshot_path,
            page_source_path=page_source_path,
            errors=errors,
            metadata={"app_specific": app_specific},
        )
    except Exception as exc:
        return BehaviorResult(
            status="BEHAVIOR_FAILED",
            foreground_state=foreground_state,
            screenshot_path=screenshot_path if screenshot_path.exists() else None,
            page_source_path=page_source_path if page_source_path.exists() else None,
            errors=[str(exc)],
        )
