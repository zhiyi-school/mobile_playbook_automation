"""
ios-feature-02-risk-01: proxies device traffic through Burp and checks for decrypted HTTPS from the app.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from mobile_playbook.storage import ios_capture_path, ios_work_dir, resolve_under_repository

from mobile_playbook.core.config_files import merge_dicts
from mobile_playbook.logging_setup import redacted, safe_url
from mobile_playbook.orchestration.appium_process import tcp_reachable
from mobile_playbook.platforms.ios.artifacts.registry import get_provider
from mobile_playbook.platforms.ios.burp_capture import (
    CaptureCursor,
    CaptureObservation,
    poll_capture,
    snapshot_capture,
)
from mobile_playbook.platforms.ios.models import ArtifactAcquisitionResult, CleanupResult, RiskRunResult
from mobile_playbook.platforms.ios.risks.base import Risk
from mobile_playbook.platforms.ios.traffic_interception_setup import (
    TrafficInterceptionSetupError,
    prepare_traffic_interception,
    restore_traffic_interception,
)

logger = logging.getLogger(__name__)


class Feature02Risk01(Risk):
    risk_id = "ios-feature-02-risk-01"
    feature_id = "feature-02"
    name = "TLS traffic interception exposure"
    requires_ipa_artifact = False

    # Install the app, point the device at Burp, exercise the app and judge the captured HTTPS traffic.
    def run(self, app_config, global_config, device_client, report_writer):
        result = self._base_result(report_writer.run_timestamp, app_config)
        report_dir = report_writer.test_report_dir(app_config.id, self.risk_id, "traffic_interception")
        risk_config = merge_dicts(global_config.traffic_interception, app_config.risks.get(self.risk_id) or {})
        burp_config = risk_config.get("burp") or {}
        exercise_config = risk_config.get("exercise") or {}
        installed_target_by_risk = False
        setup_state = None
        logger.debug("ios-feature-02-risk-01[%s]: setup report_dir=%s", app_config.id, report_dir)
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("ios-feature-02-risk-01[%s]: effective config %s", app_config.id, redacted(risk_config))
        try:
            proxy_url = str(burp_config.get("proxy_url") or "")
            if not proxy_url:
                logger.debug("ios-feature-02-risk-01[%s]: burp proxy_url not configured", app_config.id)
                result.final_status = "PROXY_NOT_CONFIGURED"
                result.errors.append(f"{self.risk_id} requires traffic_interception.burp.proxy_url to be set")
                return result
            logger.debug("ios-feature-02-risk-01[%s]: checking burp proxy reachability %s", app_config.id, safe_url(proxy_url))
            if not tcp_reachable(proxy_url, timeout=2):
                logger.debug("ios-feature-02-risk-01[%s]: burp proxy unreachable at %s", app_config.id, safe_url(proxy_url))
                result.final_status = "PROXY_UNREACHABLE"
                result.errors.append(f"Burp proxy not reachable at {proxy_url}. Start Burp Suite and confirm its listener matches this URL.")
                return result

            configured_capture_path = burp_config.get("capture_path")
            capture_path = resolve_under_repository(configured_capture_path) if configured_capture_path else ios_capture_path()
            logger.debug("ios-feature-02-risk-01[%s]: burp capture path %s (configured=%s)", app_config.id, capture_path, configured_capture_path)

            acquisition = self._prepare_app(app_config, global_config, device_client, report_writer.run_timestamp)
            result.artifact_result = acquisition
            logger.debug(
                "ios-feature-02-risk-01[%s]: acquisition status=%s ipa_path=%s errors=%s",
                app_config.id, acquisition.status, acquisition.ipa_path, acquisition.errors,
            )
            if acquisition.status not in {"ACQUIRED", "INSTALLED_APP_VERIFIED"}:
                result.final_status = self._artifact_status_to_final(acquisition.status)
                result.errors.extend(acquisition.errors)
                return result

            if acquisition.ipa_path is not None:
                logger.debug("ios-feature-02-risk-01[%s]: installing target %s", app_config.id, acquisition.ipa_path)
                install = device_client.install_app(acquisition.ipa_path, global_config.runner.app_install_timeout_ms)
                result.install_result = install
                installed_target_by_risk = install.status == "INSTALLED"
                logger.debug("ios-feature-02-risk-01[%s]: install status=%s errors=%s", app_config.id, install.status, install.errors)
                if install.status != "INSTALLED":
                    result.final_status = "INSTALL_FAILED"
                    result.errors.extend(install.errors)
                    return result

            result.launch_result = result.launch_result or {}
            logger.debug("ios-feature-02-risk-01[%s]: preparing device traffic interception", app_config.id)
            try:
                setup_state = prepare_traffic_interception(
                    device_client,
                    risk_config.get("device_setup") or {},
                    report_dir,
                )
                result.launch_result["device_setup"] = setup_state
                logger.debug(
                    "ios-feature-02-risk-01[%s]: device setup state %s",
                    app_config.id, redacted(setup_state) if isinstance(setup_state, dict) else setup_state,
                )
            except TrafficInterceptionSetupError as exc:
                logger.debug("ios-feature-02-risk-01[%s]: device setup failed status=%s: %s", app_config.id, exc.status, exc, exc_info=True)
                setup_state = exc.state
                result.launch_result["device_setup"] = exc.state
                result.final_status = exc.status
                return result

            bundle_id = app_config.bundle_id
            capture_cursor = snapshot_capture(capture_path)
            logger.debug(
                "ios-feature-02-risk-01[%s]: capture snapshot existed=%s offset=%s; launching %s",
                app_config.id, getattr(capture_cursor, "existed", None), getattr(capture_cursor, "offset", None), bundle_id,
            )
            try:
                app_launch = device_client.launch_app(bundle_id)
                result.launch_result["app_launch"] = app_launch
                logger.debug("ios-feature-02-risk-01[%s]: launch result %.200s", app_config.id, app_launch)
                alerts = list(device_client.handle_permission_alerts(global_config.runner.permission_alerts))
                time.sleep(float(global_config.runner.launch_wait_seconds))
                alerts.extend(device_client.handle_permission_alerts(global_config.runner.permission_alerts))
                result.launch_result["app_permission_alerts"] = alerts
                logger.debug("ios-feature-02-risk-01[%s]: permission alerts handled=%d", app_config.id, len(alerts))
            except Exception as exc:
                logger.debug("ios-feature-02-risk-01[%s]: launch failed: %s", app_config.id, exc, exc_info=True)
                result.final_status = "LAUNCH_FAILED"
                result.errors.append(str(exc))
                return result

            result.launch_result["exercise_navigation"] = self._exercise_app(device_client, exercise_config)

            exercise_wait = float(exercise_config.get("exercise_wait_seconds", 20))
            logger.debug(
                "ios-feature-02-risk-01[%s]: exercise navigation steps=%d; waiting %ss for traffic",
                app_config.id, len(result.launch_result["exercise_navigation"]), exercise_wait,
            )
            if exercise_wait > 0:
                time.sleep(exercise_wait)

            expected_hosts = [str(host) for host in (risk_config.get("expected_hosts") or [])]
            capture_timeout = float(risk_config.get("capture_timeout_seconds", 30))
            logger.debug("ios-feature-02-risk-01[%s]: collecting capture expected_hosts=%s timeout=%ss", app_config.id, expected_hosts, capture_timeout)
            capture = self._collect_capture(capture_cursor, expected_hosts, capture_timeout, report_dir)
            result.launch_result["capture_summary"] = capture["summary"]
            logger.debug("ios-feature-02-risk-01[%s]: verdict inputs capture summary %s", app_config.id, capture["summary"])

            result.final_status = self._capture_final_status(capture["summary"])
            if result.final_status == "RISK_EXISTS":
                result.verdict = "At Risk"
            logger.debug("ios-feature-02-risk-01[%s]: verdict final_status=%s verdict=%s", app_config.id, result.final_status, result.verdict)
            return result
        except Exception as exc:
            logger.debug("ios-feature-02-risk-01[%s]: run failed: %s", app_config.id, exc, exc_info=True)
            result.final_status = "FAILED"
            result.errors.append(str(exc))
            return result
        finally:
            if setup_state is not None:
                logger.debug("ios-feature-02-risk-01[%s]: restoring device traffic interception", app_config.id)
                try:
                    restored = restore_traffic_interception(device_client, setup_state, report_dir)
                    result.launch_result = result.launch_result or {}
                    device_setup = result.launch_result.setdefault("device_setup", setup_state)
                    device_setup["restore"] = restored
                    logger.debug(
                        "ios-feature-02-risk-01[%s]: restore result %s",
                        app_config.id, redacted(restored) if isinstance(restored, dict) else restored,
                    )
                except TrafficInterceptionSetupError as exc:
                    logger.debug("ios-feature-02-risk-01[%s]: restore failed status=%s: %s", app_config.id, exc.status, exc, exc_info=True)
                    result.final_status = "DEVICE_PROXY_RESTORE_FAILED"
                    result.verdict = "Inconclusive"
                    result.launch_result = result.launch_result or {}
                    device_setup = result.launch_result.setdefault("device_setup", setup_state)
                    device_setup["restore"] = exc.state
            result.cleanup_result = self._cleanup(app_config, global_config, device_client, installed_target_by_risk)
            result.timestamp_end = datetime.now(timezone.utc).isoformat()
            logger.debug(
                "ios-feature-02-risk-01[%s]: result final_status=%s verdict=%s errors=%d cleanup=%s; writing result to %s",
                app_config.id, result.final_status, result.verdict, len(result.errors),
                getattr(result.cleanup_result, "status", None), report_dir,
            )
            report_writer.write_result(result, report_dir)

    # Map a capture summary to the final status, from risk found to a silent pipeline.
    def _capture_final_status(self, summary: dict) -> str:
        if summary["matched_https_count"] > 0:
            return "RISK_EXISTS"
        if summary["source_unavailable"]:
            return "CAPTURE_SOURCE_UNAVAILABLE"
        if summary["source_changed"]:
            return "CAPTURE_SOURCE_CHANGED"
        if (
            summary["valid_entry_count"] == 0
            and (
                summary["new_line_count"] > 0
                or summary["malformed_entry_count"] > 0
                or summary["trailing_partial_line"]
            )
        ):
            return "CAPTURE_DATA_INVALID"
        if summary["valid_entry_count"] > 0:
            return "TRAFFIC_INTERCEPTION_NOT_OBSERVED"
        return "CAPTURE_PIPELINE_SILENT"

    # Create the initial run result for this risk and app.
    def _base_result(self, run_timestamp: str, app_config) -> RiskRunResult:
        return RiskRunResult(
            run_timestamp=run_timestamp,
            timestamp_start=datetime.now(timezone.utc).isoformat(),
            timestamp_end=None,
            app_id=app_config.id,
            app_name=app_config.name,
            original_bundle_id=app_config.bundle_id,
            test_bundle_id=app_config.test_bundle_id,
            risk_id=self.risk_id,
            feature_id=self.feature_id,
            test_case_id="traffic_interception",
            test_case_type="tls_proxy_capture",
            artifact_source=app_config.artifact.get("source", ""),
        )

    # Acquire the app through its configured artifact provider.
    def _prepare_app(self, app_config, global_config, device_client, run_timestamp: str) -> ArtifactAcquisitionResult:
        provider = get_provider(app_config.artifact.get("source", ""))
        if provider is None:
            logger.debug("ios-feature-02-risk-01[%s]: no artifact provider for source %r", app_config.id, app_config.artifact.get("source", ""))
            return ArtifactAcquisitionResult(
                app_config.id,
                app_config.artifact.get("source", ""),
                "UNSUPPORTED_ARTIFACT_SOURCE",
                errors=[f"Unsupported artifact source: {app_config.artifact.get('source', '')}"],
            )
        logger.debug("ios-feature-02-risk-01[%s]: acquiring app via provider %s", app_config.id, type(provider).__name__)
        return provider.acquire(
            app_config,
            global_config,
            device_client,
            run_timestamp,
            Path(app_config.artifact.get("workspace_dir") or ios_work_dir() / "acquired"),
        )

    # Tap each configured accessibility ID in turn, recording each step's result or error.
    def _exercise_app(self, device_client, exercise_config: dict) -> list[dict]:
        navigation = []
        settle_seconds = float(exercise_config.get("settle_seconds", 1))
        for index, accessibility_id in enumerate(exercise_config.get("accessibility_ids") or []):
            logger.debug("ios-feature-02-risk-01: exercise step %d tap accessibility_id=%r", index + 1, accessibility_id)
            try:
                tapped = device_client.tap_by_accessibility_id(accessibility_id)
                tapped["step"] = index + 1
                navigation.append(tapped)
                logger.debug("ios-feature-02-risk-01: exercise step %d result %.200s", index + 1, tapped)
            except Exception as exc:
                logger.debug("ios-feature-02-risk-01: exercise step %d failed: %s", index + 1, exc, exc_info=True)
                navigation.append({"step": index + 1, "accessibility_id": accessibility_id, "error": str(exc)})
            time.sleep(settle_seconds)
        return navigation

    # Poll the capture file until a match, a source problem or the timeout, then save matches.
    def _collect_capture(
        self,
        cursor: CaptureCursor,
        expected_hosts: list[str],
        timeout_seconds: float,
        report_dir: Path,
    ) -> dict:
        deadline = time.monotonic() + timeout_seconds
        aggregate = CaptureObservation(matched_entries=[])
        polls = 0
        while True:
            cursor, observed = poll_capture(cursor, expected_hosts)
            polls += 1
            logger.debug(
                "ios-feature-02-risk-01: capture poll %d new_lines=%d valid=%d malformed=%d https=%d matched=%d changed=%s unavailable=%s",
                polls, observed.new_line_count, observed.valid_entry_count, observed.malformed_entry_count,
                observed.https_entry_count, len(observed.matched_entries), observed.source_changed, observed.source_unavailable,
            )
            aggregate.matched_entries.extend(observed.matched_entries)
            aggregate.new_line_count += observed.new_line_count
            aggregate.valid_entry_count += observed.valid_entry_count
            aggregate.malformed_entry_count += observed.malformed_entry_count
            aggregate.https_entry_count += observed.https_entry_count
            aggregate.non_https_entry_count += observed.non_https_entry_count
            aggregate.unmatched_entry_count += observed.unmatched_entry_count
            aggregate.created_during_window |= observed.created_during_window
            aggregate.source_changed |= observed.source_changed
            aggregate.source_unavailable |= observed.source_unavailable
            aggregate.trailing_partial_line = observed.trailing_partial_line
            if (
                aggregate.matched_entries
                or aggregate.source_changed
                or aggregate.source_unavailable
                or time.monotonic() >= deadline
            ):
                break
            time.sleep(min(1, max(0, deadline - time.monotonic())))
        evidence_path = report_dir / "burp_capture.json"
        evidence_path.write_text(json.dumps(aggregate.matched_entries, indent=2, sort_keys=True))
        logger.debug(
            "ios-feature-02-risk-01: capture collected after %d poll(s); matched=%d evidence=%s",
            polls, len(aggregate.matched_entries), evidence_path,
        )
        return {
            "summary": {
                "new_line_count": aggregate.new_line_count,
                "valid_entry_count": aggregate.valid_entry_count,
                "malformed_entry_count": aggregate.malformed_entry_count,
                "https_entry_count": aggregate.https_entry_count,
                "non_https_entry_count": aggregate.non_https_entry_count,
                "matched_count": len(aggregate.matched_entries),
                "matched_https_count": len(aggregate.matched_entries),
                "unmatched_entry_count": aggregate.unmatched_entry_count,
                "created_during_window": aggregate.created_during_window,
                "source_changed": aggregate.source_changed,
                "source_unavailable": aggregate.source_unavailable,
                "trailing_partial_line": aggregate.trailing_partial_line,
                "expected_hosts": expected_hosts,
                "hosts": sorted(
                    {str(entry["host"]) for entry in aggregate.matched_entries if entry.get("host")}
                ),
                "evidence_path": str(evidence_path),
            },
        }

    # Uninstall the target when this risk installed it and the runner requires cleanup.
    def _cleanup(self, app_config, global_config, device_client, installed_target_by_risk: bool) -> CleanupResult:
        logger.debug("ios-feature-02-risk-01[%s]: cleanup installed_target_by_risk=%s", app_config.id, installed_target_by_risk)
        if not global_config.runner.uninstall_after_each_test or not installed_target_by_risk:
            return CleanupResult(status="SKIPPED", metadata={"installed_target_by_risk": installed_target_by_risk})
        try:
            if device_client.is_installed(app_config.bundle_id):
                removed = device_client.remove_app(app_config.bundle_id)
                return CleanupResult(
                    status="CLEANED" if removed else "CLEANUP_FAILED",
                    removed=removed,
                    errors=[] if removed else [f"Could not remove {app_config.bundle_id}"],
                )
            return CleanupResult(status="CLEANED", removed=False)
        except Exception as exc:
            logger.debug("ios-feature-02-risk-01[%s]: cleanup failed: %s", app_config.id, exc, exc_info=True)
            return CleanupResult(status="CLEANUP_FAILED", errors=[str(exc)])

    # Map an artifact acquisition status to the risk's final status.
    def _artifact_status_to_final(self, status: str) -> str:
        mapping = {
            "ARTIFACT_REQUIRED": "ARTIFACT_REQUIRED",
            "ARTIFACT_NOT_FOUND": "ARTIFACT_NOT_FOUND",
            "ARTIFACT_INVALID": "ARTIFACT_INVALID",
            "ARTIFACT_BUNDLE_ID_MISMATCH": "ARTIFACT_BUNDLE_ID_MISMATCH",
            "INSTALLED_APP_NOT_FOUND": "ARTIFACT_NOT_FOUND",
            "UNSUPPORTED_ARTIFACT_SOURCE": "UNSUPPORTED_ARTIFACT_SOURCE",
        }
        return mapping.get(status, "ARTIFACT_ACQUISITION_FAILED")
