from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from mobile_playbook.core.config_files import merge_dicts
from mobile_playbook.orchestration.appium_process import tcp_reachable
from mobile_playbook.platforms.ios.artifacts.registry import get_provider
from mobile_playbook.platforms.ios.burp_capture import capture_line_count, read_new_capture_entries
from mobile_playbook.platforms.ios.models import ArtifactAcquisitionResult, CleanupResult, RiskRunResult
from mobile_playbook.platforms.ios.risks.base import Risk


class Feature02Risk01(Risk):
    risk_id = "ios-feature-02-risk-01"
    feature_id = "feature-02"
    name = "TLS traffic interception exposure"
    requires_ipa_artifact = False

    def run(self, app_config, global_config, device_client, report_writer):
        result = self._base_result(report_writer.run_timestamp, app_config)
        report_dir = report_writer.test_report_dir(app_config.id, self.risk_id, "traffic_interception")
        risk_config = merge_dicts(global_config.traffic_interception, app_config.risks.get(self.risk_id) or {})
        burp_config = risk_config.get("burp") or {}
        exercise_config = risk_config.get("exercise") or {}
        installed_target_by_risk = False
        try:
            proxy_url = str(burp_config.get("proxy_url") or "")
            if not proxy_url:
                result.final_status = "PROXY_NOT_CONFIGURED"
                result.errors.append(f"{self.risk_id} requires traffic_interception.burp.proxy_url to be set")
                return result
            if not tcp_reachable(proxy_url, timeout=2):
                result.final_status = "PROXY_UNREACHABLE"
                result.errors.append(f"Burp proxy not reachable at {proxy_url}. Start Burp Suite and confirm its listener matches this URL.")
                return result

            capture_path = Path(str(burp_config.get("capture_path") or "work/ios/traffic_interception/capture.jsonl"))
            start_line = capture_line_count(capture_path)

            acquisition = self._prepare_app(app_config, global_config, device_client, report_writer.run_timestamp)
            result.artifact_result = acquisition
            if acquisition.status not in {"ACQUIRED", "INSTALLED_APP_VERIFIED"}:
                result.final_status = self._artifact_status_to_final(acquisition.status)
                result.errors.extend(acquisition.errors)
                return result

            if acquisition.ipa_path is not None:
                install = device_client.install_app(acquisition.ipa_path, global_config.runner.app_install_timeout_ms)
                result.install_result = install
                installed_target_by_risk = install.status == "INSTALLED"
                if install.status != "INSTALLED":
                    result.final_status = "INSTALL_FAILED"
                    result.errors.extend(install.errors)
                    return result

            bundle_id = app_config.bundle_id
            try:
                app_launch = device_client.launch_app(bundle_id)
                result.launch_result = {"app_launch": app_launch}
                alerts = list(device_client.handle_permission_alerts(global_config.runner.permission_alerts))
                time.sleep(float(global_config.runner.launch_wait_seconds))
                alerts.extend(device_client.handle_permission_alerts(global_config.runner.permission_alerts))
                result.launch_result["app_permission_alerts"] = alerts
            except Exception as exc:
                result.final_status = "LAUNCH_FAILED"
                result.errors.append(str(exc))
                return result

            result.launch_result["exercise_navigation"] = self._exercise_app(device_client, exercise_config)

            exercise_wait = float(exercise_config.get("exercise_wait_seconds", 20))
            if exercise_wait > 0:
                time.sleep(exercise_wait)

            expected_hosts = [str(host).lower() for host in (risk_config.get("expected_hosts") or [])]
            capture_timeout = float(risk_config.get("capture_timeout_seconds", 30))
            capture = self._collect_capture(capture_path, start_line, expected_hosts, capture_timeout, report_dir)
            result.launch_result["capture_summary"] = capture["summary"]

            if capture["status"] == "DECRYPTED_TRAFFIC_OBSERVED":
                result.final_status = "RISK_EXISTS"
                result.verdict = "At Risk"
            else:
                result.final_status = "TRAFFIC_INTERCEPTION_NOT_OBSERVED"
                result.errors.append("No proxied traffic matching this app was observed through Burp during the exercise window.")
            return result
        except Exception as exc:
            result.final_status = "FAILED"
            result.errors.append(str(exc))
            return result
        finally:
            result.cleanup_result = self._cleanup(app_config, global_config, device_client, installed_target_by_risk)
            result.timestamp_end = datetime.now(timezone.utc).isoformat()
            report_writer.write_result(result, report_dir)

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

    def _prepare_app(self, app_config, global_config, device_client, run_timestamp: str) -> ArtifactAcquisitionResult:
        provider = get_provider(app_config.artifact.get("source", ""))
        if provider is None:
            return ArtifactAcquisitionResult(
                app_config.id,
                app_config.artifact.get("source", ""),
                "UNSUPPORTED_ARTIFACT_SOURCE",
                errors=[f"Unsupported artifact source: {app_config.artifact.get('source', '')}"],
            )
        return provider.acquire(
            app_config,
            global_config,
            device_client,
            run_timestamp,
            Path(app_config.artifact.get("workspace_dir") or "work/ios/acquired"),
        )

    def _exercise_app(self, device_client, exercise_config: dict) -> list[dict]:
        navigation = []
        settle_seconds = float(exercise_config.get("settle_seconds", 1))
        for index, accessibility_id in enumerate(exercise_config.get("accessibility_ids") or []):
            try:
                tapped = device_client.tap_by_accessibility_id(accessibility_id)
                tapped["step"] = index + 1
                navigation.append(tapped)
            except Exception as exc:
                navigation.append({"step": index + 1, "accessibility_id": accessibility_id, "error": str(exc)})
            time.sleep(settle_seconds)
        return navigation

    def _collect_capture(
        self,
        capture_path: Path,
        start_line: int,
        expected_hosts: list[str],
        timeout_seconds: float,
        report_dir: Path,
    ) -> dict:
        deadline = time.monotonic() + timeout_seconds
        matched: list[dict] = []
        while True:
            matched = read_new_capture_entries(capture_path, start_line, expected_hosts)
            if matched or time.monotonic() >= deadline:
                break
            time.sleep(1)
        evidence_path = report_dir / "burp_capture.json"
        evidence_path.write_text(json.dumps(matched, indent=2, sort_keys=True))
        status = "DECRYPTED_TRAFFIC_OBSERVED" if matched else "NO_TRAFFIC_CAPTURED"
        return {
            "status": status,
            "summary": {
                "matched_count": len(matched),
                "capture_path": str(capture_path),
                "evidence_path": str(evidence_path),
                "expected_hosts": expected_hosts,
                "hosts": sorted({str(entry["host"]) for entry in matched if entry.get("host")}),
            },
        }

    def _cleanup(self, app_config, global_config, device_client, installed_target_by_risk: bool) -> CleanupResult:
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
            return CleanupResult(status="CLEANUP_FAILED", errors=[str(exc)])

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
