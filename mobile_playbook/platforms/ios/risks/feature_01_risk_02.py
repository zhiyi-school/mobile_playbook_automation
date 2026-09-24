from __future__ import annotations

import json
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
from mobile_playbook.storage import ios_work_dir


class Feature01Risk02(Risk):
    risk_id = "ios-feature-01-risk-02"
    feature_id = "feature-01"
    name = "IPA repackaging resistance"
    requires_ipa_artifact = True
    requires_device = True
    test_case_id = "repackaging"
    test_case_type = "ipa_repackage_resign_validate"

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
        try:
            source = app_config.artifact.get("source", "")
            if source == "installed_app_reference":
                result.final_status = "ARTIFACT_REQUIRED"
                result.errors.append(f"{self.risk_id} requires a supplied IPA artifact; installed_app_reference cannot be repackaged")
                return result
            provider = get_provider(source)
            if provider is None:
                result.final_status = "UNSUPPORTED_ARTIFACT_SOURCE"
                result.errors.append(f"Unsupported artifact source: {source}")
                return result

            acquisition = provider.acquire(
                app_config,
                global_config,
                device_client,
                report_writer.run_timestamp,
                Path(app_config.artifact.get("workspace_dir") or ios_work_dir() / "acquired"),
            )
            result.artifact_result = acquisition
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

            install = device_client.install_app(acquisition.ipa_path, global_config.runner.app_install_timeout_ms)
            result.install_result = install
            installed_by_risk = install.status == "INSTALLED"
            if install.status != "INSTALLED":
                result.final_status = "INSTALL_FAILED"
                result.errors.extend(install.errors)
                return result

            baseline = exercise_and_observe(device_client, bundle_id, cfg, report_dir, "baseline")
            result.launch_result["baseline"] = baseline.to_dict()
            if not baseline.drivable:
                result.final_status = "BASELINE_FAILED"
                result.errors.append("The clean baseline never reached a drivable foreground state, so tampering cannot be judged")
                return result

            app_dir = unpack_ipa(acquisition.ipa_path, work_dir / "unpacked")
            frida = cfg.get("frida") or {}
            dylib_src = frida_dylib_path(frida.get("dylib_path"))
            config_src = frida_config_path(frida.get("config_path"))
            insert_dylib_path = str(cfg.get("insert_dylib_path") or "insert_dylib")
            load_path = str(cfg.get("insert_dylib_load_path") or f"@executable_path/Frameworks/{dylib_src.name}")
            try:
                executable = add_frida_gadget(app_dir, dylib_src, config_src)
                inject_load_command(executable, load_path, insert_dylib_path)
            except Exception as exc:
                result.final_status = "DYLIB_INJECTION_FAILED"
                result.errors.append(str(exc))
                return result

            resign_cfg = cfg.get("resign") or {}
            identity = str(resign_cfg.get("identity") or "") or signing_identity_for_team(global_config.device.team_id)
            team_id = global_config.device.team_id
            udid = global_config.device.udid
            repackaged_bundle_id = str(resign_cfg.get("rewrite_bundle_id") or "").strip() or bundle_id
            if repackaged_bundle_id != bundle_id:
                try:
                    set_bundle_identifier(app_dir, repackaged_bundle_id)
                except Exception as exc:
                    result.final_status = "RESIGN_FAILED"
                    result.errors.append(f"Could not rewrite bundle id to {repackaged_bundle_id}: {exc}")
                    return result
            configured_profile = resign_cfg.get("provisioning_profile")
            if configured_profile:
                profile_path = Path(configured_profile).expanduser().resolve()
            else:
                margin = int(resign_cfg.get("profile_expiry_margin_seconds", 3600))
                auto_provision = bool(resign_cfg.get("auto_provision", True))
                try:
                    profile_path = ensure_provisioning_profile(
                        repackaged_bundle_id, team_id, udid, identity,
                        margin_seconds=margin, generate=auto_provision,
                    )
                except Exception as exc:
                    result.final_status = "RESIGN_FAILED"
                    result.errors.append(
                        f"Could not auto-generate a provisioning profile for {repackaged_bundle_id}: {exc}"
                    )
                    return result
            if not profile_path:
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
            if resigned.status != "RESIGNED":
                result.final_status = "RESIGN_FAILED"
                result.errors.extend(resigned.errors)
                return result

            try:
                repackaged_ipa = repack_ipa(work_dir / "unpacked", work_dir / "repackaged.ipa")
            except Exception as exc:
                result.final_status = "REPACK_FAILED"
                result.errors.append(str(exc))
                return result
            result.acquired_ipa = repackaged_ipa
            result.acquired_ipa_sha256 = sha256_file(repackaged_ipa)

            if device_client.is_installed(repackaged_bundle_id):
                device_client.remove_app(repackaged_bundle_id)
            repackaged_install = device_client.install_app(repackaged_ipa, global_config.runner.app_install_timeout_ms)
            result.launch_result["repackaged_install"] = repackaged_install.to_dict()
            if repackaged_install.status != "INSTALLED":
                result.final_status = "INSTALL_FAILED"
                result.errors.extend(repackaged_install.errors)
                return result

            repackaged = exercise_and_observe(device_client, repackaged_bundle_id, cfg, report_dir, "repackaged")
            result.launch_result["repackaged"] = repackaged.to_dict()

            verdict = compare(baseline, repackaged, cfg)
            (report_dir / "baseline_comparison.json").write_text(
                json.dumps(
                    {"baseline": baseline.to_dict(), "repackaged": repackaged.to_dict(), **verdict.to_dict()},
                    indent=2,
                    sort_keys=True,
                )
            )
            result.launch_result["comparison"] = verdict.to_dict()
            if verdict.verdict == "SURVIVED":
                gadget = confirm_gadget(cfg, repackaged_bundle_id, report_dir)
                result.launch_result["gadget"] = gadget
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
            return result
        except Exception as exc:
            result.final_status = "FAILED"
            result.errors.append(str(exc))
            return result
        finally:
            result.cleanup_result = self._cleanup(app_config, global_config, device_client, installed_by_risk, cfg)
            if repackaged_bundle_id != app_config.bundle_id and bool(cfg.get("restore_original_after_test", True)):
                try:
                    if device_client.is_installed(repackaged_bundle_id):
                        device_client.remove_app(repackaged_bundle_id)
                except Exception:
                    pass
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
            test_case_id=self.test_case_id,
            test_case_type=self.test_case_type,
            artifact_source=app_config.artifact.get("source", ""),
        )

    def _cleanup(self, app_config, global_config, device_client, installed_by_risk: bool, cfg: dict) -> CleanupResult:
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
            return CleanupResult(status="CLEANUP_FAILED", errors=[str(exc)])

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
