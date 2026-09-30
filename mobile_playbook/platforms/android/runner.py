"""
Android implementation of the platform runner used by scan orchestration.
"""

from __future__ import annotations

import logging
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from mobile_playbook.common.storage_paths import android_work_dir

from mobile_playbook.orchestration.appium_process import ensure_appium_running, tcp_reachable
from mobile_playbook.orchestration.artifact_intake import app_matches_selector
from mobile_playbook.orchestration.platform_runner import (
    appium_start_message,
    enabled_test_ids,
    ensure_appium_session,
    iter_enabled_tests,
    requires_device,
)
from mobile_playbook.platforms.android.adb import AdbClient
from mobile_playbook.platforms.android.device_client import AndroidDeviceClient
from mobile_playbook.platforms.android.models import AndroidRiskRunResult
from mobile_playbook.platforms.android.permissions import grant_all
from mobile_playbook.platforms.android.preflight import check_android_preflight
from mobile_playbook.platforms.android.risks import get_risk

logger = logging.getLogger(__name__)


class AndroidPlatformRunner:
    """Plans, connects and runs Android risk checks for the shared scan runner."""

    platform = "android"

    # Report whether any planned Android risk needs a connected device.
    def requires_device(self, config, selected_tests: set[str] | None, selected_apps: set[str] | None = None) -> bool:
        return requires_device(config, selected_tests, selected_apps, get_risk)

    # Ensure Appium is running, auto-starting it when configured, and return a connected device client.
    def connect_device(self, config, run_dir: Path | None = None):
        log_dir = run_dir or android_work_dir()
        logger.debug("android runner: connecting device (appium=%s, adb_path=%s, serial=%s, appium log dir=%s)", config.device.appium_server_url, getattr(config.device, "adb_path", None), getattr(config.device, "adb_serial", None), log_dir)
        outcome = ensure_appium_running(config.device.appium_server_url, getattr(config.device, "appium_auto_start", None), log_dir / "appium.log")
        message = appium_start_message(self.platform, config.device.appium_server_url, outcome)
        if message is not None:
            logger.info("%s", message)
        if outcome.status == "FAILED":
            logger.debug("android runner: Appium start failed: %s (log tail %s chars)", getattr(outcome, "error", None), len(getattr(outcome, "log_tail", None) or ""))
            detail = f" Appium log tail:\n{outcome.log_tail}" if outcome.log_tail else ""
            raise RuntimeError(f"android: {outcome.error}{detail}")
        adb = AdbClient(config.device.adb_path, config.device.adb_serial)
        return AndroidDeviceClient(config, adb).connect()

    # Release the device client.
    def close_device(self, device_client) -> None:
        logger.debug("android runner: closing device client %s", type(device_client).__name__)
        device_client.quit()

    # Return the device client, reconnecting when Appium has become unreachable mid-run.
    def ensure_device_healthy(self, config, device_client, run_dir: Path | None = None):
        return ensure_appium_session(
            platform=self.platform,
            appium_server_url=config.device.appium_server_url,
            device_client=device_client,
            run_dir=run_dir,
            fallback_dir=android_work_dir(),
            close_device=self.close_device,
            connect_device=lambda: self.connect_device(config, run_dir),
            is_reachable=lambda url, timeout: tcp_reachable(url, timeout=timeout),
        )

    # Yield (app, risk_id) pairs for the selected apps' enabled Android risks.
    def iter_enabled_tests(self, config, selected_tests: set[str] | None, selected_apps: set[str] | None):
        yield from iter_enabled_tests(config, selected_tests, selected_apps, get_risk)

    # Return no run-level preflight warnings, since Android checks run per risk.
    def preflight_warnings(self, config, planned_tests):
        return []

    # Run one risk for an app after its preflight and optional permission grant, recording a FAILED result on error.
    def run_test(self, app, test_id: str, config, device_client, report_writer) -> None:
        risk = get_risk(test_id)
        if risk is None:
            logger.debug("android runner: skip %s for app %s (risk not registered)", test_id, getattr(app, "id", app))
            return
        failure_result = self._failure_result_template(app, test_id, risk, report_writer)
        started = time.monotonic()
        try:
            logger.debug("android runner: preflight for %s/%s requires=%s", app.id, test_id, getattr(risk, "requires", []))
            preflight = check_android_preflight(config, device_client.adb, getattr(risk, "requires", []))
            logger.debug("android runner: preflight for %s/%s ok=%s errors=%s warnings=%s", app.id, test_id, getattr(preflight, "ok", None), getattr(preflight, "errors", None), getattr(preflight, "warnings", None))
            if not preflight.ok:
                raise RuntimeError("; ".join(preflight.errors))
            if config.runner.auto_grant_permissions:
                logger.debug("android runner: auto-granting permissions for %s", app.package_name)
                grant_result = grant_all(device_client.adb, app.package_name)
                logger.debug("android runner: grant result for %s: %s", app.package_name, grant_result)
            else:
                logger.debug("android runner: auto_grant_permissions disabled; not granting for %s", app.package_name)
            logger.debug("android runner: running %s (%s) for app %s package %s", test_id, type(risk).__name__, app.id, app.package_name)
            risk.run(app, config, device_client, report_writer)
            logger.debug("android runner: %s for app %s returned after %.2fs", test_id, app.id, time.monotonic() - started)
        except Exception as exc:
            logger.debug("android runner: %s for app %s failed after %.2fs: %s", test_id, app.id, time.monotonic() - started, exc, exc_info=True)
            self._record_failure(failure_result, report_writer, exc)

    # Build the FAILED result recorded when a risk run raises.
    def _failure_result_template(self, app, test_id: str, risk, report_writer) -> AndroidRiskRunResult:
        case_id = getattr(risk, "test_case_id", "") or "risk_execution_failed"
        return AndroidRiskRunResult(
            run_timestamp=report_writer.run_timestamp,
            timestamp_start="",
            timestamp_end=None,
            app_id=app.id,
            app_name=app.name,
            package_name=app.package_name,
            risk_id=test_id,
            test_case_id=case_id,
            test_case_type=getattr(risk, "test_case_type", "unhandled_exception"),
            artifact_source="installed_app",
            final_status="FAILED",
            errors=[],
        )

    # Write a timestamped FAILED result carrying the exception message.
    def _record_failure(self, failure_result: AndroidRiskRunResult, report_writer, exc: Exception) -> None:
        now = datetime.now().astimezone().isoformat()
        report_dir = report_writer.test_report_dir(
            failure_result.app_id,
            failure_result.risk_id,
            failure_result.test_case_id,
            platform="android",
        )
        logger.debug("android runner: recording FAILED result for %s/%s (%s) in %s: %s", failure_result.app_id, failure_result.risk_id, failure_result.test_case_id, report_dir, exc)
        report_writer.write_result(
            replace(failure_result, timestamp_start=now, timestamp_end=now, errors=[str(exc)]),
            report_dir,
        )

    # Yield an app's selected, enabled and automatable Android risk ids.
    def enabled_test_ids(self, app, selected_tests: set[str] | None):
        yield from enabled_test_ids(app, selected_tests, get_risk)

    # Describe the apps and risks a run would execute, without touching a device.
    def dry_run_lines(self, config, selected_tests: set[str] | None, selected_apps: set[str] | None = None) -> list[str]:
        lines = ["Dry run: no Android device, Appium session, APK install, or repackaging files will be touched."]
        for app in config.apps:
            if not app_matches_selector(app, selected_apps):
                continue
            lines.append(f"App: {app.id} ({app.package_name})")
            for risk_id in self.enabled_test_ids(app, selected_tests):
                lines.append(f"  planned: {risk_id}")
        return lines
