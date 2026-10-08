"""
Android risk check that takes the app's APKs from the intake folder and runs static analysis to show their contents
are exposed.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from mobile_playbook.common.config_loader import merge_dicts
from mobile_playbook.common.hashing import sha256_file
from mobile_playbook.platforms.android.acquisition.apk import intake_apks
from mobile_playbook.platforms.android.apk_tools import inspect_apk_metadata
from mobile_playbook.platforms.android.models import AndroidRiskRunResult
from mobile_playbook.platforms.android.risks.base import AndroidRisk
from mobile_playbook.platforms.android.static_analysis.critical_findings import OWASP_REFERENCE, critical_markdown
from mobile_playbook.platforms.android.static_analysis.inventory import analyze_apk
from mobile_playbook.platforms.android.static_analysis.mobsf_report import analyze_apk_with_mobsf
from mobile_playbook.platforms.static_analysis.analyzer import run_configured_analysis, write_analysis_reports

logger = logging.getLogger(__name__)

REPORT_LABELS = {
    "analysis_path": "APK analysis summary",
    "inventory_path": "APK package inventory",
    "critical_findings_path": "Critical findings",
    "critical_findings_markdown_path": "Critical findings (Markdown)",
    "mobsf_report_path": "MobSF report",
}


class AndroidApkExposureRisk(AndroidRisk):
    risk_id = "android-feature-01-risk-01"
    feature_id = "feature-01"
    name = "Android APK Static Analysis"
    test_case_id = "apk_static_analysis"
    test_case_type = "mobsf_or_package_analysis"
    requires: list[str] = []
    # The APKs come from the intake folder, so no device is needed.
    requires_device = False

    # Analyze the app's intake APKs with the configured analyzer, write the evidence and record the verdict.
    def run(self, app_config, global_config, device_client, report_writer):
        report_dir = report_writer.test_report_dir(app_config.id, self.risk_id, self.test_case_id, platform="android")
        report_dir.mkdir(parents=True, exist_ok=True)
        risk_config = self._risk_config(app_config, global_config)
        result = AndroidRiskRunResult(
            run_timestamp=report_writer.run_timestamp,
            timestamp_start=datetime.now().astimezone().isoformat(),
            timestamp_end=None,
            app_id=app_config.id,
            app_name=app_config.name,
            package_name=app_config.package_name,
            risk_id=self.risk_id,
            test_case_id=self.test_case_id,
            test_case_type=self.test_case_type,
            artifact_source="apk_intake",
        )
        try:
            apks = intake_apks(app_config.package_name)
            logger.debug("%s[%s]: intake APKs %s", self.risk_id, app_config.id, [str(apk) for apk in apks])
            if not apks:
                result.final_status = "APK_NOT_FOUND"
                result.errors.append(
                    f"No APKs for {app_config.package_name} are in the intake folder. Put the app's APKs in "
                    f"artifacts/intake/android/apks/{app_config.package_name}/, then retry."
                )
                return result
            apk_info = self._apk_info(apks)
            result.metadata.update({"base_apk": apk_info["base_apk"], "apk_sha256": apk_info["apk_sha256"], "apks": apk_info["apks"]})
            if apk_info.get("package_name") and apk_info["package_name"] != app_config.package_name:
                result.final_status = "APK_PACKAGE_MISMATCH"
                result.errors.append(
                    f"The intake APK is {apk_info['package_name']}, not {app_config.package_name}. Replace the APKs in "
                    f"artifacts/intake/android/apks/{app_config.package_name}/ with this app's APKs, then retry."
                )
                return result

            base_apk = apks[0]
            analysis = run_configured_analysis(
                self.risk_id,
                risk_config,
                builtin=lambda: analyze_apk(apks, apk_info, risk_config),
                mobsf=lambda: analyze_apk_with_mobsf(base_apk, apk_info, risk_config),
            )
            report_paths = write_analysis_reports(report_dir, analysis, "apk_analysis.json", critical_markdown(analysis["critical_findings"]))
            critical = analysis["critical_findings"]
            result.final_status = "APK_ANALYSIS_COMPLETE"
            # A completed static analysis is itself the finding: any obtainable APK can be inventoried.
            result.verdict = "At Risk"
            result.errors.extend(flag["title"] for flag in critical["flags"][:3])
            result.evidence.extend(
                {"kind": "report", "path": str(path), "label": REPORT_LABELS[key]} for key, path in report_paths.items() if path is not None
            )
            result.metadata.update(
                {
                    "analysis_provider": analysis["summary"].get("analysis_provider", "builtin"),
                    **{key: str(path) if path else None for key, path in report_paths.items()},
                    "highest_severity": critical["highest_severity"],
                    "flag_ids": [flag["id"] for flag in critical["flags"]],
                    "owasp_reference": OWASP_REFERENCE,
                }
            )
            logger.debug(
                "%s[%s]: provider=%s flags=%s highest_severity=%s",
                self.risk_id, app_config.id, result.metadata["analysis_provider"], result.metadata["flag_ids"], critical["highest_severity"],
            )
        except Exception as exc:
            logger.debug("%s[%s]: run failed: %s", self.risk_id, app_config.id, exc, exc_info=True)
            result.final_status = "FAILED"
            result.errors.append(str(exc))
        finally:
            result.timestamp_end = datetime.now().astimezone().isoformat()
            logger.debug(
                "%s[%s]: result final_status=%s verdict=%s errors=%d; writing result to %s",
                self.risk_id, app_config.id, result.final_status, result.verdict, len(result.errors), report_dir,
            )
            report_writer.write_result(result, report_dir)
        return result

    # Merge the global apk_static_analysis settings with the app's override, taking the MobSF URL and API key from
    # the tools section when the analyzer does not set them.
    def _risk_config(self, app_config, global_config) -> dict[str, Any]:
        risk_config = merge_dicts(global_config.apk_static_analysis or {}, app_config.risks.get(self.risk_id) or {})
        analyzer = dict(risk_config.get("analyzer") or {})
        tools = global_config.tools or {}
        if not analyzer.get("mobsf_url") and tools.get("mobsf_url"):
            analyzer["mobsf_url"] = tools["mobsf_url"]
        if not analyzer.get("api_key") and tools.get("mobsf_api_key"):
            analyzer["api_key"] = tools["mobsf_api_key"]
        risk_config["analyzer"] = analyzer
        return risk_config

    # Hash every APK and read the base APK's package, version and label; the metadata is left out when no SDK tool
    # can read it, since the analyzers can still run.
    def _apk_info(self, apks: list[Path]) -> dict[str, Any]:
        base_apk = apks[0]
        hashed = [{"name": apk.name, "path": str(apk), "size": apk.stat().st_size, "sha256": sha256_file(apk)} for apk in apks]
        info: dict[str, Any] = {"base_apk": str(base_apk), "apk_sha256": hashed[0]["sha256"], "apks": hashed}
        try:
            info.update(inspect_apk_metadata(base_apk))
        except (RuntimeError, OSError) as exc:
            logger.debug("%s: metadata of %s unavailable: %s", self.risk_id, base_apk, exc, exc_info=True)
        return info
