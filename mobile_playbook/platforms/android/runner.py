"""
Android implementation of the platform runner used by scan orchestration.
"""

from __future__ import annotations

import logging
import time
from dataclasses import replace
from datetime import datetime
from functools import partial
from pathlib import Path

from mobile_playbook.common.storage_paths import android_work_dir

from mobile_playbook.orchestration.appium_server import ensure_appium_running, tcp_reachable
from mobile_playbook.orchestration.selection import app_matches_selector
from mobile_playbook.orchestration.platform_runner import (
    appium_start_message,
    enabled_test_ids,
    ensure_appium_session,
    iter_enabled_tests,
    requires_device,
)
from mobile_playbook.platforms.android.device.app_version import installed_version
from mobile_playbook.platforms.android.device.client import open_device
from mobile_playbook.platforms.android.device.corellium import reconnect_corellium
from mobile_playbook.platforms.android.device.corellium_roles import connect_claimed
from mobile_playbook.platforms.android.device.pool import AndroidDevicePool
from mobile_playbook.platforms.android.device_roles import plan_risk_roles, required_roles, risk_role
from mobile_playbook.platforms.android.models import AndroidRiskRunResult
from mobile_playbook.platforms.android.device.permissions import grant_all
from mobile_playbook.platforms.android.device.screen import keep_screen_on_while_charging, wake_and_unlock
from mobile_playbook.platforms.android.preflight import check_android_preflight
from mobile_playbook.platforms.android.risks import get_risk

logger = logging.getLogger(__name__)


class AndroidPlatformRunner:
    """Plans, connects and runs Android risk checks for the shared scan runner."""

    platform = "android"
    acquires_builds_during_tests = True

    # Report whether any planned Android risk needs a connected device.
    def requires_device(self, config, selected_tests: set[str] | None, selected_apps: set[str] | None = None) -> bool:
        return requires_device(config, selected_tests, selected_apps, get_risk)

    # Return the device roles the selected risks need, in the shared role order, rejecting malformed declarations.
    def required_device_roles(self, config, selected_tests: set[str] | None, selected_apps: set[str] | None = None) -> tuple[str, ...]:
        return required_roles(plan_risk_roles(self.iter_enabled_tests(config, selected_tests, selected_apps), get_risk))

    # Start Appium when it is down and auto-start allows, failing with its log tail when it cannot start.
    def _ensure_appium(self, config, log_dir: Path) -> None:
        outcome = ensure_appium_running(config.device.appium_server_url, getattr(config.device, "appium_auto_start", None), log_dir / "appium.log")
        message = appium_start_message(self.platform, config.device.appium_server_url, outcome)
        if message is not None:
            logger.info("%s", message)
        if outcome.status == "FAILED":
            logger.debug("android runner: Appium start failed: %s (log tail %s chars)", getattr(outcome, "error", None), len(getattr(outcome, "log_tail", None) or ""))
            detail = f" Appium log tail:\n{outcome.log_tail}" if outcome.log_tail else ""
            raise RuntimeError(f"android: {outcome.error}{detail}")

    # Ensure Appium is running, auto-starting it when configured, and return a connected device client.
    def connect_device(self, config, run_dir: Path | None = None):
        log_dir = run_dir or android_work_dir()
        logger.debug("android runner: connecting device (appium=%s, adb_path=%s, serial=%s, appium log dir=%s)", config.device.appium_server_url, getattr(config.device, "adb_path", None), getattr(config.device, "adb_serial", None), log_dir)
        self._ensure_appium(config, log_dir)
        client = open_device(config).connect()
        if config.runner.keep_screen_on:
            kept_on = keep_screen_on_while_charging(client.adb)
            logger.debug("android runner: keep screen on while charging -> %s", kept_on)
            if not kept_on:
                logger.warning("Could not set the device to keep its screen on while charging; tests still wake it first.")
        return client

    # Ensure Appium is running and connect every claimed role to its prepared Corellium instance, returning the pool
    # that routes each risk to its role; debugging-prompt diagnostics go to the run folder.
    def connect_role_devices(self, config, run_dir: Path | None, devices, prepared, corellium_client, *,
                             role_settings=None, policy=None, recognizer=None) -> AndroidDevicePool:
        log_dir = run_dir or android_work_dir()
        self._ensure_appium(config, log_dir)
        pool = connect_claimed(config, devices, prepared, corellium_client, diagnostics_dir=log_dir,
                               role_settings=role_settings, policy=policy, recognizer=recognizer)
        if config.runner.keep_screen_on:
            for role in pool.roles:
                if not keep_screen_on_while_charging(pool.device(role).client.adb):
                    logger.warning("Could not set the %s device to keep its screen on while charging; tests still wake it first.", role)
        return pool

    # Release the device client at the end of a run, which powers off a Corellium instance when auto_stop is set.
    def close_device(self, device_client) -> None:
        logger.debug("android runner: closing device client %s", type(device_client).__name__)
        device_client.release()

    # Return the device client, reconnecting when Appium has become unreachable mid-run. A Corellium device is
    # reconnected to the same instance it already uses; recovery never selects, restores, starts or stops a device.
    def ensure_device_healthy(self, config, device_client, run_dir: Path | None = None):
        if isinstance(device_client, AndroidDevicePool):
            reconnect = partial(self._recover_pool, config, device_client, run_dir)
        elif getattr(device_client, "corellium", None) is not None:
            reconnect = partial(self._reconnect_corellium, config, device_client, run_dir)
        else:
            reconnect = partial(self.connect_device, config, run_dir)
        return ensure_appium_session(
            platform=self.platform,
            appium_server_url=config.device.appium_server_url,
            device_client=device_client,
            run_dir=run_dir,
            fallback_dir=android_work_dir(),
            # Only quit the broken session: releasing would power off a Corellium instance the run still needs.
            close_device=lambda client: client.quit(),
            connect_device=reconnect,
            is_reachable=lambda url, timeout: tcp_reachable(url, timeout=timeout),
        )

    # Restart Appium and reconnect every role to its own claimed instance, returning the same pool.
    def _recover_pool(self, config, pool: AndroidDevicePool, run_dir: Path | None) -> AndroidDevicePool:
        self._ensure_appium(config, run_dir or android_work_dir())
        for role in pool.roles:
            device = pool.recover(role)
            logger.info("android: reconnected the %s device to Corellium instance %s.", role, device.instance_id)
        return pool

    # Restart Appium and reconnect adb to the Corellium instance the client already uses, returning the same client.
    def _reconnect_corellium(self, config, client, run_dir: Path | None):
        self._ensure_appium(config, run_dir or android_work_dir())
        reconnect_corellium(config.device.adb_path, client.corellium, config.device.corellium)
        return client

    # Yield (app, risk_id) pairs for the selected apps' enabled Android risks.
    def iter_enabled_tests(self, config, selected_tests: set[str] | None, selected_apps: set[str] | None):
        yield from iter_enabled_tests(config, selected_tests, selected_apps, get_risk)

    # Return the version name and version code of the app installed on the device, or None when it reports none.
    # Role devices must agree on the version they report.
    def installed_app_version(self, app, device_client) -> tuple[str, str | None] | None:
        if isinstance(device_client, AndroidDevicePool):
            found = {}
            for role in device_client.roles:
                version = installed_version(device_client.device(role).client.adb, app.package_name)
                if version is not None:
                    found[role] = (version.version_name, version.version_code)
            if len(set(found.values())) > 1:
                raise RuntimeError(f"The devices report different installed versions of {app.package_name}: {found}")
            return next(iter(found.values()), None)
        version = installed_version(device_client.adb, app.package_name)
        return (version.version_name, version.version_code) if version is not None else None

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
            role = risk_role(risk)
            device_client = self._route(device_client, role)
            logger.debug("android runner: preflight for %s/%s requires=%s role=%s", app.id, test_id, getattr(risk, "requires", []), role)
            # The scan runner connects no device when no planned risk needs one, so device_client can be None here.
            adb = device_client.adb if device_client is not None else None
            preflight = check_android_preflight(config, adb, getattr(risk, "requires", []))
            logger.debug("android runner: preflight for %s/%s ok=%s errors=%s warnings=%s", app.id, test_id, getattr(preflight, "ok", None), getattr(preflight, "errors", None), getattr(preflight, "warnings", None))
            if not preflight.ok:
                raise RuntimeError("; ".join(preflight.errors))
            if getattr(risk, "requires_device", True):
                # The screen may have timed out since the last test; a risk that uses the device drives its UI.
                wake_and_unlock(adb)
                if config.runner.auto_grant_permissions:
                    logger.debug("android runner: auto-granting permissions for %s", app.package_name)
                    grant_result = grant_all(adb, app.package_name)
                    logger.debug("android runner: grant result for %s: %s", app.package_name, grant_result)
                else:
                    logger.debug("android runner: auto_grant_permissions disabled; not granting for %s", app.package_name)
            else:
                logger.debug("android runner: %s does not use the device; skipping wake and permission grants", test_id)
            logger.debug("android runner: running %s (%s) for app %s package %s", test_id, type(risk).__name__, app.id, app.package_name)
            risk.run(app, config, device_client, report_writer)
            logger.debug("android runner: %s for app %s returned after %.2fs", test_id, app.id, time.monotonic() - started)
        except Exception as exc:
            logger.debug("android runner: %s for app %s failed after %.2fs: %s", test_id, app.id, time.monotonic() - started, exc, exc_info=True)
            self._record_failure(failure_result, report_writer, exc)

    # Return the client a risk of this role runs on: the role's own device from a pool, which is never another role's,
    # or the single connected device otherwise.
    def _route(self, device_client, role: str | None):
        if isinstance(device_client, AndroidDevicePool):
            return device_client.ready(role) if role is not None else None
        return device_client

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
