"""
Configures the device Wi-Fi proxy and Burp CA trust through the Settings UI, and restores the proxy afterwards.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from mobile_playbook.common.logging_setup import safe_url
from mobile_playbook.common.network import resolve_lan_host

logger = logging.getLogger(__name__)

SETTINGS_BUNDLE_ID = "com.apple.Preferences"
SAFARI_BUNDLE_ID = "com.apple.mobilesafari"
SPRINGBOARD_BUNDLE_ID = "com.apple.springboard"


class TrafficInterceptionSetupError(RuntimeError):
    """Traffic-interception setup failure carrying a status and the setup state reached."""

    # Store the failure status and setup state alongside the message.
    def __init__(self, status: str, message: str, state: dict[str, Any] | None = None):
        super().__init__(message)
        self.status = status
        self.state = state or {}


# Resolve the configured proxy host, or auto-detect the LAN address, for the device to use.
def resolve_device_proxy_host(configured_host: str) -> str:
    resolved = resolve_lan_host(configured_host, label="device_setup.proxy_host")
    logger.debug("ios traffic setup: proxy host %s resolved to %s", configured_host or "(auto)", resolved)
    return resolved


# Save device diagnostics and return a setup error carrying the status and state.
def _save_failure(
    device_client,
    report_dir: Path,
    prefix: str,
    status: str,
    error: Exception,
    state: dict[str, Any],
) -> TrafficInterceptionSetupError:
    logger.debug("ios traffic setup: %s (state status %s): %s; saving diagnostics as %s", status, state.get("status"), error, prefix)
    details = dict(state)
    details.update(
        {
            "status": status,
            "error": str(error),
            "diagnostics": device_client.save_diagnostics(report_dir, prefix),
        }
    )
    return TrafficInterceptionSetupError(status, str(error), details)


# Activate Settings and tap back until its root page shows, raising after max_attempts.
def _return_to_settings_root(device_client, timeout: float, max_attempts: int = 8) -> None:
    logger.debug("ios traffic setup: activating %s and returning to Settings root", SETTINGS_BUNDLE_ID)
    device_client.activate_app(SETTINGS_BUNDLE_ID)
    for _ in range(max_attempts + 1):
        root_heading_visible = device_client.has_label(["Settings"], timeout)
        if root_heading_visible and device_client.has_label(["General"], timeout):
            logger.debug("ios traffic setup: at Settings root after %s back taps", _)
            return
        if _ == max_attempts:
            break
        logger.debug("ios traffic setup: not at Settings root (Settings heading=%s); tapping back (attempt %s)", root_heading_visible, _ + 1)
        device_client.tap_navigation_back(timeout)
    logger.debug("ios traffic setup: Settings root not reached after %s attempts", max_attempts)
    raise RuntimeError("could not return to the Settings root page")


# Open the details page for the named Wi-Fi network from the Settings root.
def _open_wifi_details(device_client, wifi_ssid: str, timeout: float) -> None:
    _return_to_settings_root(device_client, timeout)
    logger.debug("ios traffic setup: opening Wi-Fi details for %s", wifi_ssid)
    device_client.tap_label(["Wi-Fi"], timeout)
    device_client.tap_label([wifi_ssid], timeout)


# Read the current proxy mode, then open Configure Proxy and read its server, port and URL.
def _read_proxy_state(device_client, timeout: float) -> dict[str, str | None]:
    mode = device_client.element_value_by_label(["Configure Proxy"]) or "Off"
    logger.debug("ios traffic setup: current Configure Proxy mode %s", mode)
    device_client.tap_label(["Configure Proxy"], timeout)
    proxy_state = {
        "mode": mode,
        "server": device_client.element_value_by_label(["Server"]),
        "port": device_client.element_value_by_label(["Port"]),
        "url": device_client.element_value_by_label(["URL"]),
    }
    logger.debug("ios traffic setup: original proxy state %s", proxy_state)
    return proxy_state


# Tap Save or Done on the proxy page.
def _save_proxy(device_client, timeout: float) -> None:
    logger.debug("ios traffic setup: saving proxy settings")
    device_client.tap_label(["Save", "Done"], timeout)


# Select a manual proxy with the given host and port and save it.
def _configure_manual_proxy(device_client, host: str, port: int, timeout: float) -> None:
    logger.debug("ios traffic setup: configuring manual proxy %s:%s", host, port)
    device_client.tap_label(["Manual"], timeout)
    device_client.set_visible_text_field("Server", host)
    device_client.set_visible_text_field("Port", str(port))
    _save_proxy(device_client, timeout)


# Open General > About > Certificate Trust Settings, returning False when the row is absent.
def _open_certificate_trust_settings(device_client, timeout: float) -> bool:
    logger.debug("ios traffic setup: opening Certificate Trust Settings")
    device_client.activate_app(SPRINGBOARD_BUNDLE_ID)
    _return_to_settings_root(device_client, timeout)
    device_client.tap_label(["General"], timeout)
    device_client.tap_label(["About"], timeout)
    if not device_client.has_label(["About"], timeout):
        logger.debug("ios traffic setup: About page not confirmed")
        raise RuntimeError("could not confirm the Settings About page")
    if not device_client.has_label(["Certificate Trust Settings"], timeout):
        logger.debug("ios traffic setup: Certificate Trust Settings row not present (no user CA installed)")
        return False
    device_client.tap_label(["Certificate Trust Settings"], timeout)
    return True


# Return whether a trust switch value reads as on.
def _trusted_value(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "on"}


# Return whether the CA is fully trusted, or None when it is not installed.
def _ca_trust_state(device_client, ca_display_name: str, timeout: float) -> bool | None:
    if not _open_certificate_trust_settings(device_client, timeout):
        logger.debug("ios traffic setup: CA %s trust state unknown (no trust settings page)", ca_display_name)
        return None
    if not device_client.has_label([ca_display_name], timeout):
        logger.debug("ios traffic setup: CA %s not listed in Certificate Trust Settings", ca_display_name)
        return None
    value = device_client.element_value_by_label([ca_display_name])
    logger.debug("ios traffic setup: CA %s trust switch value %s -> trusted=%s", ca_display_name, value, _trusted_value(value))
    return _trusted_value(value)


# Download the CA profile in Safari and install it from Settings.
def _install_ca(device_client, download_url: str, timeout: float) -> None:
    logger.debug("ios traffic setup: downloading CA profile from %s in Safari", safe_url(download_url))
    device_client.activate_app(SAFARI_BUNDLE_ID)
    device_client.open_url(download_url, bundle_id=SAFARI_BUNDLE_ID)
    device_client.tap_label(["Allow", "Download"], timeout)
    try:
        device_client.tap_label(["Close"], 2)
    except Exception as exc:
        logger.debug("ios traffic setup: no Close button after download: %s", exc, exc_info=True)
    logger.debug("ios traffic setup: installing downloaded CA profile from Settings")
    device_client.activate_app(SPRINGBOARD_BUNDLE_ID)
    _return_to_settings_root(device_client, timeout)
    device_client.tap_label(["Profile Downloaded"], timeout)
    device_client.tap_label(["Install"], timeout)
    device_client.tap_label(["Install"], timeout)
    device_client.tap_label(["Done"], timeout)
    logger.debug("ios traffic setup: CA profile installation taps complete")


# Turn on full trust for the CA, confirming the prompt, with up to three attempts.
def _enable_ca_trust(device_client, ca_display_name: str, timeout: float) -> None:
    if not _open_certificate_trust_settings(device_client, timeout):
        raise RuntimeError("Certificate Trust Settings is unavailable after CA installation")
    switch = device_client.find_switch_by_label([ca_display_name], timeout)
    if _trusted_value(switch.get_attribute("value")):
        logger.debug("ios traffic setup: CA %s already fully trusted", ca_display_name)
        return
    for _ in range(3):
        logger.debug("ios traffic setup: toggling full trust for %s (attempt %s)", ca_display_name, _ + 1)
        device_client.tap_switch_by_label([ca_display_name], timeout)
        try:
            device_client.tap_label(["Continue"], 5)
        except Exception as exc:
            logger.debug("ios traffic setup: no Continue confirmation for %s: %s", ca_display_name, exc, exc_info=True)
        if _trusted_value(device_client.find_switch_by_label([ca_display_name], timeout).get_attribute("value")):
            logger.debug("ios traffic setup: CA %s now fully trusted", ca_display_name)
            return
    logger.debug("ios traffic setup: CA %s still not trusted after 3 attempts", ca_display_name)
    raise RuntimeError(f"{ca_display_name} is not fully trusted")


# Point the device proxy at Burp and ensure the CA is installed and trusted when mode is appium_ui.
def prepare_traffic_interception(device_client, config: dict[str, Any], report_dir: Path) -> dict[str, Any]:
    mode = str(config.get("mode") or "").strip()
    if mode != "appium_ui":
        logger.debug("ios traffic setup: skipped (mode=%s)", mode or "disabled")
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
    logger.debug("ios traffic setup: starting with state %s", state)
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
        logger.debug("ios traffic setup: proxy configured (restore_required=%s)", state["restore_required"])
    except Exception as exc:
        logger.debug("ios traffic setup: proxy setup failed: %s", exc, exc_info=True)
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
        logger.debug("ios traffic setup: CA trust check failed: %s", exc, exc_info=True)
        raise _save_failure(
            device_client,
            report_dir,
            "traffic_interception_ca_trust_check_failed",
            "CA_TRUST_FAILED",
            exc,
            state,
        ) from exc

    logger.debug("ios traffic setup: CA %s trust state %s", ca_display_name, trust_state)
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
            logger.debug("ios traffic setup: CA installation failed: %s", exc, exc_info=True)
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
        logger.debug("ios traffic setup: enabling CA trust failed: %s", exc, exc_info=True)
        raise _save_failure(
            device_client,
            report_dir,
            "traffic_interception_ca_trust_failed",
            "CA_TRUST_FAILED",
            exc,
            state,
        ) from exc
    state.update({"status": "READY", "ca_action": state.get("ca_action") or "trusted"})
    logger.debug("ios traffic setup: READY (ca_action=%s)", state["ca_action"])
    return state


# Turn the proxy off or restore its original settings when setup changed it.
def restore_traffic_interception(device_client, state: dict[str, Any], report_dir: Path) -> dict[str, Any]:
    if not state or not state.get("restore_required"):
        logger.debug("ios traffic restore: skipped (restore_required=%s)", (state or {}).get("restore_required"))
        return {"status": "SKIPPED"}
    original = state.get("original_proxy") or {}
    timeout = float(state.get("navigation_timeout_seconds", 20))
    logger.debug(
        "ios traffic restore: restoring proxy on %s (cleanup_mode=%s, original=%s)",
        state.get("wifi_ssid"),
        state.get("proxy_cleanup_mode"),
        original,
    )
    try:
        _return_to_settings_root(device_client, timeout)
        _open_wifi_details(device_client, str(state.get("wifi_ssid") or ""), timeout)
        device_client.tap_label(["Configure Proxy"], timeout)
        if state.get("proxy_cleanup_mode") == "off":
            device_client.tap_label(["Off"], timeout)
            _save_proxy(device_client, timeout)
            logger.debug("ios traffic restore: proxy turned Off")
            return {"status": "RESTORED", "proxy_mode": "Off"}
        mode = str(original.get("mode") or "Off")
        normalized_mode = "Automatic" if mode.lower() in {"auto", "automatic"} else mode
        logger.debug("ios traffic restore: selecting proxy mode %s", normalized_mode)
        device_client.tap_label([normalized_mode], timeout)
        if normalized_mode == "Manual":
            device_client.set_visible_text_field("Server", str(original.get("server") or ""))
            device_client.set_visible_text_field("Port", str(original.get("port") or ""))
        elif normalized_mode == "Automatic":
            device_client.set_visible_text_field("URL", str(original.get("url") or ""))
        _save_proxy(device_client, timeout)
        logger.debug("ios traffic restore: RESTORED to mode %s", normalized_mode)
        return {"status": "RESTORED", "original_proxy": original}
    except Exception as exc:
        logger.debug("ios traffic restore: failed: %s", exc, exc_info=True)
        raise _save_failure(
            device_client,
            report_dir,
            "traffic_interception_proxy_restore_failed",
            "DEVICE_PROXY_RESTORE_FAILED",
            exc,
            state,
        ) from exc
