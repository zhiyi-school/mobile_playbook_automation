from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime
from pathlib import Path

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
    platform = "android"

    def requires_device(self, config, selected_tests: set[str] | None, selected_apps: set[str] | None = None) -> bool:
        return requires_device(config, selected_tests, selected_apps, get_risk)

    def connect_device(self, config, run_dir: Path | None = None):
        log_dir = run_dir or Path("work/android")
        outcome = ensure_appium_running(config.device.appium_server_url, getattr(config.device, "appium_auto_start", None), log_dir / "appium.log")
        message = appium_start_message(self.platform, config.device.appium_server_url, outcome)
        if message is not None:
            logger.info("%s", message)
        if outcome.status == "FAILED":
            detail = f" Appium log tail:\n{outcome.log_tail}" if outcome.log_tail else ""
            raise RuntimeError(f"android: {outcome.error}{detail}")
        adb = AdbClient(config.device.adb_path, config.device.adb_serial)
        return AndroidDeviceClient(config, adb).connect()

    def close_device(self, device_client) -> None:
        device_client.quit()

    def ensure_device_healthy(self, config, device_client, run_dir: Path | None = None):
        return ensure_appium_session(
            platform=self.platform,
            appium_server_url=config.device.appium_server_url,
            device_client=device_client,
            run_dir=run_dir,
            fallback_dir=Path("work/android"),
            close_device=self.close_device,
            connect_device=lambda: self.connect_device(config, run_dir),
            is_reachable=lambda url, timeout: tcp_reachable(url, timeout=timeout),
        )

    def iter_enabled_tests(self, config, selected_tests: set[str] | None, selected_apps: set[str] | None):
        yield from iter_enabled_tests(config, selected_tests, selected_apps, get_risk)

    def run_test(self, app, test_id: str, config, device_client, report_writer) -> None:
        risk = get_risk(test_id)
        if risk is None:
            return
        failure_result = self._failure_result_template(app, test_id, risk, report_writer)
        try:
            preflight = check_android_preflight(config, device_client.adb, getattr(risk, "requires", []))
            if not preflight.ok:
                raise RuntimeError("; ".join(preflight.errors))
            if config.runner.auto_grant_permissions:
                grant_all(device_client.adb, app.package_name)
            risk.run(app, config, device_client, report_writer)
        except Exception as exc:
            self._record_failure(failure_result, report_writer, exc)

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

    def _record_failure(self, failure_result: AndroidRiskRunResult, report_writer, exc: Exception) -> None:
        now = datetime.now().astimezone().isoformat()
        report_dir = report_writer.test_report_dir(
            failure_result.app_id,
            failure_result.risk_id,
            failure_result.test_case_id,
            platform="android",
        )
        report_writer.write_result(
            replace(failure_result, timestamp_start=now, timestamp_end=now, errors=[str(exc)]),
            report_dir,
        )

    def enabled_test_ids(self, app, selected_tests: set[str] | None):
        yield from enabled_test_ids(app, selected_tests, get_risk)

    def dry_run_lines(self, config, selected_tests: set[str] | None, selected_apps: set[str] | None = None) -> list[str]:
        lines = ["Dry run: no Android device, Appium session, APK install, or repackaging files will be touched."]
        for app in config.apps:
            if not app_matches_selector(app, selected_apps):
                continue
            lines.append(f"App: {app.id} ({app.package_name})")
            for risk_id in self.enabled_test_ids(app, selected_tests):
                lines.append(f"  planned: {risk_id}")
        return lines
