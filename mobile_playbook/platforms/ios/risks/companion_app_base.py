"""
Shared base for risks that install a second, tester-owned companion app alongside the target.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from mobile_playbook.common.logging_setup import redacted
from mobile_playbook.common.storage_paths import ios_work_dir
from mobile_playbook.platforms.ios.keyboard import resign as keyboard_resign
from mobile_playbook.platforms.ios.acquisition.registry import get_provider
from mobile_playbook.platforms.ios.models import ArtifactAcquisitionResult, CleanupResult
from mobile_playbook.platforms.ios.risks.base import Risk

logger = logging.getLogger(__name__)


class CompanionAppRiskBase(Risk):
    """Target-app and companion-app helpers shared by risks that install a second, tester-owned app."""

    companion_config_key = "companion_app"
    companion_label = "companion"

    # Install the companion IPA, resigning on expiry or verification failure, or verify it is installed.
    def _install_or_verify_companion_app(self, cfg: dict, global_config, device_client) -> dict:
        bundle_id = cfg.get("bundle_id")
        ipa = cfg.get("ipa")
        logger.debug(
            "%s: %s app setup bundle_id=%s ipa=%s install=%s",
            getattr(self, "risk_id", type(self).__name__),
            self.companion_label,
            bundle_id,
            ipa,
            bool(cfg.get("install", True)),
        )
        if not bundle_id and not ipa:
            logger.debug("%s: %s app has neither bundle_id nor ipa configured", getattr(self, "risk_id", type(self).__name__), self.companion_label)
            return {"status": "ARTIFACT_REQUIRED", "errors": [f"{self.companion_config_key}.bundle_id or ipa is required"]}
        if ipa and bool(cfg.get("install", True)):
            ipa_path = Path(ipa).expanduser()
            resign_config = cfg.get("resign") or {}
            auto_resign = bool(resign_config.get("enabled", False))
            timeout_ms = global_config.runner.app_install_timeout_ms
            attempts: list[dict] = []
            logger.debug("%s: installing %s app from %s auto_resign=%s timeout_ms=%s", getattr(self, "risk_id", type(self).__name__), self.companion_label, ipa_path, auto_resign, timeout_ms)
            if auto_resign and keyboard_resign.signature_expired(ipa_path):
                logger.debug("%s: %s ipa signature expired; re-signing before install", getattr(self, "risk_id", type(self).__name__), self.companion_label)
                attempts.append(self._resign_companion_ipa(ipa_path, global_config, resign_config, "PROFILE_EXPIRED"))
            install = device_client.install_app(ipa_path, timeout_ms)
            logger.debug("%s: %s app install status=%s errors=%s", getattr(self, "risk_id", type(self).__name__), self.companion_label, install.status, install.errors)
            if (
                auto_resign
                and not attempts
                and install.status != "INSTALLED"
                and keyboard_resign.is_verification_failure(install.errors)
            ):
                logger.debug("%s: %s install rejected by verification; re-signing and retrying", getattr(self, "risk_id", type(self).__name__), self.companion_label)
                attempts.append(self._resign_companion_ipa(ipa_path, global_config, resign_config, "INSTALL_REJECTED"))
                if attempts[-1]["status"] == "RESIGNED":
                    install = device_client.install_app(ipa_path, timeout_ms)
                    logger.debug("%s: %s app reinstall status=%s errors=%s", getattr(self, "risk_id", type(self).__name__), self.companion_label, install.status, install.errors)
            logger.debug("%s: %s app setup finished status=%s resign_attempts=%s", getattr(self, "risk_id", type(self).__name__), self.companion_label, install.status, len(attempts))
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
                logger.debug("%s: is_installed(%s) raised: %s", getattr(self, "risk_id", type(self).__name__), bundle_id, exc, exc_info=True)
                return {"status": "FAILED", "bundle_id": bundle_id, "errors": [str(exc)]}
            logger.debug("%s: %s app %s installed=%s", getattr(self, "risk_id", type(self).__name__), self.companion_label, bundle_id, installed)
            return {
                "status": "INSTALLED_APP_VERIFIED" if installed else "ARTIFACT_REQUIRED",
                "bundle_id": bundle_id,
                "installed_by_risk": False,
                "errors": [] if installed else [f"{self.companion_label.capitalize()} app is not installed: {bundle_id}"],
            }
        logger.debug("%s: %s app install disabled and no bundle_id; skipping", getattr(self, "risk_id", type(self).__name__), self.companion_label)
        return {"status": "SKIPPED", "installed_by_risk": False}

    # Resign the companion IPA for the device and team, returning the attempt record.
    def _resign_companion_ipa(self, ipa_path: Path, global_config, resign_config: dict, trigger: str) -> dict:
        logger.debug(
            "%s: re-signing %s (trigger=%s, udid=%s, team_id=%s, timeout=%ss)",
            getattr(self, "risk_id", type(self).__name__),
            ipa_path,
            trigger,
            global_config.device.udid,
            global_config.device.team_id,
            resign_config.get("timeout_seconds", 900),
        )
        started = time.monotonic()
        result = keyboard_resign.resign(
            ipa_path,
            global_config.device.udid,
            global_config.device.team_id,
            timeout_seconds=int(resign_config.get("timeout_seconds", 900)),
        )
        logger.debug("%s: re-sign finished status=%s errors=%s in %.2fs", getattr(self, "risk_id", type(self).__name__), result.status, result.errors, time.monotonic() - started)
        return {"trigger": trigger, "status": result.status, "errors": result.errors}

    # Focus the configured text field, tapping through navigation elements when auto_navigation allows.
    def _focus_text_field_with_navigation(self, device_client, report_dir: Path, control: dict, global_config) -> dict:
        navigation = []
        selector = control.get("text_field")
        logger.debug("%s: focusing text field selector=%s", getattr(self, "risk_id", type(self).__name__), selector)
        initial_alerts = self._handle_permission_alerts(device_client, global_config)
        if self._has_handled_alert(initial_alerts):
            navigation.append({"step": 0, "type": "permission_alerts", "alerts": initial_alerts})
        try:
            return {"focus": device_client.tap_text_field(selector), "navigation": navigation}
        except Exception as first_error:
            logger.debug("%s: initial text field focus failed: %s", getattr(self, "risk_id", type(self).__name__), first_error, exc_info=True)
            self._append_text_field_diagnostics(device_client, navigation, 0, first_error)
            auto_nav = control.get("auto_navigation") or {}
            if not bool(auto_nav.get("enabled", False)):
                logger.debug("%s: auto_navigation disabled; giving up on text field focus", getattr(self, "risk_id", type(self).__name__))
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
            logger.debug(
                "%s: auto_navigation max_steps=%s settle=%ss accessibility_ids=%s allow_any=%s class_names=%s",
                getattr(self, "risk_id", type(self).__name__),
                max_steps,
                settle_seconds,
                accessibility_ids,
                allow_any,
                element_class_names,
            )
            for step in range(max_steps):
                logger.debug("%s: auto_navigation step %s/%s", getattr(self, "risk_id", type(self).__name__), step + 1, max_steps)
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
                    logger.debug("%s: auto_navigation step %s tap failed: %s", getattr(self, "risk_id", type(self).__name__), step + 1, exc, exc_info=True)
                    navigation.append({"step": step + 1, "type": "button_tap_failed", "error": str(exc)})
                    self._capture_target_debug(device_client, report_dir, suffix=f"-navigation-step-{step + 1}")
                    raise last_error
                tapped["step"] = step + 1
                tapped["type"] = "button_tap"
                logger.debug("%s: auto_navigation step %s tapped matched_by=%s label=%s", getattr(self, "risk_id", type(self).__name__), step + 1, tapped.get("matched_by"), tapped.get("label"))
                navigation.append(tapped)
                time.sleep(settle_seconds)
                after_alerts = self._handle_permission_alerts(device_client, global_config)
                if self._has_handled_alert(after_alerts):
                    navigation.append({"step": step + 1, "type": "permission_alerts_after_tap", "alerts": after_alerts})
                try:
                    focus = device_client.tap_text_field(selector)
                    logger.debug("%s: text field focused after auto_navigation step %s", getattr(self, "risk_id", type(self).__name__), step + 1)
                    return {"focus": focus, "navigation": navigation}
                except Exception as exc:
                    logger.debug("%s: text field focus after step %s failed: %s", getattr(self, "risk_id", type(self).__name__), step + 1, exc, exc_info=True)
                    last_error = exc
                    self._append_text_field_diagnostics(device_client, navigation, step + 1, exc)
                    self._capture_target_debug(device_client, report_dir, suffix=f"-navigation-step-{step + 1}")
            logger.debug("%s: text field not focused after %s auto_navigation step(s)", getattr(self, "risk_id", type(self).__name__), max_steps)
            raise last_error

    # Tap the first configured accessibility ID, else the first element whose label matches.
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
            logger.debug("%s: navigation tap accessibility_id=%s", getattr(self, "risk_id", type(self).__name__), accessibility_id)
            try:
                tapped = device_client.tap_by_accessibility_id(accessibility_id)
                tapped["matched_by"] = "accessibility_id"
                tapped["accessibility_id"] = accessibility_id
                return tapped
            except Exception as exc:
                logger.debug("%s: navigation accessibility_id=%s tap failed: %s", getattr(self, "risk_id", type(self).__name__), accessibility_id, exc, exc_info=True)
                id_errors.append({"accessibility_id": accessibility_id, "error": str(exc)})
        tapper = getattr(device_client, "tap_first_element_matching", None)
        logger.debug(
            "%s: navigation label match via %s include=%s exclude=%s allow_any=%s",
            getattr(self, "risk_id", type(self).__name__),
            "tap_first_element_matching" if tapper else "tap_first_button_matching",
            label_contains,
            exclude_label_contains,
            allow_any,
        )
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

    # Record a focus failure, with text-field candidates when the client can describe them.
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
                logger.debug("%s: describe_text_field_candidates raised: %s", getattr(self, "risk_id", type(self).__name__), exc, exc_info=True)
                diagnostic["text_field_candidates_error"] = str(exc)
        logger.debug("%s: text field diagnostics step=%s error=%s candidates_captured=%s", getattr(self, "risk_id", type(self).__name__), step, error, "text_field_candidates" in diagnostic)
        navigation.append(diagnostic)

    # Return whether any permission alert was handled or present.
    def _has_handled_alert(self, alerts: list[dict]) -> bool:
        return any(alert.get("status") in {"HANDLED", "ALERT_PRESENT"} for alert in alerts)

    # Save the page source, text-field candidates and a screenshot as debug evidence.
    def _capture_target_debug(self, device_client, report_dir: Path, suffix: str = "") -> None:
        logger.debug("%s: capturing target debug evidence into %s suffix=%s", getattr(self, "risk_id", type(self).__name__), report_dir, suffix)
        try:
            source = device_client.page_source()
            (report_dir / f"target_page_source{suffix}.xml").write_text(source)
            logger.debug("%s: wrote %s (%s chars)", getattr(self, "risk_id", type(self).__name__), report_dir / f"target_page_source{suffix}.xml", len(source))
        except Exception as exc:
            logger.debug("%s: target page source capture failed: %s", getattr(self, "risk_id", type(self).__name__), exc, exc_info=True)
        describer = getattr(device_client, "describe_text_field_candidates", None)
        if describer:
            try:
                candidates = describer()
                (report_dir / f"target_text_field_candidates{suffix}.json").write_text(
                    json.dumps(candidates, indent=2, sort_keys=True)
                )
                logger.debug("%s: wrote %s", getattr(self, "risk_id", type(self).__name__), report_dir / f"target_text_field_candidates{suffix}.json")
            except Exception as exc:
                logger.debug("%s: text field candidates capture failed: %s", getattr(self, "risk_id", type(self).__name__), exc, exc_info=True)
        try:
            device_client.screenshot(report_dir / f"target_screen{suffix}.png")
            logger.debug("%s: wrote %s", getattr(self, "risk_id", type(self).__name__), report_dir / f"target_screen{suffix}.png")
        except Exception as exc:
            logger.debug("%s: target screenshot capture failed: %s", getattr(self, "risk_id", type(self).__name__), exc, exc_info=True)

    # Acquire the target app through its configured artifact provider.
    def _prepare_app(self, app_config, global_config, device_client, run_timestamp: str) -> ArtifactAcquisitionResult:
        provider = get_provider(app_config.artifact.get("source", ""))
        logger.debug("%s: preparing target app %s from source=%s provider=%s", getattr(self, "risk_id", type(self).__name__), app_config.id, app_config.artifact.get("source", ""), type(provider).__name__ if provider is not None else None)
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

    # Handle permission alerts with the runner config plus overrides, capturing failures as results.
    def _handle_permission_alerts(self, device_client, global_config, overrides: dict | None = None) -> list[dict]:
        handler = getattr(device_client, "handle_permission_alerts", None)
        if not handler:
            logger.debug("%s: device client has no permission alert handler", getattr(self, "risk_id", type(self).__name__))
            return [{"status": "UNSUPPORTED", "reason": "device client does not support permission alert handling"}]
        config = dict(global_config.runner.permission_alerts or {})
        if overrides:
            config.update(overrides)
        logger.debug("%s: handling permission alerts config=%s", getattr(self, "risk_id", type(self).__name__), redacted(config))
        try:
            alerts = handler(config)
        except Exception as exc:
            logger.debug("%s: permission alert handling raised: %s", getattr(self, "risk_id", type(self).__name__), exc, exc_info=True)
            return [{"status": "FAILED", "error": str(exc)}]
        logger.debug("%s: permission alerts result statuses=%s", getattr(self, "risk_id", type(self).__name__), [alert.get("status") for alert in alerts if isinstance(alert, dict)] if isinstance(alerts, list) else alerts)
        return alerts

    # Uninstall the target and companion apps as configured, verifying each removal.
    def _cleanup(
        self,
        app_config,
        global_config,
        device_client,
        installed_target_by_risk: bool,
        companion_config: dict,
        installed_companion_by_risk: bool,
    ) -> CleanupResult:
        logger.debug(
            "%s: cleanup uninstall_after_each_test=%s installed_target_by_risk=%s installed_%s_by_risk=%s",
            getattr(self, "risk_id", type(self).__name__),
            global_config.runner.uninstall_after_each_test,
            installed_target_by_risk,
            self.companion_label,
            installed_companion_by_risk,
        )
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

        # Remove an installed bundle and record whether its removal was verified.
        def _remove(label: str, bundle_id: str) -> None:
            try:
                if not device_client.is_installed(bundle_id):
                    logger.debug("%s: cleanup %s %s not installed; nothing to remove", getattr(self, "risk_id", type(self).__name__), label, bundle_id)
                    verification[bundle_id] = {"requested": False, "verified": True}
                    return
                logger.debug("%s: cleanup removing %s %s", getattr(self, "risk_id", type(self).__name__), label, bundle_id)
                outcome = device_client.remove_app_verified(bundle_id)
                verification[bundle_id] = outcome
                logger.debug("%s: cleanup %s %s removal outcome=%s", getattr(self, "risk_id", type(self).__name__), label, bundle_id, outcome)
                if outcome["verified"]:
                    removed.append(bundle_id)
                else:
                    errors.append(f"{label} {bundle_id} still present after removal")
            except Exception as exc:
                logger.debug("%s: cleanup %s %s raised: %s", getattr(self, "risk_id", type(self).__name__), label, bundle_id, exc, exc_info=True)
                errors.append(f"{label} {bundle_id} cleanup raised: {exc}")

        if installed_target_by_risk:
            _remove("target app", app_config.bundle_id)

        companion_bundle_id = companion_config.get("bundle_id")
        # Not gated on installed_companion_by_risk: a crashed run's leftover must not skew the next run.
        if companion_bundle_id and bool(companion_config.get("uninstall_after_test", False)):
            _remove(f"{self.companion_label} app", companion_bundle_id)

        logger.debug("%s: cleanup finished removed=%s errors=%s", getattr(self, "risk_id", type(self).__name__), removed, errors)
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
        final_status = mapping.get(status, "ARTIFACT_ACQUISITION_FAILED")
        logger.debug("%s: artifact status %s maps to final status %s", getattr(self, "risk_id", type(self).__name__), status, final_status)
        return final_status
