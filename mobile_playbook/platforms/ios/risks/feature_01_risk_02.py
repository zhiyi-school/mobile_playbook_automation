"""
ios-feature-01-risk-02: repackages the IPA with a Frida gadget, resigns it and compares it to a clean baseline.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from mobile_playbook.platforms.ios.artifacts.registry import get_provider
from mobile_playbook.platforms.ios.config import effective_risk_config
from mobile_playbook.platforms.ios.ipa.unpacker import unpack_ipa
from mobile_playbook.platforms.ios.models import CleanupResult, RiskRunResult
from mobile_playbook.platforms.ios.mutations.hashing import sha256_file
from mobile_playbook.platforms.ios.mutations.repackage import (
    add_frida_gadget,
    frida_config_path,
    frida_dylib_path,
    inject_load_command,
    repack_ipa,
    set_bundle_identifier,
)
from mobile_playbook.platforms.ios.repackage_resign import (
    discover_provisioning_profile,
    ensure_provisioning_profile,
    resign_app,
    signing_identity_for_team,
)
from mobile_playbook.platforms.ios.risks.base import Risk
from mobile_playbook.platforms.ios.risks.repackaging_evidence import compare, confirm_gadget, exercise_and_observe
from mobile_playbook.common.logging_setup import redacted
from mobile_playbook.common.storage_paths import ios_work_dir

logger = logging.getLogger(__name__)


class Feature01Risk02(Risk):
    risk_id = "ios-feature-01-risk-02"
    feature_id = "feature-01"
    name = "IPA repackaging resistance"
    requires_ipa_artifact = True
    requires_device = True
    test_case_id = "repackaging"
    test_case_type = "ipa_repackage_resign_validate"

    # Baseline the clean app, inject and resign a repackaged build, then compare runs and confirm the gadget.
    def run(self, app_config, global_config, device_client, report_writer):
        result = self._base_result(report_writer.run_timestamp, app_config)
        report_dir = report_writer.test_report_dir(app_config.id, self.risk_id, self.test_case_id)
        cfg = effective_risk_config(global_config, self.risk_id, app_config.risks.get(self.risk_id))
        work_dir = (
            Path(global_config.runner.work_dir)
            / report_writer.run_timestamp
            / app_config.id
            / self.risk_id
            / self.test_case_id
        )
        work_dir.mkdir(parents=True, exist_ok=True)
        result.launch_result = {}
        installed_by_risk = False
        repackaged_bundle_id = app_config.bundle_id
        logger.debug("ios-feature-01-risk-02[%s]: setup report_dir=%s work_dir=%s", app_config.id, report_dir, work_dir)
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("ios-feature-01-risk-02[%s]: effective config %s", app_config.id, redacted(cfg))
        try:
            source = app_config.artifact.get("source", "")
            logger.debug("ios-feature-01-risk-02[%s]: artifact source=%r", app_config.id, source)
            if source == "installed_app_reference":
                logger.debug("ios-feature-01-risk-02[%s]: installed_app_reference cannot be repackaged; IPA required", app_config.id)
                result.final_status = "ARTIFACT_REQUIRED"
                result.errors.append(f"{self.risk_id} requires a supplied IPA artifact; installed_app_reference cannot be repackaged")
                return result
            provider = get_provider(source)
            if provider is None:
                logger.debug("ios-feature-01-risk-02[%s]: no artifact provider for source %r", app_config.id, source)
                result.final_status = "UNSUPPORTED_ARTIFACT_SOURCE"
                result.errors.append(f"Unsupported artifact source: {source}")
                return result

            logger.debug("ios-feature-01-risk-02[%s]: acquiring IPA via provider %s", app_config.id, type(provider).__name__)
            acquisition = provider.acquire(
                app_config,
                global_config,
                device_client,
                report_writer.run_timestamp,
                Path(app_config.artifact.get("workspace_dir") or ios_work_dir() / "acquired"),
            )
            result.artifact_result = acquisition
            logger.debug(
                "ios-feature-01-risk-02[%s]: acquisition status=%s ipa_path=%s errors=%s",
                app_config.id, acquisition.status, acquisition.ipa_path, acquisition.errors,
            )
            if acquisition.status == "INSTALLED_APP_VERIFIED":
                result.final_status = "ARTIFACT_REQUIRED"
                result.errors.append(f"{self.risk_id} requires a supplied IPA artifact; installed_app_reference cannot be repackaged")
                return result
            if acquisition.status != "ACQUIRED" or acquisition.ipa_path is None:
                result.final_status = self._artifact_status_to_final(acquisition.status)
                result.errors.extend(acquisition.errors)
                return result

            result.input_ipa = acquisition.ipa_path
            result.input_ipa_sha256 = acquisition.input_sha256 or sha256_file(acquisition.ipa_path)
            if acquisition.bundle_id:
                result.original_bundle_id = acquisition.bundle_id
            bundle_id = app_config.bundle_id
            logger.debug(
                "ios-feature-01-risk-02[%s]: input ipa=%s sha256=%s bundle_id=%s",
                app_config.id, result.input_ipa, result.input_ipa_sha256, bundle_id,
            )

            logger.debug("ios-feature-01-risk-02[%s]: installing clean baseline %s", app_config.id, acquisition.ipa_path)
            install = device_client.install_app(acquisition.ipa_path, global_config.runner.app_install_timeout_ms)
            result.install_result = install
            installed_by_risk = install.status == "INSTALLED"
            logger.debug("ios-feature-01-risk-02[%s]: baseline install status=%s errors=%s", app_config.id, install.status, install.errors)
            if install.status != "INSTALLED":
                result.final_status = "INSTALL_FAILED"
                result.errors.extend(install.errors)
                return result

            logger.debug("ios-feature-01-risk-02[%s]: exercising clean baseline %s", app_config.id, bundle_id)
            baseline = exercise_and_observe(device_client, bundle_id, cfg, report_dir, "baseline")
            result.launch_result["baseline"] = baseline.to_dict()
            logger.debug(
                "ios-feature-01-risk-02[%s]: baseline drivable=%s foreground_samples=%s terminated_midwindow=%s markers=%s",
                app_config.id, baseline.drivable, baseline.foreground_samples, baseline.terminated_midwindow,
                baseline.tamper_markers_found,
            )
            if not baseline.drivable:
                logger.debug("ios-feature-01-risk-02[%s]: baseline never reached a drivable foreground state", app_config.id)
                result.final_status = "BASELINE_FAILED"
                result.errors.append("The clean baseline never reached a drivable foreground state, so tampering cannot be judged")
                return result

            app_dir = unpack_ipa(acquisition.ipa_path, work_dir / "unpacked")
            frida = cfg.get("frida") or {}
            dylib_src = frida_dylib_path(frida.get("dylib_path"))
            config_src = frida_config_path(frida.get("config_path"))
            insert_dylib_path = str(cfg.get("insert_dylib_path") or "insert_dylib")
            load_path = str(cfg.get("insert_dylib_load_path") or f"@executable_path/Frameworks/{dylib_src.name}")
            logger.debug(
                "ios-feature-01-risk-02[%s]: unpacked to %s; injecting gadget dylib=%s config=%s load_path=%s insert_dylib=%s",
                app_config.id, app_dir, dylib_src, config_src, load_path, insert_dylib_path,
            )
            try:
                executable = add_frida_gadget(app_dir, dylib_src, config_src)
                inject_load_command(executable, load_path, insert_dylib_path)
                logger.debug("ios-feature-01-risk-02[%s]: load command injected into %s", app_config.id, executable)
            except Exception as exc:
                logger.debug("ios-feature-01-risk-02[%s]: dylib injection failed: %s", app_config.id, exc, exc_info=True)
                result.final_status = "DYLIB_INJECTION_FAILED"
                result.errors.append(str(exc))
                return result

            resign_cfg = cfg.get("resign") or {}
            identity = str(resign_cfg.get("identity") or "") or signing_identity_for_team(global_config.device.team_id)
            team_id = global_config.device.team_id
            udid = global_config.device.udid
            repackaged_bundle_id = str(resign_cfg.get("rewrite_bundle_id") or "").strip() or bundle_id
            logger.debug(
                "ios-feature-01-risk-02[%s]: resign identity=%r team_id=%s udid=%s repackaged_bundle_id=%s",
                app_config.id, identity, team_id, udid, repackaged_bundle_id,
            )
            if repackaged_bundle_id != bundle_id:
                logger.debug("ios-feature-01-risk-02[%s]: rewriting bundle id %s -> %s", app_config.id, bundle_id, repackaged_bundle_id)
                try:
                    set_bundle_identifier(app_dir, repackaged_bundle_id)
                except Exception as exc:
                    logger.debug("ios-feature-01-risk-02[%s]: bundle id rewrite failed: %s", app_config.id, exc, exc_info=True)
                    result.final_status = "RESIGN_FAILED"
                    result.errors.append(f"Could not rewrite bundle id to {repackaged_bundle_id}: {exc}")
                    return result
            configured_profile = resign_cfg.get("provisioning_profile")
            if configured_profile:
                profile_path = Path(configured_profile).expanduser().resolve()
                logger.debug("ios-feature-01-risk-02[%s]: using configured provisioning profile %s", app_config.id, profile_path)
            else:
                margin = int(resign_cfg.get("profile_expiry_margin_seconds", 3600))
                auto_provision = bool(resign_cfg.get("auto_provision", True))
                logger.debug(
                    "ios-feature-01-risk-02[%s]: ensuring provisioning profile margin_seconds=%s auto_provision=%s",
                    app_config.id, margin, auto_provision,
                )
                try:
                    profile_path = ensure_provisioning_profile(
                        repackaged_bundle_id, team_id, udid, identity,
                        margin_seconds=margin, generate=auto_provision,
                    )
                    logger.debug("ios-feature-01-risk-02[%s]: provisioning profile resolved to %s", app_config.id, profile_path)
                except Exception as exc:
                    logger.debug("ios-feature-01-risk-02[%s]: provisioning profile generation failed: %s", app_config.id, exc, exc_info=True)
                    result.final_status = "RESIGN_FAILED"
                    result.errors.append(
                        f"Could not auto-generate a provisioning profile for {repackaged_bundle_id}: {exc}"
                    )
                    return result
            if not profile_path:
                logger.debug("ios-feature-01-risk-02[%s]: no valid provisioning profile for %s", app_config.id, repackaged_bundle_id)
                result.final_status = "RESIGN_FAILED"
                result.errors.append(
                    f"No valid provisioning profile for {repackaged_bundle_id}. Set resign.provisioning_profile, "
                    f"or enable resign.auto_provision with an Apple ID for team {team_id} signed into Xcode."
                )
                return result
            resigned = resign_app(
                app_dir,
                identity,
                team_id=team_id,
                profile_path=profile_path,
                bundle_id=repackaged_bundle_id,
                udid=udid,
                work_dir=work_dir,
            )
            result.launch_result["resign"] = {"status": resigned.status, "errors": resigned.errors}
            logger.debug("ios-feature-01-risk-02[%s]: resign status=%s errors=%s", app_config.id, resigned.status, resigned.errors)
            if resigned.status != "RESIGNED":
                result.final_status = "RESIGN_FAILED"
                result.errors.extend(resigned.errors)
                return result

            try:
                repackaged_ipa = repack_ipa(work_dir / "unpacked", work_dir / "repackaged.ipa")
            except Exception as exc:
                logger.debug("ios-feature-01-risk-02[%s]: repack failed: %s", app_config.id, exc, exc_info=True)
                result.final_status = "REPACK_FAILED"
                result.errors.append(str(exc))
                return result
            result.acquired_ipa = repackaged_ipa
            result.acquired_ipa_sha256 = sha256_file(repackaged_ipa)
            logger.debug("ios-feature-01-risk-02[%s]: repackaged ipa=%s sha256=%s", app_config.id, repackaged_ipa, result.acquired_ipa_sha256)

            if device_client.is_installed(repackaged_bundle_id):
                logger.debug("ios-feature-01-risk-02[%s]: removing existing %s before installing repackaged build", app_config.id, repackaged_bundle_id)
                device_client.remove_app(repackaged_bundle_id)
            logger.debug("ios-feature-01-risk-02[%s]: installing repackaged build %s", app_config.id, repackaged_ipa)
            repackaged_install = device_client.install_app(repackaged_ipa, global_config.runner.app_install_timeout_ms)
            result.launch_result["repackaged_install"] = repackaged_install.to_dict()
            logger.debug(
                "ios-feature-01-risk-02[%s]: repackaged install status=%s errors=%s",
                app_config.id, repackaged_install.status, repackaged_install.errors,
            )
            if repackaged_install.status != "INSTALLED":
                result.final_status = "INSTALL_FAILED"
                result.errors.extend(repackaged_install.errors)
                return result

            logger.debug("ios-feature-01-risk-02[%s]: exercising repackaged build %s", app_config.id, repackaged_bundle_id)
            repackaged = exercise_and_observe(device_client, repackaged_bundle_id, cfg, report_dir, "repackaged")
            result.launch_result["repackaged"] = repackaged.to_dict()
            logger.debug(
                "ios-feature-01-risk-02[%s]: repackaged drivable=%s foreground_samples=%s terminated_midwindow=%s markers=%s",
                app_config.id, repackaged.drivable, repackaged.foreground_samples, repackaged.terminated_midwindow,
                repackaged.tamper_markers_found,
            )

            verdict = compare(baseline, repackaged, cfg)
            logger.debug(
                "ios-feature-01-risk-02[%s]: comparison verdict=%s divergences=%s", app_config.id, verdict.verdict, verdict.divergences,
            )
            (report_dir / "baseline_comparison.json").write_text(
                json.dumps(
                    {"baseline": baseline.to_dict(), "repackaged": repackaged.to_dict(), **verdict.to_dict()},
                    indent=2,
                    sort_keys=True,
                )
            )
            result.launch_result["comparison"] = verdict.to_dict()
            logger.debug("ios-feature-01-risk-02[%s]: comparison evidence written to %s", app_config.id, report_dir / "baseline_comparison.json")
            if verdict.verdict == "SURVIVED":
                logger.debug("ios-feature-01-risk-02[%s]: repackaged build survived; confirming Frida gadget attach", app_config.id)
                gadget = confirm_gadget(cfg, repackaged_bundle_id, report_dir)
                result.launch_result["gadget"] = gadget
                logger.debug(
                    "ios-feature-01-risk-02[%s]: gadget ok=%s target=%s messages=%d errors=%s",
                    app_config.id, gadget.get("ok"), gadget.get("target"), len(gadget.get("messages") or []),
                    gadget.get("errors"),
                )
                if gadget.get("ok"):
                    result.final_status = "REPACKAGING_SURVIVED"
                    result.verdict = "At Risk"
                else:
                    result.final_status = "GADGET_ATTACH_FAILED"
                    result.verdict = "Inconclusive"
                    result.errors.extend(gadget.get("errors", [])[:3])
            else:
                result.final_status = "REPACKAGING_BLOCKED"
                result.verdict = "Reduced Risk"
                result.errors.extend(verdict.divergences[:3])
            logger.debug("ios-feature-01-risk-02[%s]: verdict final_status=%s verdict=%s", app_config.id, result.final_status, result.verdict)
            return result
        except Exception as exc:
            logger.debug("ios-feature-01-risk-02[%s]: run failed: %s", app_config.id, exc, exc_info=True)
            result.final_status = "FAILED"
            result.errors.append(str(exc))
            return result
        finally:
            result.cleanup_result = self._cleanup(app_config, global_config, device_client, installed_by_risk, cfg)
            logger.debug(
                "ios-feature-01-risk-02[%s]: cleanup status=%s removed=%s errors=%s",
                app_config.id, getattr(result.cleanup_result, "status", None),
                getattr(result.cleanup_result, "removed", None), getattr(result.cleanup_result, "errors", None),
            )
            if repackaged_bundle_id != app_config.bundle_id and bool(cfg.get("restore_original_after_test", True)):
                logger.debug("ios-feature-01-risk-02[%s]: removing rewritten bundle %s", app_config.id, repackaged_bundle_id)
                try:
                    if device_client.is_installed(repackaged_bundle_id):
                        device_client.remove_app(repackaged_bundle_id)
                except Exception:
                    logger.debug("ios-feature-01-risk-02[%s]: could not remove %s", app_config.id, repackaged_bundle_id, exc_info=True)
            result.timestamp_end = datetime.now(timezone.utc).isoformat()
            logger.debug(
                "ios-feature-01-risk-02[%s]: result final_status=%s verdict=%s errors=%d; writing result to %s",
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
            test_case_id=self.test_case_id,
            test_case_type=self.test_case_type,
            artifact_source=app_config.artifact.get("source", ""),
        )

    # Uninstall the baseline app when this risk installed it and cleanup is enabled.
    def _cleanup(self, app_config, global_config, device_client, installed_by_risk: bool, cfg: dict) -> CleanupResult:
        logger.debug(
            "ios-feature-01-risk-02[%s]: cleanup installed_by_risk=%s uninstall_after_each_test=%s",
            app_config.id, installed_by_risk, getattr(global_config.runner, "uninstall_after_each_test", None),
        )
        if not bool(cfg.get("restore_original_after_test", True)):
            return CleanupResult(status="SKIPPED", metadata={"restore_original_after_test": False})
        if not global_config.runner.uninstall_after_each_test or not installed_by_risk:
            return CleanupResult(status="SKIPPED", metadata={"installed_by_risk": installed_by_risk})
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
            logger.debug("ios-feature-01-risk-02[%s]: cleanup failed: %s", app_config.id, exc, exc_info=True)
            return CleanupResult(status="CLEANUP_FAILED", errors=[str(exc)])

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
