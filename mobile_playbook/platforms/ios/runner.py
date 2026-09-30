"""
iOS platform runner: device connection, preflight, risk execution and artifact acquisition.
"""

from __future__ import annotations

import logging
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from mobile_playbook.common.storage_paths import ios_work_dir

from mobile_playbook.platforms.ios.acquisition.registry import get_provider
from mobile_playbook.orchestration.appium_server import ensure_appium_running, tcp_reachable
from mobile_playbook.orchestration.selection import app_matches_selector
from mobile_playbook.orchestration.platform_runner import (
    appium_start_message,
    enabled_test_ids,
    ensure_appium_session,
    iter_enabled_tests,
    requires_device,
)
from mobile_playbook.platforms.ios.config import effective_risk_config
from mobile_playbook.platforms.ios.device_client import AppiumDeviceClient
from mobile_playbook.platforms.ios.models import RiskRunResult
from mobile_playbook.platforms.ios.preflight import (
    check_ios_preflight,
    check_repackaging_preflight,
    check_screen_capture_preflight,
    check_traffic_interception_preflight,
)
from mobile_playbook.platforms.ios.risks import get_risk
from mobile_playbook.reporting.run_events import append_event

logger = logging.getLogger(__name__)


class IosPlatformRunner:
    platform = "ios"

    # Return whether any selected test for the selected apps needs a device.
    def requires_device(self, config, selected_tests: set[str] | None, selected_apps: set[str] | None = None) -> bool:
        needed = requires_device(config, selected_tests, selected_apps, get_risk)
        logger.debug("ios runner: requires_device=%s (tests=%s, apps=%s)", needed, selected_tests, selected_apps)
        return needed

    # Ensure Appium is running, pass device preflight, connect a session and try to unlock.
    def connect_device(self, config, run_dir: Path | None = None):
        log_dir = run_dir or ios_work_dir()
        logger.debug(
            "ios runner: connecting device udid=%s appium=%s log_dir=%s",
            getattr(config.device, "udid", None),
            config.device.appium_server_url,
            log_dir,
        )
        started = time.monotonic()
        outcome = ensure_appium_running(config.device.appium_server_url, getattr(config.device, "appium_auto_start", None), log_dir / "appium.log")
        logger.debug(
            "ios runner: ensure_appium_running status=%s in %.2fs",
            getattr(outcome, "status", None),
            time.monotonic() - started,
        )
        message = appium_start_message(self.platform, config.device.appium_server_url, outcome)
        if message is not None:
            logger.info("%s", message)
        if outcome.status == "FAILED":
            logger.debug("ios runner: Appium start failed: %s", outcome.error)
            detail = f" Appium log tail:\n{outcome.log_tail}" if outcome.log_tail else ""
            raise RuntimeError(f"ios: {outcome.error}{detail}")
        preflight = check_ios_preflight(config)
        logger.debug("ios runner: device preflight ok=%s errors=%s", preflight.ok, getattr(preflight, "errors", None))
        if not preflight.ok:
            raise RuntimeError("; ".join(preflight.errors))
        client = AppiumDeviceClient(config.device).connect()
        logger.debug("ios runner: Appium session connected in %.2fs", time.monotonic() - started)
        self._unlock_best_effort(client, run_dir)
        return client

    # Try to unlock the device screen, logging and recording an event when it was locked.
    def _unlock_best_effort(self, device_client, run_dir: Path | None) -> None:
        # Best-effort: Appium unlocks only passcode-less devices, and a failure must not abort the run.
        try:
            result = device_client.unlock()
        except Exception as exc:
            logger.debug("ios runner: best-effort unlock failed: %s", exc, exc_info=True)
            logger.warning("ios: (could not check/unlock device screen, continuing anyway: %s)", exc)
            return
        logger.debug("ios runner: best-effort unlock result %s", result)
        if result.get("was_locked"):
            message = "ios: device screen was locked — unlocked automatically."
            logger.info(message)
            append_event(run_dir or ios_work_dir(), "device_unlocked", message=message)

    # Quit the Appium session.
    def close_device(self, device_client) -> None:
        logger.debug("ios runner: closing device session")
        device_client.quit()
        logger.debug("ios runner: device session closed")

    # Check the Appium session and reconnect it when unhealthy.
    def ensure_device_healthy(self, config, device_client, run_dir: Path | None = None):
        logger.debug("ios runner: checking device health via %s", config.device.appium_server_url)
        return ensure_appium_session(
            platform=self.platform,
            appium_server_url=config.device.appium_server_url,
            device_client=device_client,
            run_dir=run_dir,
            fallback_dir=ios_work_dir(),
            close_device=self.close_device,
            connect_device=lambda: self.connect_device(config, run_dir),
            is_reachable=lambda url, timeout: tcp_reachable(url, timeout=timeout),
            on_reachable=lambda: self._unlock_best_effort(device_client, run_dir),
        )

    # Yield the enabled tests for the selected apps and tests.
    def iter_enabled_tests(self, config, selected_tests: set[str] | None, selected_apps: set[str] | None):
        yield from iter_enabled_tests(config, selected_tests, selected_apps, get_risk)

    # Collect traffic-interception, repackaging and screen-capture preflight warnings.
    def preflight_warnings(self, config, planned_tests):
        warnings = (
            check_traffic_interception_preflight(config, planned_tests)
            + check_repackaging_preflight(config, planned_tests)
            + check_screen_capture_preflight(config, planned_tests)
        )
        logger.debug(
            "ios runner: preflight produced %s warning(s): %s",
            len(warnings),
            [getattr(warning, "code", warning) for warning in warnings],
        )
        return warnings

    # Run a risk, retrying once after an unlock, and record a FAILED result if it raises.
    def run_test(self, app, test_id: str, config, device_client, report_writer) -> None:
        risk = get_risk(test_id)
        if risk is None:
            logger.debug("ios runner: skipping %s for app %s, no registered risk", test_id, getattr(app, "id", None))
            return
        failure_result = self._failure_result_template(app, test_id, risk, report_writer)
        logger.debug("ios runner: starting risk %s for app %s (%s)", test_id, app.id, type(risk).__name__)
        started = time.monotonic()
        try:
            risk.run(app, config, device_client, report_writer)
            logger.debug("ios runner: risk %s for app %s finished in %.2fs", test_id, app.id, time.monotonic() - started)
        except Exception as exc:
            logger.debug(
                "ios runner: risk %s for app %s raised after %.2fs: %s",
                test_id,
                app.id,
                time.monotonic() - started,
                exc,
                exc_info=True,
            )
            if self._unlock_and_retry(device_client, exc):
                logger.debug("ios runner: retrying risk %s for app %s after unlock", test_id, app.id)
                try:
                    risk.run(app, config, device_client, report_writer)
                    logger.debug(
                        "ios runner: retry of risk %s for app %s finished in %.2fs", test_id, app.id, time.monotonic() - started
                    )
                    return
                except Exception as retry_exc:
                    logger.debug("ios runner: retry of risk %s for app %s raised: %s", test_id, app.id, retry_exc, exc_info=True)
                    exc = retry_exc
            self._record_failure(failure_result, report_writer, exc)

    # Unlock the device and return True only if it had been locked.
    def _unlock_and_retry(self, device_client, exc: Exception) -> bool:
        # Retry only when the device was locked; other failures would fail the same way again.
        try:
            result = device_client.unlock()
        except Exception as unlock_exc:
            logger.debug("ios runner: unlock after failure raised, not retrying: %s", unlock_exc, exc_info=True)
            return False
        if result.get("was_locked"):
            logger.info("ios: test failed (%s) and the device was locked — unlocked it, retrying the test once.", exc)
            return True
        logger.debug("ios runner: device was not locked, not retrying (%s)", exc)
        return False

    # Build the FAILED result recorded when a risk raises.
    def _failure_result_template(self, app, test_id: str, risk, report_writer) -> RiskRunResult:
        case_id = getattr(risk, "test_case_id", "") or "risk_execution_failed"
        return RiskRunResult(
            run_timestamp=report_writer.run_timestamp,
            timestamp_start="",
            timestamp_end=None,
            app_id=app.id,
            app_name=app.name,
            original_bundle_id=app.bundle_id,
            test_bundle_id=app.test_bundle_id,
            risk_id=test_id,
            feature_id=getattr(risk, "feature_id", ""),
            test_case_id=case_id,
            test_case_type=getattr(risk, "test_case_type", "unhandled_exception"),
            artifact_source=(app.artifact or {}).get("source", ""),
            final_status="FAILED",
            errors=[],
        )

    # Write the FAILED result with the exception message and current timestamps.
    def _record_failure(self, failure_result: RiskRunResult, report_writer, exc: Exception) -> None:
        now = datetime.now().astimezone().isoformat()
        report_dir = report_writer.test_report_dir(
            failure_result.app_id,
            failure_result.risk_id,
            failure_result.test_case_id,
        )
        logger.debug(
            "ios runner: recording FAILED result for %s/%s (%s) in %s: %s",
            failure_result.app_id,
            failure_result.risk_id,
            failure_result.test_case_id,
            report_dir,
            exc,
        )
        report_writer.write_result(
            replace(failure_result, timestamp_start=now, timestamp_end=now, errors=[str(exc)]),
            report_dir,
        )

    # Yield the enabled test IDs for an app.
    def enabled_test_ids(self, app, selected_tests: set[str] | None):
        yield from enabled_test_ids(app, selected_tests, get_risk)

    # Acquire artifacts for the selected apps, connecting a device only when a source needs one.
    def acquire_artifacts(self, config, selected_apps: set[str] | None, run_timestamp: str, out_dir: Path) -> list[dict]:
        client = None
        results = []
        logger.debug(
            "ios runner: acquiring artifacts for %s configured app(s), selected=%s, out_dir=%s",
            len(config.apps),
            selected_apps,
            out_dir,
        )
        try:
            if any(
                app_matches_selector(app, selected_apps)
                and (app.artifact.get("source") == "installed_app_reference" or app.artifact.get("require_original_app_installed"))
                for app in config.apps
            ):
                logger.debug("ios runner: artifact acquisition needs a device session")
                client = self.connect_device(config, out_dir / run_timestamp)
            for app in config.apps:
                if not app_matches_selector(app, selected_apps):
                    logger.debug("ios runner: skipping artifact acquisition for %s, not selected", app.id)
                    continue
                provider = get_provider(app.artifact.get("source", ""))
                if provider is None:
                    logger.warning("%s: UNSUPPORTED_ARTIFACT_SOURCE", app.id)
                    continue
                logger.debug(
                    "ios runner: acquiring %s via %s (source %s)",
                    app.id,
                    type(provider).__name__,
                    app.artifact.get("source", ""),
                )
                started = time.monotonic()
                result = provider.acquire(app, config, client, run_timestamp, out_dir)
                logger.debug(
                    "ios runner: acquisition for %s returned %s in %.2fs (errors %s)",
                    app.id,
                    result.status,
                    time.monotonic() - started,
                    getattr(result, "errors", None),
                )
                results.append(result.to_dict())
                logger.info("%s: %s %s", app.id, result.status, result.ipa_path or "")
        finally:
            if client is not None:
                self.close_device(client)
        logger.debug("ios runner: artifact acquisition produced %s result(s)", len(results))
        return results

    # Describe the planned apps and risks without touching files, devices or Appium.
    def dry_run_lines(self, config, selected_tests: set[str] | None, selected_apps: set[str] | None = None) -> list[str]:
        lines = ["Dry run: no files, devices, installs, uninstalls, or Appium session will be touched."]
        for app in config.apps:
            if not app_matches_selector(app, selected_apps):
                continue
            lines.append(f"App: {app.id} ({app.name})")
            lines.append(f"  artifact.source: {app.artifact.get('source')}")
            for risk_id in self.enabled_test_ids(app, selected_tests):
                risk_config = app.risks.get(risk_id) or {}
                if risk_id == "ios-feature-04-risk-01":
                    collection = risk_config.get("collection") or risk_config.get("control") or {}
                    lines.append("  planned: ios-feature-04-risk-01 / collection_server / keystroke_collection")
                    lines.append(f"    bind_host: {collection.get('bind_host', '0.0.0.0')}")
                    lines.append(f"    port: {collection.get('port', 8765)}")
                    lines.append(f"    pair_timeout_seconds: {collection.get('pair_timeout_seconds', 60)}")
                    lines.append(f"    evidence_source: {collection.get('evidence_source', 'local_app_ui')}")
                    lines.append(
                        "    evidence_timeout_seconds: "
                        f"{collection.get('evidence_timeout_seconds', collection.get('event_timeout_seconds', 30))}"
                    )
                    lines.append(f"    probe_text: {collection.get('probe_text', 'hello123')}")
                elif risk_id == "ios-feature-03-risk-01":
                    effective = effective_risk_config(config, risk_id, risk_config)
                    recorder = effective.get("recorder_app") or {}
                    capture = effective.get("capture") or {}
                    lines.append("  planned: ios-feature-03-risk-01 / screen_capture / replaykit_broadcast")
                    lines.append(f"    recorder bundle_id: {recorder.get('bundle_id')}")
                    lines.append(f"    capture_window_seconds: {capture.get('capture_window_seconds', 12)}")
                    lines.append(f"    ocr_provider: {capture.get('ocr_provider', 'vision')}")
                elif risk_id == "ios-feature-01-risk-01":
                    lines.append("  planned: ios-feature-01-risk-01 / ipa_static_analysis / package_inventory")
                else:
                    lines.append(f"  planned: {risk_id}")
        logger.debug("ios runner: dry run produced %s line(s)", len(lines))
        return lines
