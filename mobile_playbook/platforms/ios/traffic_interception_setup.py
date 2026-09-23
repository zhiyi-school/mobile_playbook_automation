from __future__ import annotations

from pathlib import Path
from typing import Any

from mobile_playbook.core.network import resolve_lan_host

SETTINGS_BUNDLE_ID = "com.apple.Preferences"
SAFARI_BUNDLE_ID = "com.apple.mobilesafari"
SPRINGBOARD_BUNDLE_ID = "com.apple.springboard"


class TrafficInterceptionSetupError(RuntimeError):
    def __init__(self, status: str, message: str, state: dict[str, Any] | None = None):
        super().__init__(message)
        self.status = status
        self.state = state or {}


def resolve_device_proxy_host(configured_host: str) -> str:
    return resolve_lan_host(configured_host, label="device_setup.proxy_host")


def _save_failure(
    device_client,
    report_dir: Path,
    prefix: str,
    status: str,
    error: Exception,
    state: dict[str, Any],
) -> TrafficInterceptionSetupError:
    details = dict(state)
    details.update(
        {
            "status": status,
            "error": str(error),
            "diagnostics": device_client.save_diagnostics(report_dir, prefix),
        }
    )
    return TrafficInterceptionSetupError(status, str(error), details)


def _return_to_settings_root(device_client, timeout: float, max_attempts: int = 8) -> None:
    device_client.activate_app(SETTINGS_BUNDLE_ID)
    for _ in range(max_attempts + 1):
        root_heading_visible = device_client.has_label(["Settings"], timeout)
        if root_heading_visible and device_client.has_label(["General"], timeout):
            return
        if _ == max_attempts:
            break
        device_client.tap_navigation_back(timeout)
    raise RuntimeError("could not return to the Settings root page")


def _open_wifi_details(device_client, wifi_ssid: str, timeout: float) -> None:
    _return_to_settings_root(device_client, timeout)
    device_client.tap_label(["Wi-Fi"], timeout)
    device_client.tap_label([wifi_ssid], timeout)


def _read_proxy_state(device_client, timeout: float) -> dict[str, str | None]:
    mode = device_client.element_value_by_label(["Configure Proxy"]) or "Off"
    device_client.tap_label(["Configure Proxy"], timeout)
    return {
        "mode": mode,
        "server": device_client.element_value_by_label(["Server"]),
        "port": device_client.element_value_by_label(["Port"]),
        "url": device_client.element_value_by_label(["URL"]),
    }


def _save_proxy(device_client, timeout: float) -> None:
    device_client.tap_label(["Save", "Done"], timeout)


def _configure_manual_proxy(device_client, host: str, port: int, timeout: float) -> None:
    device_client.tap_label(["Manual"], timeout)
    device_client.set_visible_text_field("Server", host)
    device_client.set_visible_text_field("Port", str(port))
    _save_proxy(device_client, timeout)


def _open_certificate_trust_settings(device_client, timeout: float) -> bool:
    device_client.activate_app(SPRINGBOARD_BUNDLE_ID)
    _return_to_settings_root(device_client, timeout)
    device_client.tap_label(["General"], timeout)
    device_client.tap_label(["About"], timeout)
    if not device_client.has_label(["About"], timeout):
        raise RuntimeError("could not confirm the Settings About page")
    if not device_client.has_label(["Certificate Trust Settings"], timeout):
        return False
    device_client.tap_label(["Certificate Trust Settings"], timeout)
    return True


def _trusted_value(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "on"}


def _ca_trust_state(device_client, ca_display_name: str, timeout: float) -> bool | None:
    if not _open_certificate_trust_settings(device_client, timeout):
        return None
    if not device_client.has_label([ca_display_name], timeout):
        return None
    return _trusted_value(device_client.element_value_by_label([ca_display_name]))


def _install_ca(device_client, download_url: str, timeout: float) -> None:
    device_client.activate_app(SAFARI_BUNDLE_ID)
    device_client.open_url(download_url, bundle_id=SAFARI_BUNDLE_ID)
    device_client.tap_label(["Allow", "Download"], timeout)
    try:
        device_client.tap_label(["Close"], 2)
    except Exception:
        pass
    device_client.activate_app(SPRINGBOARD_BUNDLE_ID)
    _return_to_settings_root(device_client, timeout)
    device_client.tap_label(["Profile Downloaded"], timeout)
    device_client.tap_label(["Install"], timeout)
    device_client.tap_label(["Install"], timeout)
    device_client.tap_label(["Done"], timeout)


def _enable_ca_trust(device_client, ca_display_name: str, timeout: float) -> None:
    if not _open_certificate_trust_settings(device_client, timeout):
        raise RuntimeError("Certificate Trust Settings is unavailable after CA installation")
    switch = device_client.find_switch_by_label([ca_display_name], timeout)
    if _trusted_value(switch.get_attribute("value")):
        return
    for _ in range(3):
        device_client.tap_switch_by_label([ca_display_name], timeout)
        try:
            device_client.tap_label(["Continue"], 5)
        except Exception:
            pass
        if _trusted_value(device_client.find_switch_by_label([ca_display_name], timeout).get_attribute("value")):
            return
    raise RuntimeError(f"{ca_display_name} is not fully trusted")


def prepare_traffic_interception(device_client, config: dict[str, Any], report_dir: Path) -> dict[str, Any]:
    mode = str(config.get("mode") or "").strip()
    if mode != "appium_ui":
        return {"status": "SKIPPED", "mode": mode or "disabled", "restore_required": False}

    timeout = float(config.get("navigation_timeout_seconds", 20))
    wifi_ssid = str(config.get("wifi_ssid") or "").strip()
    state: dict[str, Any] = {
        "status": "STARTED",
        "mode": mode,
        "wifi_ssid": wifi_ssid,
        "restore_required": False,
        "navigation_timeout_seconds": timeout,
        "proxy_cleanup_mode": str(config.get("proxy_cleanup_mode") or "restore"),
    }
    try:
        if not wifi_ssid:
            raise ValueError("device_setup.wifi_ssid is required")
        proxy_host = resolve_device_proxy_host(str(config.get("proxy_host") or ""))
        proxy_port = int(config.get("proxy_port"))
        if not 1 <= proxy_port <= 65535:
            raise ValueError("device_setup.proxy_port must be between 1 and 65535")
        state.update({"proxy_host": proxy_host, "proxy_port": proxy_port})
        _open_wifi_details(device_client, wifi_ssid, timeout)
        state["original_proxy"] = _read_proxy_state(device_client, timeout)
        state["restore_required"] = bool(config.get("restore_proxy_after_test", True))
        _configure_manual_proxy(device_client, proxy_host, proxy_port, timeout)
    except Exception as exc:
        raise _save_failure(
            device_client,
            report_dir,
            "traffic_interception_proxy_setup_failed",
            "DEVICE_PROXY_SETUP_FAILED",
            exc,
            state,
        ) from exc

    ca_display_name = str(config.get("ca_display_name") or "").strip()
    try:
        if not ca_display_name:
            raise ValueError("device_setup.ca_display_name is required")
        trust_state = _ca_trust_state(device_client, ca_display_name, timeout)
    except Exception as exc:
        raise _save_failure(
            device_client,
            report_dir,
            "traffic_interception_ca_trust_check_failed",
            "CA_TRUST_FAILED",
            exc,
            state,
        ) from exc

    if trust_state is True:
        state.update({"status": "READY", "ca_action": "already_trusted"})
        return state

    if trust_state is None:
        try:
            if not bool(config.get("configure_ca_if_missing", True)):
                raise RuntimeError(f"{ca_display_name} is not installed")
            download_url = str(config.get("ca_download_url") or "").strip()
            if not download_url:
                raise ValueError("device_setup.ca_download_url is required")
            _install_ca(device_client, download_url, timeout)
            state["ca_action"] = "installed"
        except Exception as exc:
            raise _save_failure(
                device_client,
                report_dir,
                "traffic_interception_ca_installation_failed",
                "CA_INSTALLATION_FAILED",
                exc,
                state,
            ) from exc

    try:
        _enable_ca_trust(device_client, ca_display_name, timeout)
    except Exception as exc:
        raise _save_failure(
            device_client,
            report_dir,
            "traffic_interception_ca_trust_failed",
            "CA_TRUST_FAILED",
            exc,
            state,
        ) from exc
    state.update({"status": "READY", "ca_action": state.get("ca_action") or "trusted"})
    return state


def restore_traffic_interception(device_client, state: dict[str, Any], report_dir: Path) -> dict[str, Any]:
    if not state or not state.get("restore_required"):
        return {"status": "SKIPPED"}
    original = state.get("original_proxy") or {}
    timeout = float(state.get("navigation_timeout_seconds", 20))
    try:
        _return_to_settings_root(device_client, timeout)
        _open_wifi_details(device_client, str(state.get("wifi_ssid") or ""), timeout)
        device_client.tap_label(["Configure Proxy"], timeout)
        if state.get("proxy_cleanup_mode") == "off":
            device_client.tap_label(["Off"], timeout)
            _save_proxy(device_client, timeout)
            return {"status": "RESTORED", "proxy_mode": "Off"}
        mode = str(original.get("mode") or "Off")
        normalized_mode = "Automatic" if mode.lower() in {"auto", "automatic"} else mode
        device_client.tap_label([normalized_mode], timeout)
        if normalized_mode == "Manual":
            device_client.set_visible_text_field("Server", str(original.get("server") or ""))
            device_client.set_visible_text_field("Port", str(original.get("port") or ""))
        elif normalized_mode == "Automatic":
            device_client.set_visible_text_field("URL", str(original.get("url") or ""))
        _save_proxy(device_client, timeout)
        return {"status": "RESTORED", "original_proxy": original}
    except Exception as exc:
        raise _save_failure(
            device_client,
            report_dir,
            "traffic_interception_proxy_restore_failed",
            "DEVICE_PROXY_RESTORE_FAILED",
            exc,
            state,
        ) from exc
