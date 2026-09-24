from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from mobile_playbook.storage import ios_work_dir
from mobile_playbook.platforms.ios import keyboard_resign
from mobile_playbook.platforms.ios.artifacts.registry import get_provider
from mobile_playbook.platforms.ios.models import ArtifactAcquisitionResult, CleanupResult
from mobile_playbook.platforms.ios.risks.base import Risk


class CompanionAppRiskBase(Risk):
    """Target-app and companion-app helpers shared by risks that install a second, tester-owned app."""

    #: Config key and report label of the companion app, e.g. "keyboard_app" / "keyboard".
    companion_config_key = "companion_app"
    companion_label = "companion"

    def _install_or_verify_companion_app(self, cfg: dict, global_config, device_client) -> dict:
        bundle_id = cfg.get("bundle_id")
        ipa = cfg.get("ipa")
        if not bundle_id and not ipa:
            return {"status": "ARTIFACT_REQUIRED", "errors": [f"{self.companion_config_key}.bundle_id or ipa is required"]}
        if ipa and bool(cfg.get("install", True)):
            ipa_path = Path(ipa).expanduser()
            resign_config = cfg.get("resign") or {}
            auto_resign = bool(resign_config.get("enabled", False))
            timeout_ms = global_config.runner.app_install_timeout_ms
            attempts: list[dict] = []
            if auto_resign and keyboard_resign.signature_expired(ipa_path):
                attempts.append(self._resign_companion_ipa(ipa_path, global_config, resign_config, "PROFILE_EXPIRED"))
            install = device_client.install_app(ipa_path, timeout_ms)
            if (
                auto_resign
                and not attempts
                and install.status != "INSTALLED"
                and keyboard_resign.is_verification_failure(install.errors)
            ):
                attempts.append(self._resign_companion_ipa(ipa_path, global_config, resign_config, "INSTALL_REJECTED"))
                if attempts[-1]["status"] == "RESIGNED":
                    install = device_client.install_app(ipa_path, timeout_ms)
            return {
                "status": install.status,
                "ipa_path": str(install.ipa_path) if install.ipa_path else None,
                "bundle_id": bundle_id,
                "installed_by_risk": install.status == "INSTALLED",
                "resign_attempts": attempts,
                "errors": install.errors,
            }
        if bundle_id:
            try:
                installed = device_client.is_installed(bundle_id)
            except Exception as exc:
                return {"status": "FAILED", "bundle_id": bundle_id, "errors": [str(exc)]}
            return {
                "status": "INSTALLED_APP_VERIFIED" if installed else "ARTIFACT_REQUIRED",
                "bundle_id": bundle_id,
                "installed_by_risk": False,
                "errors": [] if installed else [f"{self.companion_label.capitalize()} app is not installed: {bundle_id}"],
            }
        return {"status": "SKIPPED", "installed_by_risk": False}

    def _resign_companion_ipa(self, ipa_path: Path, global_config, resign_config: dict, trigger: str) -> dict:
        result = keyboard_resign.resign(
            ipa_path,
            global_config.device.udid,
            global_config.device.team_id,
            timeout_seconds=int(resign_config.get("timeout_seconds", 900)),
        )
        return {"trigger": trigger, "status": result.status, "errors": result.errors}

    def _focus_text_field_with_navigation(self, device_client, report_dir: Path, control: dict, global_config) -> dict:
        navigation = []
        selector = control.get("text_field")
        initial_alerts = self._handle_permission_alerts(device_client, global_config)
        if self._has_handled_alert(initial_alerts):
            navigation.append({"step": 0, "type": "permission_alerts", "alerts": initial_alerts})
        try:
            return {"focus": device_client.tap_text_field(selector), "navigation": navigation}
        except Exception as first_error:
            self._append_text_field_diagnostics(device_client, navigation, 0, first_error)
            auto_nav = control.get("auto_navigation") or {}
            if not bool(auto_nav.get("enabled", False)):
                raise first_error
            max_steps = int(auto_nav.get("max_steps", 3))
            settle_seconds = float(auto_nav.get("settle_seconds", 1))
            accessibility_ids = [value for value in (auto_nav.get("accessibility_ids") or []) if value]
            label_contains = auto_nav.get("button_label_contains") or [
                "log in",
                "login",
                "use password",
                "log in with password",
                "login with password",
                "sign in with password",
                "sign in",
                "continue",
                "next",
                "get started",
                "start",
                "search",
                "select car park",
                "enter vehicle details",
            ]
            exclude_label_contains = auto_nav.get("exclude_button_label_contains") or [
                "delete",
                "remove",
                "cancel",
                "logout",
                "log out",
                "sign out",
                "forgot",
                "pay",
                "purchase",
            ]
            allow_any = bool(auto_nav.get("allow_any_button", False))
            element_class_names = auto_nav.get("element_class_names") or [
                "XCUIElementTypeButton",
                "XCUIElementTypeOther",
                "XCUIElementTypeCell",
            ]
            last_error = first_error
            for step in range(max_steps):
                before_alerts = self._handle_permission_alerts(device_client, global_config)
                if self._has_handled_alert(before_alerts):
                    navigation.append({"step": step + 1, "type": "permission_alerts_before_tap", "alerts": before_alerts})
                try:
                    tapped = self._tap_navigation_element(
                        device_client,
                        accessibility_ids,
                        label_contains,
                        exclude_label_contains,
                        allow_any,
                        element_class_names,
                    )
                except Exception as exc:
                    navigation.append({"step": step + 1, "type": "button_tap_failed", "error": str(exc)})
                    self._capture_target_debug(device_client, report_dir, suffix=f"-navigation-step-{step + 1}")
                    raise last_error
                tapped["step"] = step + 1
                tapped["type"] = "button_tap"
                navigation.append(tapped)
                time.sleep(settle_seconds)
                after_alerts = self._handle_permission_alerts(device_client, global_config)
                if self._has_handled_alert(after_alerts):
                    navigation.append({"step": step + 1, "type": "permission_alerts_after_tap", "alerts": after_alerts})
                try:
                    return {"focus": device_client.tap_text_field(selector), "navigation": navigation}
                except Exception as exc:
                    last_error = exc
                    self._append_text_field_diagnostics(device_client, navigation, step + 1, exc)
                    self._capture_target_debug(device_client, report_dir, suffix=f"-navigation-step-{step + 1}")
            raise last_error

    def _tap_navigation_element(
        self,
        device_client,
        accessibility_ids: list[str],
        label_contains: list[str],
        exclude_label_contains: list[str],
        allow_any: bool,
        element_class_names: list[str],
    ) -> dict:
        id_errors = []
        for accessibility_id in accessibility_ids:
            try:
                tapped = device_client.tap_by_accessibility_id(accessibility_id)
                tapped["matched_by"] = "accessibility_id"
                tapped["accessibility_id"] = accessibility_id
                return tapped
            except Exception as exc:
                id_errors.append({"accessibility_id": accessibility_id, "error": str(exc)})
        tapper = getattr(device_client, "tap_first_element_matching", None)
        if tapper:
            tapped = tapper(
                label_contains,
                exclude_label_contains,
                allow_any=allow_any,
                class_names=element_class_names,
                element_label="navigation element",
            )
        else:
            tapped = device_client.tap_first_button_matching(
                label_contains,
                exclude_label_contains,
                allow_any=allow_any,
            )
        if id_errors:
            tapped["accessibility_id_attempts"] = id_errors
        return tapped

    def _append_text_field_diagnostics(self, device_client, navigation: list[dict], step: int, error: Exception) -> None:
        diagnostic = {
            "step": step,
            "type": "text_field_focus_failed",
            "error": str(error),
        }
        describer = getattr(device_client, "describe_text_field_candidates", None)
        if describer:
            try:
                diagnostic["text_field_candidates"] = describer()
            except Exception as exc:
                diagnostic["text_field_candidates_error"] = str(exc)
        navigation.append(diagnostic)

    def _has_handled_alert(self, alerts: list[dict]) -> bool:
        return any(alert.get("status") in {"HANDLED", "ALERT_PRESENT"} for alert in alerts)

    def _capture_target_debug(self, device_client, report_dir: Path, suffix: str = "") -> None:
        try:
            source = device_client.page_source()
            (report_dir / f"target_page_source{suffix}.xml").write_text(source)
        except Exception:
            pass
        describer = getattr(device_client, "describe_text_field_candidates", None)
        if describer:
            try:
                candidates = describer()
                (report_dir / f"target_text_field_candidates{suffix}.json").write_text(
                    json.dumps(candidates, indent=2, sort_keys=True)
                )
            except Exception:
                pass
        try:
            device_client.screenshot(report_dir / f"target_screen{suffix}.png")
        except Exception:
            pass

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
            Path(app_config.artifact.get("workspace_dir") or ios_work_dir() / "acquired"),
        )

    def _handle_permission_alerts(self, device_client, global_config, overrides: dict | None = None) -> list[dict]:
        handler = getattr(device_client, "handle_permission_alerts", None)
        if not handler:
            return [{"status": "UNSUPPORTED", "reason": "device client does not support permission alert handling"}]
        config = dict(global_config.runner.permission_alerts or {})
        if overrides:
            config.update(overrides)
        try:
            return handler(config)
        except Exception as exc:
            return [{"status": "FAILED", "error": str(exc)}]

    def _cleanup(
        self,
        app_config,
        global_config,
        device_client,
        installed_target_by_risk: bool,
        companion_config: dict,
        installed_companion_by_risk: bool,
    ) -> CleanupResult:
        if not global_config.runner.uninstall_after_each_test:
            return CleanupResult(
                status="SKIPPED",
                metadata={
                    "installed_target_by_risk": installed_target_by_risk,
                    f"installed_{self.companion_label}_by_risk": installed_companion_by_risk,
                },
            )
        removed: list[str] = []
        errors: list[str] = []
        verification: dict[str, Any] = {}

        def _remove(label: str, bundle_id: str) -> None:
            try:
                if not device_client.is_installed(bundle_id):
                    verification[bundle_id] = {"requested": False, "verified": True}
                    return
                outcome = device_client.remove_app_verified(bundle_id)
                verification[bundle_id] = outcome
                if outcome["verified"]:
                    removed.append(bundle_id)
                else:
                    errors.append(f"{label} {bundle_id} still present after removal")
            except Exception as exc:
                errors.append(f"{label} {bundle_id} cleanup raised: {exc}")

        if installed_target_by_risk:
            _remove("target app", app_config.bundle_id)

        companion_bundle_id = companion_config.get("bundle_id")
        # Not gated on installed_companion_by_risk: a leftover from a crashed run
        # must be removed, or the next run branches on stale device state.
        if companion_bundle_id and bool(companion_config.get("uninstall_after_test", False)):
            _remove(f"{self.companion_label} app", companion_bundle_id)

        return CleanupResult(
            status="CLEANUP_FAILED" if errors else "CLEANED",
            removed=bool(removed),
            errors=errors,
            metadata={
                "removed_bundle_ids": removed,
                "verification": verification,
                "installed_target_by_risk": installed_target_by_risk,
                f"installed_{self.companion_label}_by_risk": installed_companion_by_risk,
            },
        )

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
