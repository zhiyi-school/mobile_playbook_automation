"""
ios-feature-01-risk-01: acquires the IPA and runs static analysis to show its contents are exposed.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from mobile_playbook.common.storage_paths import ios_work_dir
from typing import Any

from mobile_playbook.common.config_loader import merge_dicts
from mobile_playbook.platforms.ios.artifacts.registry import get_provider
from mobile_playbook.platforms.ios.mutations.hashing import sha256_file
from mobile_playbook.platforms.ios.mutations.mutability import inspect_main_executable
from mobile_playbook.platforms.ios.ipa.unpacker import unpack_ipa
from mobile_playbook.platforms.ios.models import ArtifactAcquisitionResult, RiskRunResult
from mobile_playbook.platforms.ios.risks.base import Risk
from mobile_playbook.platforms.ios.risks.critical_markdown import critical_markdown, highest_severity, sort_flags_by_severity
from mobile_playbook.platforms.ios.risks.ipa_inventory import analyze_package
from mobile_playbook.platforms.ios.risks.mobsf_client import analyze_with_mobsf

logger = logging.getLogger(__name__)


class Feature01Risk01(Risk):
    risk_id = "ios-feature-01-risk-01"
    feature_id = "feature-01"
    name = "IPA acquisition static analysis exposure"
    requires_ipa_artifact = True
    requires_device = False

    # Acquire and unpack the IPA, run the configured analyzer, write evidence and record the verdict.
    def run(self, app_config, global_config, device_client, report_writer):
        risk_config = merge_dicts(global_config.ipa_static_analysis, app_config.risks.get(self.risk_id) or {})
        result = self._base_result(report_writer.run_timestamp, app_config)
        report_dir = report_writer.test_report_dir(app_config.id, self.risk_id, "ipa_static_analysis")
        work_dir = Path(global_config.runner.work_dir) / report_writer.run_timestamp / app_config.id / self.risk_id / "ipa_static_analysis"
        work_dir.mkdir(parents=True, exist_ok=True)
        logger.debug("ios-feature-01-risk-01[%s]: setup report_dir=%s work_dir=%s", app_config.id, report_dir, work_dir)
        try:
            artifact_source = app_config.artifact.get("source", "")
            logger.debug("ios-feature-01-risk-01[%s]: artifact source=%r", app_config.id, artifact_source)
            if artifact_source == "installed_app_reference":
                logger.debug("ios-feature-01-risk-01[%s]: installed_app_reference is black-box only; IPA required", app_config.id)
                result.final_status = "ARTIFACT_REQUIRED"
                result.errors.append("ios-feature-01-risk-01 requires an IPA artifact; installed_app_reference is black-box only")
                return [result]
            provider = get_provider(artifact_source)
            if provider is None:
                logger.debug("ios-feature-01-risk-01[%s]: no artifact provider for source %r", app_config.id, artifact_source)
                result.final_status = "UNSUPPORTED_ARTIFACT_SOURCE"
                result.errors.append(f"Unsupported artifact source: {app_config.artifact.get('source', '')}")
                return [result]

            logger.debug("ios-feature-01-risk-01[%s]: acquiring IPA via provider %s", app_config.id, type(provider).__name__)
            acquisition = provider.acquire(
                app_config,
                global_config,
                device_client,
                report_writer.run_timestamp,
                Path(app_config.artifact.get("workspace_dir") or ios_work_dir() / "acquired"),
            )
            result.artifact_result = acquisition
            logger.debug(
                "ios-feature-01-risk-01[%s]: acquisition status=%s ipa_path=%s errors=%s",
                app_config.id, acquisition.status, acquisition.ipa_path, acquisition.errors,
            )
            if acquisition.status == "INSTALLED_APP_VERIFIED":
                result.final_status = "ARTIFACT_REQUIRED"
                result.errors.append("ios-feature-01-risk-01 requires an IPA artifact; installed_app_reference is black-box only")
                return [result]
            if acquisition.status != "ACQUIRED" or acquisition.ipa_path is None:
                result.final_status = self._artifact_status_to_final(acquisition.status)
                result.errors.extend(acquisition.errors)
                return [result]

            result.acquired_ipa = acquisition.ipa_path
            result.acquired_ipa_sha256 = acquisition.input_sha256
            result.input_ipa = acquisition.ipa_path
            result.input_ipa_sha256 = acquisition.input_sha256 or sha256_file(acquisition.ipa_path)
            if acquisition.bundle_id:
                result.original_bundle_id = acquisition.bundle_id
            logger.debug(
                "ios-feature-01-risk-01[%s]: input ipa=%s sha256=%s bundle_id=%s",
                app_config.id, result.input_ipa, result.input_ipa_sha256, result.original_bundle_id,
            )

            try:
                app_dir = unpack_ipa(acquisition.ipa_path, work_dir / "unpacked")
            except Exception as exc:
                logger.debug("ios-feature-01-risk-01[%s]: unpack failed: %s", app_config.id, exc, exc_info=True)
                result.final_status = "UNPACK_FAILED"
                result.errors.append(str(exc))
                return [result]

            logger.debug("ios-feature-01-risk-01[%s]: unpacked app bundle at %s", app_config.id, app_dir)
            binary_inspection = inspect_main_executable(app_dir)
            result.binary_inspection_result = binary_inspection
            logger.debug(
                "ios-feature-01-risk-01[%s]: binary inspection status=%s encrypted=%s cryptid=%s",
                app_config.id, binary_inspection.status, getattr(binary_inspection, "encrypted", None),
                getattr(binary_inspection, "cryptid", None),
            )
            analysis = self._run_configured_analysis(acquisition.ipa_path, app_dir, acquisition, binary_inspection, risk_config)
            analysis_path = report_dir / "ipa_analysis.json"
            inventory_path = report_dir / "package_inventory.json"
            critical_path = report_dir / "critical_findings.json"
            critical_md_path = report_dir / "critical_findings.md"
            analysis_path.write_text(json.dumps(analysis["summary"], indent=2, sort_keys=True))
            inventory_path.write_text(json.dumps(analysis["inventory"], indent=2, sort_keys=True))
            critical_path.write_text(json.dumps(analysis["critical_findings"], indent=2, sort_keys=True))
            critical_md_path.write_text(critical_markdown(analysis["critical_findings"]))
            mobsf_report_path = None
            if analysis.get("mobsf_raw_report") is not None:
                mobsf_report_path = report_dir / "mobsf_report.json"
                mobsf_report_path.write_text(json.dumps(analysis["mobsf_raw_report"], indent=2, sort_keys=True))
            logger.debug(
                "ios-feature-01-risk-01[%s]: evidence written analysis=%s inventory=%s critical=%s critical_md=%s mobsf=%s",
                app_config.id, analysis_path, inventory_path, critical_path, critical_md_path, mobsf_report_path,
            )
            if logger.isEnabledFor(logging.DEBUG):
                flags = analysis["critical_findings"].get("flags") or []
                logger.debug(
                    "ios-feature-01-risk-01[%s]: verdict inputs provider=%s findings=%d critical_flags=%d highest_severity=%s flag_ids=%s",
                    app_config.id, analysis["summary"].get("analysis_provider", "builtin"),
                    len(analysis["summary"].get("findings") or []), len(flags),
                    analysis["critical_findings"].get("highest_severity"), [flag.get("id") for flag in flags[:20] if isinstance(flag, dict)],
                )
            result.final_status = "IPA_ANALYSIS_COMPLETE"
            
            # A completed static analysis is itself the finding: any acquired IPA can be inventoried.
            result.verdict = "At Risk"
            logger.debug("ios-feature-01-risk-01[%s]: completed static analysis; verdict=%s", app_config.id, result.verdict)
            result.errors.extend(item["title"] for item in analysis["critical_findings"]["flags"][:3])
            result.launch_result = {
                "analysis_provider": analysis["summary"].get("analysis_provider", "builtin"),
                "analysis_path": str(analysis_path),
                "inventory_path": str(inventory_path),
                "critical_findings_path": str(critical_path),
                "critical_findings_markdown_path": str(critical_md_path),
                "mobsf_report_path": str(mobsf_report_path) if mobsf_report_path else None,
                "owasp_reference": "https://mas.owasp.org/MASTG/techniques/ios/MASTG-TECH-0058/",
            }
            return [result]
        except Exception as exc:
            logger.debug("ios-feature-01-risk-01[%s]: run failed: %s", app_config.id, exc, exc_info=True)
            result.final_status = "FAILED"
            result.errors.append(str(exc))
            return [result]
        finally:
            result.timestamp_end = datetime.now(timezone.utc).isoformat()
            logger.debug(
                "ios-feature-01-risk-01[%s]: result final_status=%s verdict=%s errors=%d; writing result to %s",
                app_config.id, result.final_status, result.verdict, len(result.errors), report_dir,
            )
            report_writer.write_result(result, report_dir)

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
            test_case_id="ipa_static_analysis",
            test_case_type="mobsf_or_package_analysis",
            artifact_source=app_config.artifact.get("source", ""),
        )

    # Run the builtin or MobSF analyzer, falling back to builtin when allowed.
    def _run_configured_analysis(
        self,
        ipa_path: Path,
        app_dir: Path,
        acquisition: ArtifactAcquisitionResult,
        binary_inspection,
        risk_config: dict[str, Any],
    ) -> dict[str, Any]:
        analyzer_config = risk_config.get("analyzer") or {}
        provider = str(analyzer_config.get("provider") or "builtin").strip().lower()
        fallback_to_builtin = bool(analyzer_config.get("fallback_to_builtin", True))
        logger.debug("ios-feature-01-risk-01: analyzer provider=%s fallback_to_builtin=%s", provider, fallback_to_builtin)

        if provider in {"builtin", "package", "package_inventory", "local"}:
            return analyze_package(app_dir, acquisition, binary_inspection, risk_config)

        if provider in {"mobsf", "mobsf_api"}:
            try:
                return analyze_with_mobsf(ipa_path, acquisition, binary_inspection, risk_config)
            except Exception as exc:
                logger.debug("ios-feature-01-risk-01: MobSF analysis failed: %s", exc, exc_info=True)
                if not fallback_to_builtin:
                    raise
                logger.debug("ios-feature-01-risk-01: falling back to built-in package scanner after MobSF failure")
                analysis = analyze_package(app_dir, acquisition, binary_inspection, risk_config)
                analysis["summary"]["mobsf_fallback"] = {
                    "used": True,
                    "reason": str(exc),
                    "configured_provider": provider,
                }
                analysis["summary"]["findings"].append(f"MobSF analysis failed; package scanner fallback was used: {exc}")
                analysis["critical_findings"]["flags"].append(
                    {
                        "id": "MOBSF_FALLBACK_USED",
                        "severity": "LOW",
                        "title": "MobSF analysis was unavailable, built-in package scanner was used",
                        "evidence": [str(exc)],
                        "recommendation": "Start MobSF, verify the API key, and rerun ios-feature-01-risk-01 for a MobSF-backed report.",
                    }
                )
                analysis["critical_findings"]["flags"] = sort_flags_by_severity(analysis["critical_findings"]["flags"])
                analysis["critical_findings"]["flag_count"] = len(analysis["critical_findings"]["flags"])
                analysis["critical_findings"]["highest_severity"] = highest_severity(analysis["critical_findings"]["flags"])
                return analysis

        if fallback_to_builtin:
            logger.debug("ios-feature-01-risk-01: unknown analyzer provider %r; using built-in package scanner", provider)
            analysis = analyze_package(app_dir, acquisition, binary_inspection, risk_config)
            analysis["summary"]["analyzer_warning"] = f"Unknown analyzer provider '{provider}', built-in package scanner was used."
            return analysis
        raise ValueError(f"Unknown ios-feature-01-risk-01 analyzer provider: {provider}")

    # Map an artifact acquisition status to the risk's final status.
    def _artifact_status_to_final(self, status: str) -> str:
        mapping = {
            "ARTIFACT_REQUIRED": "ARTIFACT_REQUIRED",
            "ARTIFACT_NOT_FOUND": "ARTIFACT_NOT_FOUND",
            "ARTIFACT_INVALID": "ARTIFACT_INVALID",
            "ARTIFACT_BUNDLE_ID_MISMATCH": "ARTIFACT_BUNDLE_ID_MISMATCH",
            "ORIGINAL_APP_NOT_INSTALLED": "ORIGINAL_APP_NOT_INSTALLED",
            "UNSUPPORTED_ARTIFACT_SOURCE": "UNSUPPORTED_ARTIFACT_SOURCE",
        }
        return mapping.get(status, "ARTIFACT_ACQUISITION_FAILED")
