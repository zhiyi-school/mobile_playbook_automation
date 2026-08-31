from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from mobile_playbook.platforms.ios.artifacts.registry import get_provider
from mobile_playbook.orchestration.appium_process import ensure_appium_running, tcp_reachable
from mobile_playbook.orchestration.artifact_intake import app_matches_selector
from mobile_playbook.orchestration.platform_runner import (
    appium_start_message,
    enabled_test_ids,
    ensure_appium_session,
    iter_enabled_tests,
    requires_device,
)
from mobile_playbook.platforms.ios.device import AppiumDeviceClient
from mobile_playbook.platforms.ios.models import RiskRunResult
from mobile_playbook.platforms.ios.preflight import check_ios_preflight
from mobile_playbook.platforms.ios.risks import get_risk
from mobile_playbook.reporting.run_events import append_event

logger = logging.getLogger(__name__)


class IosPlatformRunner:
    platform = "ios"

    def requires_device(self, config, selected_tests: set[str] | None, selected_apps: set[str] | None = None) -> bool:
        return requires_device(config, selected_tests, selected_apps, get_risk)

    def connect_device(self, config, run_dir: Path | None = None):
        log_dir = run_dir or Path("work/ios")
        outcome = ensure_appium_running(config.device.appium_server_url, getattr(config.device, "appium_auto_start", None), log_dir / "appium.log")
        message = appium_start_message(self.platform, config.device.appium_server_url, outcome)
        if message is not None:
            logger.info("%s", message)
        if outcome.status == "FAILED":
            detail = f" Appium log tail:\n{outcome.log_tail}" if outcome.log_tail else ""
            raise RuntimeError(f"ios: {outcome.error}{detail}")
        preflight = check_ios_preflight(config)
        if not preflight.ok:
            raise RuntimeError("; ".join(preflight.errors))
        client = AppiumDeviceClient(config.device).connect()
        self._unlock_best_effort(client, run_dir)
        return client

    def _unlock_best_effort(self, device_client, run_dir: Path | None) -> None:
        # Best-effort: only meaningfully unlocks a passcode-less device (Appium
        # can't enter a passcode or Face/Touch ID on a real device), and a
        # failure here shouldn't abort a run over what's otherwise a recoverable
        # device-state hiccup.
        try:
            result = device_client.unlock()
        except Exception as exc:
            logger.warning("ios: (could not check/unlock device screen, continuing anyway: %s)", exc)
            return
        if result.get("was_locked"):
            message = "ios: device screen was locked — unlocked automatically."
            logger.info(message)
            append_event(run_dir or Path("work/ios"), "device_unlocked", message=message)

    def close_device(self, device_client) -> None:
        device_client.quit()

    def ensure_device_healthy(self, config, device_client, run_dir: Path | None = None):
        return ensure_appium_session(
            platform=self.platform,
            appium_server_url=config.device.appium_server_url,
            device_client=device_client,
            run_dir=run_dir,
            fallback_dir=Path("work/ios"),
            close_device=self.close_device,
            connect_device=lambda: self.connect_device(config, run_dir),
            is_reachable=lambda url, timeout: tcp_reachable(url, timeout=timeout),
            on_reachable=lambda: self._unlock_best_effort(device_client, run_dir),
        )

    def iter_enabled_tests(self, config, selected_tests: set[str] | None, selected_apps: set[str] | None):
        yield from iter_enabled_tests(config, selected_tests, selected_apps, get_risk)

    def run_test(self, app, test_id: str, config, device_client, report_writer) -> None:
        risk = get_risk(test_id)
        if risk is None:
            return
        failure_result = self._failure_result_template(app, test_id, risk, report_writer)
        try:
            risk.run(app, config, device_client, report_writer)
        except Exception as exc:
            if self._unlock_and_retry(device_client, exc):
                try:
                    risk.run(app, config, device_client, report_writer)
                    return
                except Exception as retry_exc:
                    exc = retry_exc
            self._record_failure(failure_result, report_writer, exc)

    def _unlock_and_retry(self, device_client, exc: Exception) -> bool:
        # Only worth retrying if the device actually was locked — an unrelated
        # failure (bad selector, missing config, real app behavior) would just
        # fail the same way again, so this isn't a blind retry-on-any-error.
        try:
            result = device_client.unlock()
        except Exception:
            return False
        if result.get("was_locked"):
            logger.info("ios: test failed (%s) and the device was locked — unlocked it, retrying the test once.", exc)
            return True
        return False

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

    def _record_failure(self, failure_result: RiskRunResult, report_writer, exc: Exception) -> None:
        now = datetime.now().astimezone().isoformat()
        report_dir = report_writer.test_report_dir(
            failure_result.app_id,
            failure_result.risk_id,
            failure_result.test_case_id,
        )
        report_writer.write_result(
            replace(failure_result, timestamp_start=now, timestamp_end=now, errors=[str(exc)]),
            report_dir,
        )

    def enabled_test_ids(self, app, selected_tests: set[str] | None):
        yield from enabled_test_ids(app, selected_tests, get_risk)

    def acquire_artifacts(self, config, selected_apps: set[str] | None, run_timestamp: str, out_dir: Path) -> list[dict]:
        client = None
        results = []
        try:
            if any(
                app_matches_selector(app, selected_apps)
                and (app.artifact.get("source") == "installed_app_reference" or app.artifact.get("require_original_app_installed"))
                for app in config.apps
            ):
                client = self.connect_device(config, out_dir / run_timestamp)
            for app in config.apps:
                if not app_matches_selector(app, selected_apps):
                    continue
                provider = get_provider(app.artifact.get("source", ""))
                if provider is None:
                    logger.warning("%s: UNSUPPORTED_ARTIFACT_SOURCE", app.id)
                    continue
                result = provider.acquire(app, config, client, run_timestamp, out_dir)
                results.append(result.to_dict())
                logger.info("%s: %s %s", app.id, result.status, result.ipa_path or "")
        finally:
            if client is not None:
                self.close_device(client)
        return results

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
                elif risk_id == "ios-feature-01-risk-01":
                    lines.append("  planned: ios-feature-01-risk-01 / ipa_static_analysis / package_inventory")
                else:
                    lines.append(f"  planned: {risk_id}")
        return lines
