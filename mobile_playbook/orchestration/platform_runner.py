"""
Platform-neutral scan planning and Appium session recovery helpers.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from mobile_playbook.orchestration.appium_server import AppiumStartResult, tcp_reachable
from mobile_playbook.orchestration.selection import app_matches_selector
from mobile_playbook.reporting.run_events import append_event

RiskGetter = Callable[[str], Any]
logger = logging.getLogger(__name__)


# Describe an Appium auto-start outcome for the run log, or None when there is nothing to report.
def appium_start_message(platform: str, appium_server_url: str, outcome: AppiumStartResult) -> str | None:
    logger.debug("%s: Appium start outcome status=%s error=%s log_path=%s", platform, getattr(outcome, "status", None), getattr(outcome, "error", None), getattr(outcome, "log_path", None))
    if outcome.status == "ALREADY_RUNNING":
        return f"{platform}: Appium already reachable at {appium_server_url}."
    if outcome.status == "STARTED":
        return f"{platform}: Appium was not running — started it (log: {outcome.log_path})."
    if outcome.status == "DISABLED":
        return f"{platform}: Appium not reachable at {appium_server_url} and appium_auto_start is disabled."
    return None


# Yield an app's risk ids that are selected, enabled in config and automatable.
def enabled_test_ids(app: Any, selected_tests: set[str] | None, get_risk: RiskGetter) -> Iterable[str]:
    app_id = getattr(app, "id", app)
    for risk_id, risk_config in app.risks.items():
        if selected_tests and risk_id not in selected_tests:
            logger.debug("planning: skip %s for app %s (not in selected tests %s)", risk_id, app_id, sorted(selected_tests))
            continue
        if not risk_config.get("enabled", False):
            logger.debug("planning: skip %s for app %s (disabled in config)", risk_id, app_id)
            continue
        risk = get_risk(risk_id)
        if risk is not None and not getattr(risk, "automation_available", True):
            logger.debug("planning: skip %s for app %s (automation not available)", risk_id, app_id)
            continue
        logger.debug("planning: enabled %s for app %s (registered=%s)", risk_id, app_id, risk is not None)
        yield risk_id


# Yield (app, risk_id) pairs for every selected app's enabled risks.
def iter_enabled_tests(
    config: Any, selected_tests: set[str] | None, selected_apps: set[str] | None, get_risk: RiskGetter
):
    for app in config.apps:
        if not app_matches_selector(app, selected_apps):
            logger.debug("planning: skip app %s (not in selected apps %s)", getattr(app, "id", app), selected_apps)
            continue
        for risk_id in enabled_test_ids(app, selected_tests, get_risk):
            yield app, risk_id


# Report whether any planned risk needs a connected device.
def requires_device(
    config: Any, selected_tests: set[str] | None, selected_apps: set[str] | None, get_risk: RiskGetter
) -> bool:
    for _, risk_id in iter_enabled_tests(config, selected_tests, selected_apps, get_risk):
        risk = get_risk(risk_id)
        if risk is not None and getattr(risk, "requires_device", True):
            logger.debug("planning: device required by %s", risk_id)
            return True
    logger.debug("planning: no planned risk requires a device")
    return False


# Return the device client while Appium is reachable, otherwise log a recovery event and reconnect.
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
        logger.debug("%s: Appium health check ok at %s", platform, appium_server_url)
        if on_reachable is not None:
            on_reachable()
        return device_client

    message = f"{platform}: Appium server at {appium_server_url} is no longer reachable mid-run — attempting to recover and resume."
    logger.warning(message)
    append_event(run_dir or fallback_dir, "appium_recovery", message=message)
    try:
        logger.debug("%s: closing broken device session before reconnecting", platform)
        close_device(device_client)
    except Exception as exc:
        logger.debug("%s: close of broken session failed: %s", platform, exc, exc_info=True)
        logger.warning("%s: (ignoring failure while closing the broken session: %s)", platform, exc)
    logger.debug("%s: reconnecting device after Appium recovery", platform)
    return connect_device()
