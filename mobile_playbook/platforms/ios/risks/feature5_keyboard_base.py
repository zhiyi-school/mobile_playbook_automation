from __future__ import annotations

import json
import time
from pathlib import Path
from urllib.parse import urlparse

from mobile_playbook.platforms.ios.artifacts.registry import get_provider
from mobile_playbook.platforms.ios.control_server import CommandControlServer
from mobile_playbook.platforms.ios.models import ArtifactAcquisitionResult, CleanupResult, RiskRunResult
from mobile_playbook.platforms.ios.risks.base import Risk


class Feature5KeyboardRiskBase(Risk):
    """Shared custom-keyboard workflow helpers for iOS feature5 risks."""

    def __init__(self, server_factory=CommandControlServer):
        self.server_factory = server_factory

    def _start_server(self, control: dict) -> CommandControlServer:
        server = self.server_factory(
            host=control.get("bind_host", "0.0.0.0"),
            port=int(control.get("port", 8765)),
            token=control.get("token"),
            enqueue_requires_token=bool(control.get("enqueue_requires_token", False)),
        )
        return server.start()

    def _device_reachable_base_url(self, base_url: str, control: dict) -> str:
        advertised = control.get("advertised_host")
        if not advertised or str(advertised).startswith("REPLACE_WITH"):
            return base_url
        parsed = urlparse(base_url)
        return f"{parsed.scheme}://{advertised}:{parsed.port}"

    def _install_or_verify_keyboard_app(self, keyboard_config: dict, global_config, device_client) -> dict:
        bundle_id = keyboard_config.get("bundle_id")
        ipa = keyboard_config.get("ipa")
        if not bundle_id and not ipa:
            return {"status": "ARTIFACT_REQUIRED", "errors": ["keyboard_app.bundle_id or ipa is required"]}
        if ipa and bool(keyboard_config.get("install", True)):
            install = device_client.install_app(Path(ipa).expanduser(), global_config.runner.app_install_timeout_ms)
            return {
                "status": install.status,
                "ipa_path": str(install.ipa_path) if install.ipa_path else None,
                "bundle_id": bundle_id,
                "installed_by_risk": install.status == "INSTALLED",
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
                "errors": [] if installed else [f"Keyboard app is not installed: {bundle_id}"],
            }
        return {"status": "SKIPPED", "installed_by_risk": False}

    def _configure_keyboard_server_url(self, device_client, keyboard_config: dict, device_reachable_base_url: str) -> dict | None:
        server_setup = keyboard_config.get("server_setup") or {}
        field_id = server_setup.get("server_url_input_accessibility_id")
        if not field_id:
            return None
        value = server_setup.get("value") or device_reachable_base_url
        clear_first = bool(server_setup.get("clear_first", True))
        result = {
            "server_url": value,
            "field": device_client.set_text_by_accessibility_id(field_id, value, clear_first=clear_first),
        }
        save_button_id = server_setup.get("save_button_accessibility_id")
        if save_button_id:
            result["save_button"] = device_client.tap_by_accessibility_id(save_button_id)
        return result

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

    def _select_custom_keyboard(self, device_client, control: dict, keyboard_config: dict) -> dict:
        selection_config = dict(control.get("keyboard_selection") or {})
        if "enabled" not in selection_config:
            selection_config["enabled"] = True
        expected = list(selection_config.get("expected_source_contains") or [])
        for value in (
            keyboard_config.get("keyboard_extension_bundle_id"),
            keyboard_config.get("bundle_id"),
            keyboard_config.get("name"),
        ):
            if value and value not in expected:
                expected.append(value)
        if expected:
            selection_config["expected_source_contains"] = expected
        selector = getattr(device_client, "ensure_keyboard_selected", None)
        if not selector:
            return {"status": "UNSUPPORTED", "reason": "device client does not support keyboard selection"}
        try:
            return selector(selection_config)
        except Exception as exc:
            return {"status": "FAILED", "error": str(exc)}

    def _focused_field_custom_keyboard_blocker(self, focus_result: dict) -> str | None:
        element_type = str(focus_result.get("element_type") or "")
        if element_type == "XCUIElementTypeSecureTextField":
            return (
                "A text field was found and focused, but it is a secure text field. "
                "iOS does not allow third-party custom keyboards in secure text fields, so LocalKeyboard cannot be used there."
            )
        keyboard_type = str((focus_result.get("element") or {}).get("keyboard_type") or "")
        if keyboard_type and keyboard_type.lower() in {"phonepad", "numberpad", "decimalpad"}:
            return (
                f"A text field was found and focused, but its keyboard type is {keyboard_type}. "
                "The custom keyboard may not be available for this input type."
            )
        return None

    def _keyboard_selection_allows_test(self, keyboard_selection: dict) -> bool:
        return keyboard_selection.get("status") in {"SELECTED", "ATTEMPTED", "SKIPPED", "UNSUPPORTED"}

    def _keyboard_selection_error(self, keyboard_selection: dict) -> str:
        status = keyboard_selection.get("status")
        if status == "NOT_CONFIRMED":
            expected = keyboard_selection.get("expected_source_contains") or []
            return (
                "A text field was found and focused, but the configured custom keyboard could not be confirmed. "
                f"Expected keyboard indicators were not found after cycling the keyboard switcher: {expected}"
            )
        if status == "NOT_FOUND":
            return (
                "A text field was found and focused, but the iOS keyboard switcher could not be found. "
                "The custom keyboard may not be enabled for this device, may not have Full Access, or may not be available for this field."
            )
        if status == "FAILED":
            return f"A text field was found and focused, but custom keyboard selection failed: {keyboard_selection.get('error')}"
        return f"A text field was found and focused, but the custom keyboard is not available: {keyboard_selection}"

    def _queue_not_consumed_error(self, snapshot: dict, keyboard_selection: dict | None) -> str:
        next_count = int(snapshot.get("next_request_count") or 0)
        unauthorized_count = int(snapshot.get("unauthorized_next_count") or 0)
        keyboard_status = (keyboard_selection or {}).get("status")
        if next_count == 0:
            return (
                "Queued input was not consumed because the keyboard never called /next while the server was running. "
                "The focused field may be using the system keyboard, Full Access may be off, the keyboard extension may not be selected, "
                "or the extension may not be able to read the paired server config/token from its app group. "
                f"Keyboard selection status: {keyboard_status}."
            )
        if unauthorized_count:
            return (
                "Queued input was not consumed because the keyboard called /next with an invalid or missing token. "
                "The host app paired successfully, but the keyboard extension may be using stale shared config or an app group mismatch. "
                f"/next requests: {next_count}; unauthorized: {unauthorized_count}."
            )
        return (
            "Queued input was not fully consumed even though the keyboard contacted /next. "
            f"/next requests: {next_count}; queued items remaining: {snapshot.get('queued_count')}."
        )

    def _remaining_queue_is_only_return(self, snapshot: dict) -> bool:
        queue = snapshot.get("queue") or []
        if not queue:
            return False
        texts = []
        for item in queue:
            if isinstance(item, dict):
                texts.append(str(item.get("text") or ""))
            elif isinstance(item, str):
                texts.append(item)
            else:
                return False
        return bool(texts) and all(text in {"\n", "\r", "\r\n"} for text in texts)

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
            Path(app_config.artifact.get("workspace_dir") or "work/ios/acquired"),
        )

    def _handle_permission_alerts(self, device_client, global_config) -> list[dict]:
        handler = getattr(device_client, "handle_permission_alerts", None)
        if not handler:
            return [{"status": "UNSUPPORTED", "reason": "device client does not support permission alert handling"}]
        try:
            return handler(global_config.runner.permission_alerts)
        except Exception as exc:
            return [{"status": "FAILED", "error": str(exc)}]

    def _cleanup(
        self,
        app_config,
        global_config,
        device_client,
        installed_target_by_risk: bool,
        keyboard_config: dict,
        installed_keyboard_by_risk: bool,
    ) -> CleanupResult:
        if not global_config.runner.uninstall_after_each_test:
            return CleanupResult(
                status="SKIPPED",
                metadata={
                    "installed_target_by_risk": installed_target_by_risk,
                    "installed_keyboard_by_risk": installed_keyboard_by_risk,
                },
            )
        removed: list[str] = []
        errors: list[str] = []
        try:
            if installed_target_by_risk and device_client.is_installed(app_config.bundle_id):
                if device_client.remove_app(app_config.bundle_id):
                    removed.append(app_config.bundle_id)
                else:
                    errors.append(f"Could not remove target app {app_config.bundle_id}")
            keyboard_bundle_id = keyboard_config.get("bundle_id")
            if (
                installed_keyboard_by_risk
                and bool(keyboard_config.get("uninstall_after_test", False))
                and keyboard_bundle_id
                and device_client.is_installed(keyboard_bundle_id)
            ):
                if device_client.remove_app(keyboard_bundle_id):
                    removed.append(keyboard_bundle_id)
                else:
                    errors.append(f"Could not remove keyboard app {keyboard_bundle_id}")
            return CleanupResult(
                status="CLEANUP_FAILED" if errors else "CLEANED",
                removed=bool(removed),
                errors=errors,
                metadata={
                    "removed_bundle_ids": removed,
                    "installed_target_by_risk": installed_target_by_risk,
                    "installed_keyboard_by_risk": installed_keyboard_by_risk,
                },
            )
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
