from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from mobile_playbook.orchestration.appium_process import AppiumStartResult, tcp_reachable
from mobile_playbook.orchestration.artifact_intake import app_matches_selector
from mobile_playbook.reporting.run_events import append_event

RiskGetter = Callable[[str], Any]
logger = logging.getLogger(__name__)


def appium_start_message(platform: str, appium_server_url: str, outcome: AppiumStartResult) -> str | None:
    if outcome.status == "ALREADY_RUNNING":
        return f"{platform}: Appium already reachable at {appium_server_url}."
    if outcome.status == "STARTED":
        return f"{platform}: Appium was not running — started it (log: {outcome.log_path})."
    if outcome.status == "DISABLED":
        return f"{platform}: Appium not reachable at {appium_server_url} and appium_auto_start is disabled."
    return None


def enabled_test_ids(app: Any, selected_tests: set[str] | None, get_risk: RiskGetter) -> Iterable[str]:
    for risk_id, risk_config in app.risks.items():
        if selected_tests and risk_id not in selected_tests:
            continue
        if not risk_config.get("enabled", False):
            continue
        risk = get_risk(risk_id)
        if risk is not None and not getattr(risk, "automation_available", True):
            continue
        yield risk_id


def iter_enabled_tests(
    config: Any, selected_tests: set[str] | None, selected_apps: set[str] | None, get_risk: RiskGetter
):
    for app in config.apps:
        if not app_matches_selector(app, selected_apps):
            continue
        for risk_id in enabled_test_ids(app, selected_tests, get_risk):
            yield app, risk_id


def requires_device(
    config: Any, selected_tests: set[str] | None, selected_apps: set[str] | None, get_risk: RiskGetter
) -> bool:
    for _, risk_id in iter_enabled_tests(config, selected_tests, selected_apps, get_risk):
        risk = get_risk(risk_id)
        if risk is not None and getattr(risk, "requires_device", True):
            return True
    return False


def ensure_appium_session(
    *,
    platform: str,
    appium_server_url: str,
    device_client: Any,
    run_dir: Path | None,
    fallback_dir: Path,
    close_device: Callable[[Any], None],
    connect_device: Callable[[], Any],
    on_reachable: Callable[[], None] | None = None,
    is_reachable: Callable[[str, int], bool] = tcp_reachable,
) -> Any:
    if is_reachable(appium_server_url, 2):
        if on_reachable is not None:
            on_reachable()
        return device_client

    message = f"{platform}: Appium server at {appium_server_url} is no longer reachable mid-run — attempting to recover and resume."
    logger.warning(message)
    append_event(run_dir or fallback_dir, "appium_recovery", message=message)
    try:
        close_device(device_client)
    except Exception as exc:
        logger.warning("%s: (ignoring failure while closing the broken session: %s)", platform, exc)
    return connect_device()
