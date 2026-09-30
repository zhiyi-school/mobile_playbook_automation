"""
Shared custom-keyboard workflow helpers for the ios-feature-04 risks.
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from mobile_playbook.common.network import resolve_lan_host
from mobile_playbook.platforms.ios.control_server import CommandControlServer
from mobile_playbook.platforms.ios.risks.companion_app_base import CompanionAppRiskBase

logger = logging.getLogger(__name__)


class Feature04KeyboardRiskBase(CompanionAppRiskBase):
    """Shared custom-keyboard workflow helpers for ios-feature-04 risks."""

    companion_config_key = "keyboard_app"
    companion_label = "keyboard"

    # Store the factory used to create the keystroke collection server.
    def __init__(self, server_factory=CommandControlServer):
        self.server_factory = server_factory

    # Create and start the collection server from the control settings.
    def _start_server(self, control: dict) -> CommandControlServer:
        logger.debug(
            "%s: starting collection server bind_host=%s port=%s token_configured=%s enqueue_requires_token=%s",
            getattr(self, "risk_id", type(self).__name__),
            control.get("bind_host", "0.0.0.0"),
            control.get("port", 8765),
            bool(control.get("token")),
            bool(control.get("enqueue_requires_token", False)),
        )
        server = self.server_factory(
            host=control.get("bind_host", "0.0.0.0"),
            port=int(control.get("port", 8765)),
            token=control.get("token"),
            enqueue_requires_token=bool(control.get("enqueue_requires_token", False)),
        )
        started = server.start()
        logger.debug("%s: collection server started at %s", getattr(self, "risk_id", type(self).__name__), getattr(started, "base_url", None))
        return started

    # Return the server URL with the advertised LAN host substituted when one is configured.
    def _device_reachable_base_url(self, base_url: str, control: dict) -> str:
        advertised = control.get("advertised_host")
        if not advertised or str(advertised).startswith("REPLACE_WITH"):
            logger.debug("%s: no advertised_host configured (%s); device uses %s", getattr(self, "risk_id", type(self).__name__), advertised, base_url)
            return base_url
        host = resolve_lan_host(str(advertised), label="collection.advertised_host")
        parsed = urlparse(base_url)
        logger.debug("%s: advertised_host %s resolved to %s for base_url %s", getattr(self, "risk_id", type(self).__name__), advertised, host, base_url)
        return f"{parsed.scheme}://{host}:{parsed.port}"

    # Install or verify the keyboard companion app.
    def _install_or_verify_keyboard_app(self, keyboard_config: dict, global_config, device_client) -> dict:
        return self._install_or_verify_companion_app(keyboard_config, global_config, device_client)

    # Enter the server URL into the keyboard app's settings field and save, if a field is configured.
    def _configure_keyboard_server_url(self, device_client, keyboard_config: dict, device_reachable_base_url: str) -> dict | None:
        server_setup = keyboard_config.get("server_setup") or {}
        field_id = server_setup.get("server_url_input_accessibility_id")
        if not field_id:
            logger.debug("%s: keyboard server_setup has no server_url_input_accessibility_id; skipping URL setup", getattr(self, "risk_id", type(self).__name__))
            return None
        value = server_setup.get("value") or device_reachable_base_url
        clear_first = bool(server_setup.get("clear_first", True))
        logger.debug("%s: setting keyboard server URL %s into accessibility_id=%s clear_first=%s", getattr(self, "risk_id", type(self).__name__), value, field_id, clear_first)
        result = {
            "server_url": value,
            "field": device_client.set_text_by_accessibility_id(field_id, value, clear_first=clear_first),
        }
        save_button_id = server_setup.get("save_button_accessibility_id")
        if save_button_id:
            logger.debug("%s: tapping keyboard server save button accessibility_id=%s", getattr(self, "risk_id", type(self).__name__), save_button_id)
            result["save_button"] = device_client.tap_by_accessibility_id(save_button_id)
        return result

    # Ask the device client to switch to the custom keyboard, confirmed by expected page-source markers.
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
        logger.debug(
            "%s: selecting custom keyboard enabled=%s expected_source_contains=%s supported=%s",
            getattr(self, "risk_id", type(self).__name__),
            selection_config.get("enabled"),
            expected,
            bool(selector),
        )
        if not selector:
            return {"status": "UNSUPPORTED", "reason": "device client does not support keyboard selection"}
        try:
            selection = selector(selection_config)
        except Exception as exc:
            logger.debug("%s: keyboard selection raised: %s", getattr(self, "risk_id", type(self).__name__), exc, exc_info=True)
            return {"status": "FAILED", "error": str(exc)}
        logger.debug("%s: keyboard selection result status=%s", getattr(self, "risk_id", type(self).__name__), selection.get("status") if isinstance(selection, dict) else selection)
        return selection

    # Return why the focused field cannot use a custom keyboard, or None when it can.
    def _focused_field_custom_keyboard_blocker(self, focus_result: dict) -> str | None:
        element_type = str(focus_result.get("element_type") or "")
        if element_type == "XCUIElementTypeSecureTextField":
            logger.debug("%s: focused field is a secure text field; custom keyboard blocked", getattr(self, "risk_id", type(self).__name__))
            return (
                "A text field was found and focused, but it is a secure text field. "
                "iOS does not allow third-party custom keyboards in secure text fields, so LocalKeyboard cannot be used there."
            )
        keyboard_type = str((focus_result.get("element") or {}).get("keyboard_type") or "")
        if keyboard_type and keyboard_type.lower() in {"phonepad", "numberpad", "decimalpad"}:
            logger.debug("%s: focused field keyboard_type=%s blocks custom keyboard", getattr(self, "risk_id", type(self).__name__), keyboard_type)
            return (
                f"A text field was found and focused, but its keyboard type is {keyboard_type}. "
                "The custom keyboard may not be available for this input type."
            )
        logger.debug("%s: focused field element_type=%s keyboard_type=%s allows custom keyboard", getattr(self, "risk_id", type(self).__name__), element_type, keyboard_type)
        return None

    # Return whether the keyboard selection status lets the test proceed.
    def _keyboard_selection_allows_test(self, keyboard_selection: dict) -> bool:
        return keyboard_selection.get("status") in {"SELECTED", "ATTEMPTED", "SKIPPED", "UNSUPPORTED"}

    # Return the user-facing error for an unusable keyboard selection status.
    def _keyboard_selection_error(self, keyboard_selection: dict) -> str:
        status = keyboard_selection.get("status")
        logger.debug("%s: keyboard selection not usable, status=%s", getattr(self, "risk_id", type(self).__name__), status)
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

    # Explain why queued input was not consumed, based on the server's /next request counts.
    def _queue_not_consumed_error(self, snapshot: dict, keyboard_selection: dict | None) -> str:
        next_count = int(snapshot.get("next_request_count") or 0)
        unauthorized_count = int(snapshot.get("unauthorized_next_count") or 0)
        keyboard_status = (keyboard_selection or {}).get("status")
        logger.debug(
            "%s: queue not consumed next_requests=%s unauthorized=%s queued=%s keyboard_status=%s",
            getattr(self, "risk_id", type(self).__name__),
            next_count,
            unauthorized_count,
            snapshot.get("queued_count"),
            keyboard_status,
        )
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

    # Return whether every item left in the queue is only a newline.
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
