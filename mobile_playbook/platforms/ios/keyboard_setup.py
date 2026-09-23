from __future__ import annotations

from pathlib import Path
from typing import Any

SETTINGS_BUNDLE_ID = "com.apple.Preferences"
SPRINGBOARD_BUNDLE_ID = "com.apple.springboard"
ADD_NEW_KEYBOARD_LABELS = ["Add New Keyboard…", "Add New Keyboard..."]
FULL_ACCESS_LABELS = ["Allow Full Access"]
CANCEL_LABELS = ["Cancel"]


class KeyboardSetupError(RuntimeError):
    def __init__(self, status: str, message: str, state: dict[str, Any] | None = None):
        super().__init__(message)
        self.status = status
        self.state = state or {}


def _on(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "on"}


def _display_names(config: dict[str, Any]) -> list[str]:
    names = config.get("keyboard_display_names")
    if isinstance(names, str):
        names = [names]
    if not names:
        single = config.get("keyboard_display_name")
        names = [single] if single else []
    ordered: list[str] = []
    for candidate in (config.get("keyboard_extension_bundle_id"), *names):
        value = str(candidate or "").strip()
        if value and value not in ordered:
            ordered.append(value)
    return ordered


def _return_to_settings_root(device_client, timeout: float, max_attempts: int = 8) -> None:
    device_client.activate_app(SETTINGS_BUNDLE_ID)
    for attempt in range(max_attempts + 1):
        if device_client.has_label(["Settings"], timeout) and device_client.has_label(["General"], timeout):
            return
        if attempt == max_attempts:
            break
        device_client.tap_navigation_back(timeout)
    raise RuntimeError("could not return to the Settings root page")


def _open_keyboards_list(device_client, timeout: float) -> None:
    device_client.activate_app(SPRINGBOARD_BUNDLE_ID)
    _return_to_settings_root(device_client, timeout)
    device_client.tap_row_label(["General"], timeout)
    device_client.tap_row_label(["Keyboard", "Keyboards"], timeout)
    device_client.tap_row_label(["Keyboards"], timeout)


def _added_keyboard_name(device_client, names: list[str], probe: float) -> str | None:
    for name in names:
        if device_client.has_label([name], probe):
            return name
    return None


def _add_keyboard(device_client, names: list[str], timeout: float, probe: float) -> str | None:
    device_client.tap_row_label(ADD_NEW_KEYBOARD_LABELS, timeout)
    for name in names:
        if device_client.has_label([name], probe):
            device_client.tap_row_label([name], timeout)
            return name
    try:
        device_client.tap_label(CANCEL_LABELS, probe)
    except Exception:
        pass
    return None


def _enable_full_access(device_client, display_name: str, timeout: float) -> None:
    device_client.tap_row_label([display_name], timeout)
    if _on(device_client.find_switch_by_label(FULL_ACCESS_LABELS, timeout).get_attribute("value")):
        return
    for _ in range(3):
        device_client.tap_switch_by_label(FULL_ACCESS_LABELS, timeout)
        try:
            device_client.tap_label(["Allow"], 5)
        except Exception:
            pass
        if _on(device_client.find_switch_by_label(FULL_ACCESS_LABELS, timeout).get_attribute("value")):
            return
    raise RuntimeError(f"Full Access was not enabled for {display_name}")


def prepare_custom_keyboard(device_client, config: dict[str, Any], report_dir: Path) -> dict[str, Any]:
    if str(config.get("mode") or "").strip() != "appium_ui":
        return {"status": "SKIPPED", "mode": config.get("mode") or "disabled"}
    names = _display_names(config)
    timeout = float(config.get("navigation_timeout_seconds", 20))
    probe = float(config.get("probe_timeout_seconds", 2))
    state: dict[str, Any] = {"status": "STARTED", "keyboard_display_names": names, "stage": "init"}
    try:
        if not names:
            raise ValueError("keyboard_setup.keyboard_display_names is required")

        state["stage"] = "open_keyboards_list"
        _open_keyboards_list(device_client, timeout)
        # 4 == foreground. A silent navigation miss otherwise surfaces only as
        # NoSuchElementError with no indication of which app was on screen.
        state["settings_foreground"] = device_client.query_app_state("com.apple.Preferences") == 4
        if not state["settings_foreground"]:
            device_client.activate_app("com.apple.Preferences")
            _open_keyboards_list(device_client, timeout)
            state["settings_foreground"] = device_client.query_app_state("com.apple.Preferences") == 4
            if not state["settings_foreground"]:
                raise RuntimeError("the Settings app is not in the foreground; keyboard setup cannot navigate")

        state["stage"] = "probe_added"
        matched = _added_keyboard_name(device_client, names, probe)
        if matched:
            state["keyboard_action"] = "already_added"
        else:
            state["stage"] = "add_keyboard"
            matched = _add_keyboard(device_client, names, timeout, probe)
            if not matched:
                raise RuntimeError(
                    f"none of {names} appears in the Keyboards list or the Add New Keyboard sheet; "
                    "the keyboard extension is not registered on this device"
                )
            state["keyboard_action"] = "added"
        state["keyboard_display_name"] = matched
        if bool(config.get("require_full_access", True)):
            state["stage"] = "enable_full_access"
            _enable_full_access(device_client, matched, timeout)
            state["full_access"] = True
        state["stage"] = "complete"
        state["status"] = "READY"
        return state
    except Exception as exc:
        state.update({
            "status": "KEYBOARD_SETUP_FAILED",
            "error": str(exc),
            "diagnostics": device_client.save_diagnostics(report_dir, "keyboard_setup_failed"),
        })
        raise KeyboardSetupError("KEYBOARD_SETUP_FAILED", str(exc), state) from exc
