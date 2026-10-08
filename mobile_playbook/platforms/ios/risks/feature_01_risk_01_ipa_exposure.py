"""
ios-feature-01-risk-01: acquires the IPA and runs static analysis to show its contents are exposed.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from mobile_playbook.common.storage_paths import ios_acquired_dir

from mobile_playbook.common.config_loader import merge_dicts
from mobile_playbook.platforms.ios.acquisition.registry import get_provider
from mobile_playbook.common.hashing import sha256_file
from mobile_playbook.platforms.ios.ipa.encryption import inspect_main_executable
from mobile_playbook.platforms.ios.ipa.unpacker import unpack_ipa
from mobile_playbook.platforms.ios.models import RiskRunResult
from mobile_playbook.platforms.ios.risks.base import Risk
from mobile_playbook.platforms.ios.static_analysis.critical_findings import OWASP_REFERENCE, critical_markdown
from mobile_playbook.platforms.ios.static_analysis.inventory import analyze_package
from mobile_playbook.platforms.ios.static_analysis.mobsf_report import analyze_with_mobsf
from mobile_playbook.platforms.static_analysis.analyzer import run_configured_analysis, write_analysis_reports

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
                ios_acquired_dir(app_config.artifact.get("workspace_dir")),
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
            analysis = run_configured_analysis(
                self.risk_id,
                risk_config,
                builtin=lambda: analyze_package(app_dir, acquisition, binary_inspection, risk_config),
                mobsf=lambda: analyze_with_mobsf(acquisition.ipa_path, acquisition, binary_inspection, risk_config),
            )
            report_paths = write_analysis_reports(report_dir, analysis, "ipa_analysis.json", critical_markdown(analysis["critical_findings"]))
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
                **{key: str(path) if path else None for key, path in report_paths.items()},
                "owasp_reference": OWASP_REFERENCE,
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
