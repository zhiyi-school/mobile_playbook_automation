from __future__ import annotations

from datetime import datetime
from pathlib import Path

from mobile_playbook.platforms.ios.artifacts.registry import get_provider
from mobile_playbook.orchestration.appium_process import ensure_appium_running, tcp_reachable
from mobile_playbook.orchestration.artifact_intake import app_matches_selector
from mobile_playbook.platforms.ios.device import AppiumDeviceClient
from mobile_playbook.platforms.ios.models import RiskRunResult
from mobile_playbook.platforms.ios.preflight import check_ios_preflight
from mobile_playbook.platforms.ios.risks import get_risk
from mobile_playbook.reporting.run_events import append_event


class IosPlatformRunner:
    platform = "ios"

    def requires_device(self, config, selected_tests: set[str] | None, selected_apps: set[str] | None = None) -> bool:
        for app in config.apps:
            if not app_matches_selector(app, selected_apps):
                continue
            for risk_id in self.enabled_test_ids(app, selected_tests):
                risk = get_risk(risk_id)
                if risk is not None and getattr(risk, "requires_device", True):
                    return True
        return False

    def connect_device(self, config, run_dir: Path | None = None):
        log_dir = run_dir or Path("work/ios")
        outcome = ensure_appium_running(config.device.appium_server_url, getattr(config.device, "appium_auto_start", None), log_dir / "appium.log")
        if outcome.status == "ALREADY_RUNNING":
            print(f"ios: Appium already reachable at {config.device.appium_server_url}.")
        elif outcome.status == "STARTED":
            print(f"ios: Appium was not running — started it (log: {outcome.log_path}).")
        elif outcome.status == "DISABLED":
            print(f"ios: Appium not reachable at {config.device.appium_server_url} and appium_auto_start is disabled.")
        elif outcome.status == "FAILED":
            detail = f" Appium log tail:\n{outcome.log_tail}" if outcome.log_tail else ""
            raise RuntimeError(f"ios: {outcome.error}{detail}")
        preflight = check_ios_preflight(config)
        if not preflight.ok:
            raise RuntimeError("; ".join(preflight.errors))
        return AppiumDeviceClient(config.device).connect()

    def close_device(self, device_client) -> None:
        device_client.quit()

    def ensure_device_healthy(self, config, device_client, run_dir: Path | None = None):
        if tcp_reachable(config.device.appium_server_url, timeout=2):
            return device_client
        message = f"ios: Appium server at {config.device.appium_server_url} is no longer reachable mid-run — attempting to recover and resume."
        print(message)
        append_event(run_dir or Path("work/ios"), "appium_recovery", message=message)
        try:
            self.close_device(device_client)
        except Exception as exc:
            print(f"ios: (ignoring failure while closing the broken session: {exc})")
        return self.connect_device(config, run_dir)

    def iter_enabled_tests(self, config, selected_tests: set[str] | None, selected_apps: set[str] | None):
        for app in config.apps:
            if not app_matches_selector(app, selected_apps):
                continue
            for risk_id in self.enabled_test_ids(app, selected_tests):
                yield app, risk_id

    def run_test(self, app, test_id: str, config, device_client, report_writer) -> None:
        risk = get_risk(test_id)
        if risk is None:
            return
        try:
            risk.run(app, config, device_client, report_writer)
        except Exception as exc:
            self._record_failure(app, test_id, risk, report_writer, exc)

    def _record_failure(self, app, test_id: str, risk, report_writer, exc: Exception) -> None:
        # A single test's unhandled exception (a missing dependency, a device
        # hiccup, a bug not covered by that risk's own error handling) must
        # not abort every other app/risk still queued in this run — it's
        # recorded as one failed row here, and iteration continues.
        now = datetime.now().astimezone().isoformat()
        case_id = getattr(risk, "test_case_id", "") or "risk_execution_failed"
        report_dir = report_writer.test_report_dir(app.id, test_id, case_id)
        result = RiskRunResult(
            run_timestamp=report_writer.run_timestamp,
            timestamp_start=now,
            timestamp_end=now,
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
            errors=[str(exc)],
        )
        report_writer.write_result(result, report_dir)

    def enabled_test_ids(self, app, selected_tests: set[str] | None):
        for risk_id, risk_config in app.risks.items():
            if selected_tests and risk_id not in selected_tests:
                continue
            if risk_config.get("enabled", False):
                yield risk_id

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
                    print(f"{app.id}: UNSUPPORTED_ARTIFACT_SOURCE")
                    continue
                result = provider.acquire(app, config, client, run_timestamp, out_dir)
                results.append(result.to_dict())
                print(f"{app.id}: {result.status} {result.ipa_path or ''}")
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
