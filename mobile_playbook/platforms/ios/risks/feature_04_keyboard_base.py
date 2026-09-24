from __future__ import annotations

from urllib.parse import urlparse

from mobile_playbook.core.network import resolve_lan_host
from mobile_playbook.platforms.ios.control_server import CommandControlServer
from mobile_playbook.platforms.ios.risks.companion_app_base import CompanionAppRiskBase


class Feature04KeyboardRiskBase(CompanionAppRiskBase):
    """Shared custom-keyboard workflow helpers for ios-feature-04 risks."""

    companion_config_key = "keyboard_app"
    companion_label = "keyboard"

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
        host = resolve_lan_host(str(advertised), label="collection.advertised_host")
        parsed = urlparse(base_url)
        return f"{parsed.scheme}://{host}:{parsed.port}"

    def _install_or_verify_keyboard_app(self, keyboard_config: dict, global_config, device_client) -> dict:
        return self._install_or_verify_companion_app(keyboard_config, global_config, device_client)

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
