"""
Appium XCUITest device client that drives app installs, UI interaction and evidence capture on iOS.
"""

from __future__ import annotations

import base64
import functools
import io
import logging
import shutil
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from mobile_playbook.common.logging_setup import redacted
from mobile_playbook.platforms.ios.ipa.unpacker import safe_extract_zip
from mobile_playbook.platforms.ios.models import InstallResult

logger = logging.getLogger(__name__)

UNTRUSTED_DEVELOPER_CERT_MARKERS = (
    "developer app certificate is not trusted",
    "profile has not been explicitly trusted",
)


# Return whether an error says the developer certificate is not yet trusted on the device.
def is_untrusted_developer_cert_error(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in UNTRUSTED_DEVELOPER_CERT_MARKERS)


class TextFieldUnavailableError(RuntimeError):
    """The current UI has no text field available to the configured probe."""


APP_STATE_RUNNING_FOREGROUND = 4
ALERT_NEGATIVE_LABELS = (
    "don't allow", "dont allow", "ask app not to track", "ask not to track",
    "not now", "cancel", "deny", "deny permission", "no",
)
ALERT_POSITIVE_LABELS = ("allow while using app", "allow once", "allow", "ok", "continue")
ALERT_ACKNOWLEDGE_LABELS = ("ok", "close", "dismiss", "done")
PERMISSION_PROMPT_PHRASES = ("would like to", "wants to", "do you want to allow", 'allow "', "allow “")
MAX_UNSCOPED_ALERT_BUTTONS = 24
ALERT_SCAN_GRACE_SECONDS = 10.0
BLOCKING_PROMPT_WAIT_SECONDS = 1.0


class AlertButtonsNotFound(RuntimeError):
    """No button at all was visible for the alert."""


class NoSafeAlertButton(RuntimeError):
    """Buttons were visible, but none carried a label that is safe to tap for the requested action."""


class AlertScanAborted(RuntimeError):
    """The alert button scan stopped because the screen changed, ran out of time, or was not an alert."""


# Retry a failed UI operation once when a system permission prompt was blocking it and could be answered.
def _recovers_from_blocking_prompts(method):
    # Run the operation, and on failure answer a blocking prompt and run it again; nested operations defer to the outermost.
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        if self._prompt_recovery_paused or not self.prompt_recovery_config:
            return method(self, *args, **kwargs)
        self._prompt_recovery_paused = True
        try:
            try:
                return method(self, *args, **kwargs)
            except Exception as exc:
                if not self._answer_blocking_prompt(method.__name__, exc):
                    raise
            return method(self, *args, **kwargs)
        finally:
            self._prompt_recovery_paused = False

    return wrapper


# Normalize a button label for exact comparison with the known alert labels.
def _normalized_label(value) -> str:
    return " ".join(str(value or "").replace("’", "'").lower().split()).strip(" .")


class AppiumDeviceClient:
    TEXT_FIELD_CLASS_NAMES = [
        "XCUIElementTypeTextField",
        "XCUIElementTypeSearchField",
        "XCUIElementTypeTextView",
        "XCUIElementTypeSecureTextField",
    ]

    # Store the device configuration and the permission-prompt rules used to recover blocked operations.
    def __init__(self, device_config, prompt_recovery_config: dict | None = None):
        self.device_config = device_config
        self.driver = None
        self.prompt_recovery_config = dict(prompt_recovery_config or {})
        self.recovered_prompts: list[dict] = []
        self._prompt_recovery_paused = False

    # Start an XCUITest Appium session, explaining an untrusted WebDriverAgent certificate on failure.
    def connect(self):
        from appium import webdriver
        from appium.options.ios import XCUITestOptions

        options = XCUITestOptions()
        options.set_capability("platformName", "iOS")
        options.set_capability("appium:automationName", "XCUITest")
        options.set_capability("appium:udid", self.device_config.udid)
        if self.device_config.platform_version:
            options.set_capability("appium:platformVersion", self.device_config.platform_version)
        options.set_capability("appium:xcodeOrgId", self.device_config.team_id)
        options.set_capability("appium:xcodeSigningId", self.device_config.xcode_signing_id)
        options.set_capability("appium:useNewWDA", not self.device_config.keep_wda)
        options.set_capability("appium:showXcodeLog", self.device_config.show_xcode_log)
        if self.device_config.updated_wda_bundle_id:
            options.set_capability("appium:updatedWDABundleId", self.device_config.updated_wda_bundle_id)
        options.set_capability(
            "appium:allowProvisioningDeviceRegistration",
            self.device_config.allow_provisioning_device_registration,
        )
        options.set_capability("appium:newCommandTimeout", 300)
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "ios appium: creating session at %s with capabilities %s",
                self.device_config.appium_server_url,
                redacted(options.to_capabilities()),
            )
        started = time.monotonic()
        try:
            self.driver = webdriver.Remote(self.device_config.appium_server_url, options=options)
        except Exception as exc:
            logger.debug(
                "ios appium: session creation failed after %.2fs: %s", time.monotonic() - started, exc, exc_info=True
            )
            logger.debug("ios appium: untrusted developer cert error=%s", is_untrusted_developer_cert_error(str(exc)))
            if is_untrusted_developer_cert_error(str(exc)):
                raise RuntimeError(
                    "WebDriverAgent's Developer App certificate is not trusted on this device yet. "
                    "On the iPhone: Settings > General > VPN & Device Management > select the "
                    f"Developer App certificate for team {self.device_config.team_id} > Trust, then confirm "
                    "\"Trust\" in the popup. This is a one-time physical action Apple requires per device — "
                    f"nothing in Appium, xcodebuild, or this framework can grant it. Original error: {exc}"
                ) from exc
            raise RuntimeError(
                f"failed to start Appium session at {self.device_config.appium_server_url}: {exc}"
            ) from exc
        logger.debug(
            "ios appium: session created in %.2fs (session id %s)",
            time.monotonic() - started,
            getattr(self.driver, "session_id", None),
        )
        return self

    # Quit the Appium session if one is active.
    def quit(self) -> None:
        if self.driver is not None:
            logger.debug("ios appium: quitting session %s", getattr(self.driver, "session_id", None))
            self.driver.quit()
            self.driver = None
            logger.debug("ios appium: session quit")
        else:
            logger.debug("ios appium: quit requested with no active session")

    # Run a mobile: command on the session, raising when not connected.
    def _execute(self, command: str, args: dict):
        if self.driver is None:
            logger.debug("ios appium: mobile: %s requested without a session", command)
            raise RuntimeError("Appium session is not connected")
        logger.debug("ios appium: executing mobile: %s arg keys=%s", command, sorted(args))
        started = time.monotonic()
        try:
            result = self.driver.execute_script(f"mobile: {command}", args)
        except Exception as exc:
            logger.debug(
                "ios appium: mobile: %s failed after %.2fs: %s", command, time.monotonic() - started, exc, exc_info=True
            )
            raise
        logger.debug(
            "ios appium: mobile: %s returned %s (len %s) in %.2fs",
            command,
            type(result).__name__,
            len(result) if isinstance(result, (str, bytes, list, dict)) else "-",
            time.monotonic() - started,
        )
        return result

    # Return whether an app is installed on the device.
    def is_installed(self, bundle_id: str) -> bool:
        return bool(self._execute("isAppInstalled", {"bundleId": bundle_id}))

    # Uninstall an app and return whether Appium reported success.
    def remove_app(self, bundle_id: str) -> bool:
        return bool(self._execute("removeApp", {"bundleId": bundle_id}))

    # Remove an app, then poll until it is actually gone or the timeout passes.
    def remove_app_verified(self, bundle_id: str, timeout_seconds: float = 20.0) -> dict:
        logger.debug("ios appium: removing %s with verification (timeout %ss)", bundle_id, timeout_seconds)
        requested = self.remove_app(bundle_id)
        deadline = time.monotonic() + timeout_seconds
        polls = 0
        while time.monotonic() < deadline:
            polls += 1
            if not self.is_installed(bundle_id):
                logger.debug("ios appium: %s removal verified after %s poll(s), requested=%s", bundle_id, polls, requested)
                return {"requested": requested, "verified": True}
            logger.debug("ios appium: %s still installed after poll %s, retrying", bundle_id, polls)
            time.sleep(1.0)
        logger.debug("ios appium: %s removal not verified within %ss (requested=%s)", bundle_id, timeout_seconds, requested)
        return {"requested": requested, "verified": False}

    # Terminate a running app.
    def terminate_app(self, bundle_id: str) -> bool:
        return bool(self._execute("terminateApp", {"bundleId": bundle_id}))

    # Install an IPA from its absolute path, returning an INSTALLED or INSTALL_FAILED result.
    def install_app(self, ipa_path: Path, timeout_ms: int) -> InstallResult:
        # Appium is a separate process, so a relative path would resolve against its working directory.
        ipa_path = Path(ipa_path).expanduser().resolve()
        logger.debug("ios appium: installing %s (timeout %sms)", ipa_path, timeout_ms)
        try:
            self._execute("installApp", {"app": str(ipa_path), "timeout": timeout_ms})
            logger.debug("ios appium: installed %s", ipa_path)
            return InstallResult(status="INSTALLED", ipa_path=ipa_path)
        except Exception as exc:
            logger.debug("ios appium: install of %s failed: %s", ipa_path, exc, exc_info=True)
            return InstallResult(status="INSTALL_FAILED", ipa_path=ipa_path, errors=[str(exc)])

    # Launch an app by bundle ID.
    def launch_app(self, bundle_id: str) -> dict:
        return {"result": self._execute("launchApp", {"bundleId": bundle_id})}

    # Bring an app to the foreground by bundle ID.
    def activate_app(self, bundle_id: str) -> dict:
        return {"result": self._execute("activateApp", {"bundleId": bundle_id})}

    # Wait for an element whose label, name or value matches one of the labels.
    @_recovers_from_blocking_prompts
    def find_element_by_label(self, labels: list[str], timeout_seconds: float):
        if self.driver is None:
            raise RuntimeError("Appium session is not connected")
        from appium.webdriver.common.appiumby import AppiumBy
        from selenium.webdriver.support.ui import WebDriverWait

        predicate = self._label_predicate(labels)
        logger.debug("ios appium: waiting up to %ss for element by ios_predicate %s", timeout_seconds, predicate)
        try:
            element = WebDriverWait(self.driver, timeout_seconds).until(
                lambda driver: driver.find_element(AppiumBy.IOS_PREDICATE, predicate)
            )
        except Exception as exc:
            logger.debug("ios appium: element with labels %s not found within %ss: %s", labels, timeout_seconds, exc, exc_info=True)
            raise
        logger.debug("ios appium: found element for labels %s", labels)
        return element

    # Wait for a switch whose label, name or value matches one of the labels.
    @_recovers_from_blocking_prompts
    def find_switch_by_label(self, labels: list[str], timeout_seconds: float):
        if self.driver is None:
            raise RuntimeError("Appium session is not connected")
        from appium.webdriver.common.appiumby import AppiumBy
        from selenium.webdriver.support.ui import WebDriverWait

        predicate = (
            "type == 'XCUIElementTypeSwitch' AND "
            f"({self._label_predicate(labels)})"
        )
        logger.debug("ios appium: waiting up to %ss for switch by ios_predicate %s", timeout_seconds, predicate)
        try:
            element = WebDriverWait(self.driver, timeout_seconds).until(
                lambda driver: driver.find_element(AppiumBy.IOS_PREDICATE, predicate)
            )
        except Exception as exc:
            logger.debug("ios appium: switch with labels %s not found within %ss: %s", labels, timeout_seconds, exc, exc_info=True)
            raise
        logger.debug("ios appium: found switch for labels %s", labels)
        return element

    # Tap the switch matching one of the labels.
    @_recovers_from_blocking_prompts
    def tap_switch_by_label(self, labels: list[str], timeout_seconds: float):
        logger.debug("ios appium: tap switch by labels %s", labels)
        element = self.find_switch_by_label(labels, timeout_seconds)
        return self._tap_element_with_fallback(element)

    # Tap the element matching one of the labels.
    @_recovers_from_blocking_prompts
    def tap_label(self, labels: list[str], timeout_seconds: float) -> dict:
        logger.debug("ios appium: tap element by labels %s", labels)
        element = self.find_element_by_label(labels, timeout_seconds)
        tap_method = self._tap_element_with_fallback(element)
        logger.debug("ios appium: tapped labels %s via %s", labels, tap_method)
        return {
            "labels": labels,
            "tapped": True,
            "tap_method": tap_method,
            "element": self._element_summary(element, 0),
        }

    # Wait for and tap a cell or button matching one of the labels.
    @_recovers_from_blocking_prompts
    def tap_row_label(self, labels: list[str], timeout_seconds: float) -> dict:
        if self.driver is None:
            raise RuntimeError("Appium session is not connected")
        from appium.webdriver.common.appiumby import AppiumBy
        from selenium.webdriver.support.ui import WebDriverWait

        predicate = (
            "(type == 'XCUIElementTypeCell' OR type == 'XCUIElementTypeButton') AND "
            f"({self._label_predicate(labels)})"
        )
        logger.debug("ios appium: waiting up to %ss for row by ios_predicate %s", timeout_seconds, predicate)
        try:
            element = WebDriverWait(self.driver, timeout_seconds).until(
                lambda driver: driver.find_element(AppiumBy.IOS_PREDICATE, predicate)
            )
        except Exception as exc:
            logger.debug("ios appium: row with labels %s not found within %ss: %s", labels, timeout_seconds, exc, exc_info=True)
            raise
        tap_method = self._tap_element_with_fallback(element)
        logger.debug("ios appium: tapped row labels %s via %s", labels, tap_method)
        return {
            "labels": labels,
            "tapped": True,
            "tap_method": tap_method,
            "element": self._element_summary(element, 0),
        }

    # Return whether a cell or static text matching one of the labels appears within the timeout.
    def has_label(self, labels: list[str], timeout_seconds: float) -> bool:
        if self.driver is None:
            raise RuntimeError("Appium session is not connected")
        from appium.webdriver.common.appiumby import AppiumBy
        from selenium.webdriver.support.ui import WebDriverWait

        predicate = (
            "(type == 'XCUIElementTypeCell' OR type == 'XCUIElementTypeStaticText') AND "
            f"({self._label_predicate(labels)})"
        )
        logger.debug("ios appium: checking up to %ss for label by ios_predicate %s", timeout_seconds, predicate)
        try:
            WebDriverWait(self.driver, timeout_seconds).until(
                lambda driver: driver.find_element(AppiumBy.IOS_PREDICATE, predicate)
            )
        except Exception as exc:
            logger.debug("ios appium: labels %s not present within %ss: %s", labels, timeout_seconds, exc, exc_info=True)
            return False
        logger.debug("ios appium: labels %s present", labels)
        return True

    # Tap the first button in the navigation bar.
    @_recovers_from_blocking_prompts
    def tap_navigation_back(self, timeout_seconds: float) -> dict:
        if self.driver is None:
            raise RuntimeError("Appium session is not connected")
        from appium.webdriver.common.appiumby import AppiumBy
        from selenium.webdriver.support.ui import WebDriverWait

        logger.debug("ios appium: waiting up to %ss for navigation back button by ios_class_chain", timeout_seconds)
        try:
            element = WebDriverWait(self.driver, timeout_seconds).until(
                lambda driver: driver.find_element(
                    AppiumBy.IOS_CLASS_CHAIN,
                    "**/XCUIElementTypeNavigationBar/**/XCUIElementTypeButton[1]",
                )
            )
        except Exception as exc:
            logger.debug("ios appium: navigation back button not found within %ss: %s", timeout_seconds, exc, exc_info=True)
            raise
        tap_method = self._tap_element_with_fallback(element)
        logger.debug("ios appium: tapped navigation back via %s", tap_method)
        return {
            "tapped": True,
            "tap_method": tap_method,
            "element": self._element_summary(element, 0),
        }

    # Replace the value of the visible enabled text field matching a label.
    @_recovers_from_blocking_prompts
    def set_visible_text_field(self, label: str, value: str) -> dict:
        if self.driver is None:
            raise RuntimeError("Appium session is not connected")
        from appium.webdriver.common.appiumby import AppiumBy
        from selenium.webdriver.support.ui import WebDriverWait

        text_types = ", ".join(f"'{class_name}'" for class_name in self.TEXT_FIELD_CLASS_NAMES)
        predicate = (
            f"type IN {{{text_types}}} AND visible == true AND enabled == true AND "
            f"({self._label_predicate([label])})"
        )
        logger.debug("ios appium: setting text field %r (value length %s) by ios_predicate %s", label, len(value), predicate)
        try:
            element = WebDriverWait(self.driver, 10).until(
                lambda driver: driver.find_element(AppiumBy.IOS_PREDICATE, predicate)
            )
        except Exception as exc:
            logger.debug("ios appium: text field %r not found within 10s: %s", label, exc, exc_info=True)
            raise
        self._tap_element_with_fallback(element)
        try:
            element.clear()
        except Exception as exc:
            logger.debug("ios appium: clear() failed for text field %r, falling back to backspaces: %s", label, exc, exc_info=True)
            current = element.get_attribute("value") or ""
            if current:
                element.send_keys("\b" * len(str(current)))
        element.send_keys(value)
        logger.debug("ios appium: text field %r set", label)
        return {"label": label, "value": value}

    # Return the value of the element matching the labels, or None when absent.
    def element_value_by_label(self, labels: list[str]) -> str | None:
        try:
            with self.without_prompt_recovery():
                element = self.find_element_by_label(labels, 1)
        except Exception as exc:
            logger.debug("ios appium: no element for labels %s when reading value: %s", labels, exc, exc_info=True)
            return None
        value = element.get_attribute("value")
        logger.debug("ios appium: element value for labels %s is %s", labels, "absent" if value is None else "present")
        return str(value) if value is not None else None

    # Save a screenshot and page source for troubleshooting, recording any capture errors.
    def save_diagnostics(self, directory: Path, prefix: str) -> dict:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        screenshot_path = directory / f"{prefix}.png"
        source_path = directory / f"{prefix}.xml"
        diagnostics: dict = {}
        errors = []
        logger.debug("ios appium: saving diagnostics %s to %s", prefix, directory)
        try:
            self.screenshot(screenshot_path)
            diagnostics["screenshot_path"] = str(screenshot_path)
        except Exception as exc:
            logger.debug("ios appium: diagnostics screenshot failed: %s", exc, exc_info=True)
            errors.append(f"screenshot: {exc}")
        try:
            source_path.write_text(self.page_source())
            diagnostics["page_source_path"] = str(source_path)
        except Exception as exc:
            logger.debug("ios appium: diagnostics page source failed: %s", exc, exc_info=True)
            errors.append(f"page_source: {exc}")
        if errors:
            diagnostics["errors"] = errors
        logger.debug("ios appium: diagnostics saved %s with %s error(s)", sorted(diagnostics), len(errors))
        return diagnostics

    # Build an iOS predicate matching any of the labels by label, name or value.
    @staticmethod
    def _label_predicate(labels: list[str]) -> str:
        values = [str(label).strip() for label in labels if str(label).strip()]
        if not values:
            raise ValueError("at least one accessibility label is required")
        conditions = []
        for value in values:
            escaped = value.replace("\\", "\\\\").replace("'", "\\'")
            conditions.extend(
                [
                    f"label == '{escaped}'",
                    f"name == '{escaped}'",
                    f"value == '{escaped}'",
                ]
            )
        return " OR ".join(conditions)

    # Unlock the device and report whether it was locked.
    def unlock(self) -> dict:
        logger.debug("ios appium: checking device lock state")
        was_locked = self.driver.is_locked()
        self.driver.unlock()  # no-ops on its own if the device wasn't actually locked
        logger.debug("ios appium: unlock issued, was_locked=%s", was_locked)
        return {"was_locked": was_locked}

    # Open a URL in an app with mobile: deepLink.
    def open_url(self, url: str, bundle_id: str = "com.apple.mobilesafari") -> dict:
        logger.debug("ios appium: opening deep link %s in %s", str(url).split("?", 1)[0][:200], bundle_id)
        # mobile: deepLink avoids driving Safari's own address-bar UI directly.
        return {"result": self._execute("deepLink", {"url": url, "bundleId": bundle_id})}

    # Run the enclosed operations without answering prompts that block them, for steps that inspect a prompt themselves.
    @contextmanager
    def without_prompt_recovery(self):
        paused = self._prompt_recovery_paused
        self._prompt_recovery_paused = True
        try:
            yield self
        finally:
            self._prompt_recovery_paused = paused

    # Return and clear the prompts answered to recover blocked operations since the last call.
    def drain_recovered_prompts(self) -> list[dict]:
        recovered, self.recovered_prompts = self.recovered_prompts, []
        return recovered

    # Answer a system permission prompt after an operation failed, returning whether one was answered.
    def _answer_blocking_prompt(self, operation: str, exc: Exception) -> bool:
        config = {
            **self.prompt_recovery_config,
            "wait_seconds": BLOCKING_PROMPT_WAIT_SECONDS,
            "permission_prompts_only": True,
        }
        config.pop("foreground_bundle_id", None)
        try:
            results = self.handle_permission_alerts(config)
        except Exception as alert_exc:
            logger.debug("ios appium: checking for a prompt blocking %s failed: %s", operation, alert_exc, exc_info=True)
            return False
        answered = [result for result in results if result.get("status") == "HANDLED"]
        if not answered:
            logger.debug("ios appium: %s failed with no blocking prompt on screen: %s", operation, exc)
            return False
        self.recovered_prompts.extend({"operation": operation, **result} for result in answered)
        logger.info("ios: a system prompt blocked %s; answered it and retrying once.", operation)
        return True

    # Dismiss, accept or report up to max_alerts system alerts within the wait window.
    def handle_permission_alerts(self, config: dict | None = None) -> list[dict]:
        if self.driver is None:
            raise RuntimeError("Appium session is not connected")
        config = config or {}
        if not bool(config.get("enabled", True)):
            logger.debug("ios appium: permission alert handling disabled by config")
            return [{"status": "SKIPPED", "reason": "permission alert handling is disabled"}]

        import time

        max_alerts = int(config.get("max_alerts", 3))
        wait_seconds = float(config.get("wait_seconds", 2))
        action = str(config.get("action", "dismiss")).lower()
        action = action if action in {"dismiss", "accept", "alert_only"} else "dismiss"
        accept_if = [str(v).strip().lower() for v in (config.get("accept_if_text_contains") or []) if str(v).strip()]
        foreground_bundle_id = config.get("foreground_bundle_id")
        prompts_only = bool(config.get("permission_prompts_only", False))
        results: list[dict] = []
        logger.debug(
            "ios appium: handling permission alerts action=%s max_alerts=%s wait_seconds=%s accept_if=%s foreground=%s",
            action,
            max_alerts,
            wait_seconds,
            accept_if,
            foreground_bundle_id,
        )
        started = time.monotonic()
        deadline = started + wait_seconds
        scan_deadline = deadline + ALERT_SCAN_GRACE_SECONDS
        while len(results) < max_alerts and time.monotonic() <= deadline:
            # Only the target's own alerts are handled; another app in front must not be tapped through.
            if foreground_bundle_id:
                app_state = self.query_app_state(foreground_bundle_id)
                if app_state != APP_STATE_RUNNING_FOREGROUND:
                    logger.debug("ios appium: %s not in foreground (state=%s); alerts left alone", foreground_bundle_id, app_state)
                    results.append({"status": "TARGET_NOT_FOREGROUND", "bundle_id": foreground_bundle_id, "app_state": app_state})
                    break
            result = self._handle_one_permission_alert(action, accept_if, scan_deadline, prompts_only)
            logger.debug(
                "ios appium: permission alert attempt status=%s action=%s button=%s",
                result.get("status"),
                result.get("action"),
                result.get("button"),
            )
            if result["status"] == "NO_ALERT":
                if results:
                    break
                time.sleep(0.2)
                continue
            results.append(result)
            if action == "alert_only" or result["status"] in {"ALERT_UNHANDLED", "ALERT_LEFT_ALONE"}:
                break
            time.sleep(0.2)
        logger.debug("ios appium: permission alert handling finished with %s handled alert(s)", len(results))
        return results or [{"status": "NO_ALERT"}]

    # Handle one alert via switch_to, falling back to an alert element or a recognisable permission prompt.
    def _handle_one_permission_alert(
        self, action: str, accept_if: list[str] | None = None, deadline: float | None = None, prompts_only: bool = False
    ) -> dict:
        try:
            alert = self.driver.switch_to.alert
            text = getattr(alert, "text", "") or ""
        except Exception:
            logger.debug("ios appium: switch_to.alert handling failed, trying element-based alert", exc_info=True)
            return self._handle_element_alert(action, accept_if, deadline, prompts_only)
        if prompts_only and not self._looks_like_permission_prompt(text):
            logger.debug("ios appium: alert via switch_to is not a permission prompt; left alone: %.200r", text)
            return {"status": "ALERT_LEFT_ALONE", "text": text}
        effective = self._effective_alert_action(action, text, accept_if)
        logger.debug("ios appium: alert via switch_to text=%.200r effective_action=%s", text, effective)
        if effective == "alert_only":
            return {"status": "ALERT_PRESENT", "action": effective, "text": text}
        prefer_negative = effective != "accept"
        try:
            button = self._tap_permission_alert_button(prefer_negative, deadline)
        except NoSafeAlertButton as exc:
            logger.debug("ios appium: alert left alone; no safe button among %s", exc.labels)
            return {"status": "ALERT_UNHANDLED", "action": effective, "text": text, "buttons": exc.labels}
        except Exception:
            logger.debug("ios appium: alert button tap failed, using the native alert action", exc_info=True)
            try:
                alert.dismiss() if prefer_negative else alert.accept()
            except Exception:
                logger.debug("ios appium: native alert action failed, trying element-based alert", exc_info=True)
                return self._handle_element_alert(action, accept_if, deadline, prompts_only)
            return {"status": "HANDLED", "action": effective, "text": text, "button": "dismiss" if prefer_negative else "accept"}
        button.update({"status": "HANDLED", "action": effective, "text": text})
        return button

    # Handle an alert found as an alert element, or as page text that reads like a system permission prompt.
    def _handle_element_alert(
        self, action: str, accept_if: list[str] | None, deadline: float | None, prompts_only: bool = False
    ) -> dict:
        try:
            text = self._alert_text()
            looks_like_prompt = self._looks_like_permission_prompt(text)
            if self._alert_element() is None and not looks_like_prompt:
                logger.debug("ios appium: no alert element and page text is not a permission prompt")
                return {"status": "NO_ALERT"}
            if prompts_only and not looks_like_prompt:
                logger.debug("ios appium: alert element is not a permission prompt; left alone: %.200r", text)
                return {"status": "ALERT_LEFT_ALONE", "text": text}
            effective = self._effective_alert_action(action, text, accept_if)
            logger.debug("ios appium: element-based alert text=%.200r effective_action=%s", text, effective)
            if effective == "alert_only":
                try:
                    button = self._find_permission_alert_button(prefer_negative=True, deadline=deadline)
                except NoSafeAlertButton as exc:
                    return {"status": "ALERT_PRESENT", "action": effective, "text": text, "buttons": exc.labels}
                button.pop("element", None)
                return {"status": "ALERT_PRESENT", "action": effective, "button": button}
            try:
                button = self._tap_permission_alert_button(effective != "accept", deadline)
            except NoSafeAlertButton as exc:
                logger.debug("ios appium: alert left alone; no safe button among %s", exc.labels)
                return {"status": "ALERT_UNHANDLED", "action": effective, "text": text, "buttons": exc.labels}
            button.update({"status": "HANDLED", "action": effective, "text": text})
            return button
        except Exception:
            logger.debug("ios appium: no permission alert handled", exc_info=True)
            return {"status": "NO_ALERT"}

    # Return whether page text reads like an iOS permission prompt rather than ordinary screen content.
    @staticmethod
    def _looks_like_permission_prompt(text: str) -> bool:
        lowered = (text or "").replace("’", "'").lower()
        return any(phrase in lowered for phrase in PERMISSION_PROMPT_PHRASES)

    # Return the on-screen alert element, or None when the screen has none.
    def _alert_element(self):
        from selenium.webdriver.common.by import By

        try:
            return self.driver.find_element(By.CLASS_NAME, "XCUIElementTypeAlert")
        except Exception:
            return None

    # Return accept when the alert text matches accept_if, else the configured action.
    @staticmethod
    def _effective_alert_action(action: str, text: str, accept_if: list[str] | None) -> str:
        if accept_if and text and any(needle in text.lower() for needle in accept_if):
            return "accept"
        return action

    # Return the alert's static text, falling back to all static text on screen.
    def _alert_text(self) -> str:
        from selenium.webdriver.common.by import By

        finders = (
            lambda: self.driver.find_element(By.CLASS_NAME, "XCUIElementTypeAlert").find_elements(
                By.CLASS_NAME, "XCUIElementTypeStaticText"
            ),
            lambda: self.driver.find_elements(By.CLASS_NAME, "XCUIElementTypeStaticText"),
        )
        for finder in finders:
            try:
                joined = " ".join(element.text or "" for element in finder()).strip()
            except Exception as exc:
                logger.debug("ios appium: alert text finder failed: %s", exc, exc_info=True)
                continue
            if joined:
                return joined
        return ""

    # Tap the preferred alert button and return its summary.
    def _tap_permission_alert_button(self, prefer_negative: bool, deadline: float | None = None) -> dict:
        button = self._find_permission_alert_button(prefer_negative, deadline)
        logger.debug("ios appium: tapping permission alert button %r", button.get("button"))
        button["element"].click()
        button.pop("element", None)
        return button

    # Find the alert button whose label exactly matches the requested action, reading only button labels.
    def _find_permission_alert_button(self, prefer_negative: bool, deadline: float | None = None) -> dict:
        from selenium.webdriver.common.by import By

        buttons = []
        alert_element = self._alert_element()
        if alert_element is not None:
            try:
                buttons = list(alert_element.find_elements(By.CLASS_NAME, "XCUIElementTypeButton"))
            except Exception:
                logger.debug("ios appium: could not list buttons inside the alert element", exc_info=True)
        scoped = bool(buttons)
        if not scoped:
            buttons = list(self.driver.find_elements(By.CLASS_NAME, "XCUIElementTypeButton"))
        logger.debug(
            "ios appium: searching %s button(s) %s for permission alert (prefer_negative=%s)",
            len(buttons),
            "inside the alert" if scoped else "on the page",
            prefer_negative,
        )
        if not buttons:
            raise AlertButtonsNotFound("No alert button was visible")
        if not scoped and len(buttons) > MAX_UNSCOPED_ALERT_BUTTONS:
            raise AlertScanAborted(f"{len(buttons)} buttons on the page is more than an alert has")
        labelled = []
        for index, button in enumerate(buttons):
            if deadline is not None and time.monotonic() > deadline:
                raise AlertScanAborted("The alert button scan ran out of time")
            try:
                parts = [button.get_attribute(key) for key in ("label", "name", "value")]
            except Exception as exc:
                raise AlertScanAborted(f"The screen changed while reading alert buttons: {exc}") from exc
            labelled.append((index, button, parts))
        preferred = ALERT_NEGATIVE_LABELS if prefer_negative else ALERT_POSITIVE_LABELS
        fallback = ALERT_ACKNOWLEDGE_LABELS if prefer_negative else ALERT_NEGATIVE_LABELS
        for labels in (preferred, fallback):
            for index, button, parts in labelled:
                if {_normalized_label(part) for part in parts if part} & set(labels):
                    label = " ".join(str(part) for part in parts if part).strip()
                    logger.debug("ios appium: matched permission alert button %r", label)
                    return {"button": label, "matched_by": "permission_alert_button", "index": index, "element": button}
        error = NoSafeAlertButton("No alert button is safe to tap for this action")
        error.labels = [" ".join(str(part) for part in parts if part).strip() for _, _, parts in labelled]
        logger.debug("ios appium: no safe permission alert button among %s", error.labels)
        raise error

    # Tap the element with an accessibility ID, optionally clear it and type text.
    @_recovers_from_blocking_prompts
    def set_text_by_accessibility_id(self, accessibility_id: str, text: str, clear_first: bool = True) -> dict:
        if self.driver is None:
            raise RuntimeError("Appium session is not connected")
        from appium.webdriver.common.appiumby import AppiumBy

        logger.debug(
            "ios appium: set text by accessibility_id %r (text length %s, clear_first=%s)",
            accessibility_id,
            len(text),
            clear_first,
        )
        element = self.driver.find_element(AppiumBy.ACCESSIBILITY_ID, accessibility_id)
        element.click()
        if clear_first:
            try:
                element.clear()
            except Exception as exc:
                logger.debug("ios appium: clear() failed for %r, falling back to backspaces: %s", accessibility_id, exc, exc_info=True)
                current = element.get_attribute("value") or ""
                if current:
                    element.send_keys("\b" * len(current))
        element.send_keys(text)
        logger.debug("ios appium: text set on accessibility_id %r", accessibility_id)
        return {"accessibility_id": accessibility_id, "text": text, "clear_first": clear_first}

    # Tap the element with an accessibility ID.
    @_recovers_from_blocking_prompts
    def tap_by_accessibility_id(self, accessibility_id: str) -> dict:
        if self.driver is None:
            raise RuntimeError("Appium session is not connected")
        from appium.webdriver.common.appiumby import AppiumBy

        logger.debug("ios appium: tap by accessibility_id %r", accessibility_id)
        self.driver.find_element(AppiumBy.ACCESSIBILITY_ID, accessibility_id).click()
        logger.debug("ios appium: tapped accessibility_id %r", accessibility_id)
        return {"accessibility_id": accessibility_id, "tapped": True}

    # Type text into the active element, or key by key via keyboard buttons when configured.
    def type_text(self, text: str, config: dict | None = None) -> dict:
        if self.driver is None:
            raise RuntimeError("Appium session is not connected")
        config = config or {}
        method = str(config.get("method") or "active_element_send_keys")
        logger.debug("ios appium: typing text of length %s using method %s", len(text), method)
        if method == "keyboard_buttons":
            import time

            from appium.webdriver.common.appiumby import AppiumBy
            from selenium.webdriver.support.ui import WebDriverWait

            key_map = config.get("key_accessibility_ids") or {}
            template = str(config.get("key_accessibility_id_template") or "{char}")
            return_key = str(config.get("return_key_accessibility_id") or "Return")
            key_timeout = float(config.get("key_timeout_seconds", 6))
            inter_key_delay = float(config.get("inter_key_delay_seconds", 0.15))
            logger.debug(
                "ios appium: keyboard_buttons key_timeout=%ss inter_key_delay=%ss mapped_keys=%s",
                key_timeout,
                inter_key_delay,
                len(key_map),
            )
            taps = []
            for index, char in enumerate(text):
                if char in {"\n", "\r"}:
                    accessibility_id = key_map.get(char) or return_key
                else:
                    accessibility_id = key_map.get(char) or template.format(char=char)
                try:
                    element = WebDriverWait(self.driver, key_timeout).until(
                        lambda driver, aid=accessibility_id: driver.find_element(
                            AppiumBy.ACCESSIBILITY_ID, aid
                        )
                    )
                    element.click()
                except Exception as exc:
                    logger.debug(
                        "ios appium: keyboard key at index %s not tapped within %ss: %s", index, key_timeout, exc, exc_info=True
                    )
                    raise RuntimeError(
                        f"keyboard key {accessibility_id!r} for character {char!r} (index {index}) "
                        f"was not found within {key_timeout:g}s after typing "
                        f"{text[:index]!r}; the on-screen keyboard changed or was dismissed "
                        f"before the full probe text could be entered"
                    ) from exc
                taps.append(
                    {"accessibility_id": accessibility_id, "char": char, "index": index, "tapped": True}
                )
                logger.debug("ios appium: keyboard key at index %s tapped", index)
                if inter_key_delay > 0:
                    time.sleep(inter_key_delay)
            logger.debug("ios appium: keyboard_buttons typed %s key(s)", len(taps))
            return {"method": method, "text": text, "taps": taps}
        active = self.driver.switch_to.active_element
        active.send_keys(text)
        logger.debug("ios appium: sent keys to active element")
        return {"method": method, "text": text}

    # Cycle the keyboard switcher until the expected keyboard markers appear.
    def ensure_keyboard_selected(self, config: dict | None = None) -> dict:
        if self.driver is None:
            raise RuntimeError("Appium session is not connected")
        import time

        config = config or {}
        if not bool(config.get("enabled", True)):
            logger.debug("ios appium: keyboard selection disabled by config")
            return {"status": "SKIPPED", "reason": "keyboard selection is disabled"}
        expected_terms = [str(value) for value in (config.get("expected_source_contains") or []) if value]
        active_markers = [str(value) for value in (config.get("active_marker_contains") or []) if value] or expected_terms
        attempts = int(config.get("attempts", 5))
        settle_seconds = float(config.get("settle_seconds", 0.5))
        switch_button_ids = config.get("switch_button_accessibility_ids") or [
            "Next keyboard",
            "Next Keyboard",
            "Globe",
        ]
        switch_button_labels = config.get("switch_button_label_contains") or [
            "next keyboard",
            "globe",
            "keyboard",
        ]
        logger.debug(
            "ios appium: ensuring keyboard selected markers=%s attempts=%s settle=%ss switch_ids=%s",
            active_markers,
            attempts,
            settle_seconds,
            switch_button_ids,
        )
        if self._keyboard_marker_present(active_markers, switch_button_ids):
            logger.debug("ios appium: keyboard already selected")
            return {"status": "SELECTED", "matched": active_markers, "attempts": []}

        tap_attempts = []
        for attempt in range(1, attempts + 1):
            tapped = self._tap_keyboard_switcher(switch_button_ids, switch_button_labels)
            tapped["attempt"] = attempt
            tap_attempts.append(tapped)
            logger.debug(
                "ios appium: keyboard switch attempt %s/%s tapped=%s matched_by=%s",
                attempt,
                attempts,
                tapped.get("tapped"),
                tapped.get("matched_by"),
            )
            time.sleep(settle_seconds)
            if active_markers and self._keyboard_marker_present(active_markers, switch_button_ids):
                logger.debug("ios appium: keyboard selected after attempt %s", attempt)
                return {"status": "SELECTED", "matched": active_markers, "attempts": tap_attempts}
            if not expected_terms and tapped.get("tapped"):
                logger.debug("ios appium: keyboard switch attempted with no expected markers configured")
                return {"status": "ATTEMPTED", "reason": "no expected_source_contains configured", "attempts": tap_attempts}
        logger.debug("ios appium: keyboard not confirmed after %s attempt(s)", len(tap_attempts))
        return {
            "status": "NOT_CONFIRMED" if expected_terms else "NOT_FOUND",
            "expected_source_contains": expected_terms,
            "attempts": tap_attempts,
        }

    # Tap the keyboard switcher by accessibility ID, else by label.
    def _tap_keyboard_switcher(self, accessibility_ids: list[str], label_contains: list[str]) -> dict:
        for accessibility_id in accessibility_ids:
            try:
                result = self.tap_by_accessibility_id(accessibility_id)
                result["matched_by"] = "accessibility_id"
                return result
            except Exception as exc:
                logger.debug("ios appium: keyboard switcher %r not tappable: %s", accessibility_id, exc, exc_info=True)
                continue
        try:
            result = self.tap_first_button_matching(label_contains, allow_any=False)
            result["matched_by"] = "label_contains"
            return result
        except Exception as exc:
            logger.debug("ios appium: no keyboard switcher matched labels %s: %s", label_contains, exc, exc_info=True)
            return {"tapped": False, "error": str(exc)}

    # Return whether the page source contains any expected term.
    def _page_source_contains_any(self, expected_terms: list[str]) -> bool:
        if not expected_terms:
            return False
        source = self.page_source()
        found = any(term in source for term in expected_terms)
        logger.debug("ios appium: page source (%s chars) contains expected terms=%s", len(source), found)
        return found

    # Return whether an element matching a marker exists, excluding the switcher buttons.
    def _keyboard_marker_present(self, markers: list[str], switcher_names: list[str]) -> bool:
        markers = [str(marker).strip() for marker in markers if str(marker).strip()]
        if not markers:
            return False
        from appium.webdriver.common.appiumby import AppiumBy

        # Escape backslashes and quotes for an iOS predicate string.
        def escape(value: str) -> str:
            return value.replace("\\", "\\\\").replace("'", "\\'")

        match = " OR ".join(
            f"label == '{escape(m)}' OR value == '{escape(m)}' OR name == '{escape(m)}'"
            for m in markers
        )
        predicate = f"({match})"
        excluded = [escape(str(name).strip()) for name in (switcher_names or []) if str(name).strip()]
        if excluded:
            not_switcher = " AND ".join(f"name != '{name}'" for name in excluded)
            predicate = f"({match}) AND ({not_switcher})"
        logger.debug("ios appium: looking for keyboard marker by ios_predicate %s", predicate)
        try:
            self.driver.find_element(AppiumBy.IOS_PREDICATE, predicate)
            logger.debug("ios appium: keyboard marker present")
            return True
        except Exception as exc:
            logger.debug("ios appium: keyboard marker absent: %s", exc, exc_info=True)
            return False

    # Tap the first visible enabled button whose label matches.
    def tap_first_button_matching(
        self,
        label_contains: list[str] | None = None,
        exclude_label_contains: list[str] | None = None,
        allow_any: bool = False,
    ) -> dict:
        return self.tap_first_element_matching(
            label_contains=label_contains,
            exclude_label_contains=exclude_label_contains,
            allow_any=allow_any,
            class_names=["XCUIElementTypeButton"],
            element_label="button",
        )

    # Tap the first interactable element of the classes whose label matches, skipping excluded labels.
    @_recovers_from_blocking_prompts
    def tap_first_element_matching(
        self,
        label_contains: list[str] | None = None,
        exclude_label_contains: list[str] | None = None,
        allow_any: bool = False,
        class_names: list[str] | None = None,
        element_label: str = "element",
    ) -> dict:
        if self.driver is None:
            raise RuntimeError("Appium session is not connected")
        from selenium.webdriver.common.by import By

        include = [value.lower() for value in (label_contains or []) if value]
        exclude = [value.lower() for value in (exclude_label_contains or []) if value]
        classes = class_names or ["XCUIElementTypeButton"]
        logger.debug(
            "ios appium: tap first %s by class_name %s include=%s exclude=%s allow_any=%s",
            element_label,
            classes,
            include,
            exclude,
            allow_any,
        )
        fallback = None
        visible_elements = []
        index = 0
        for class_name in classes:
            elements = self.driver.find_elements(By.CLASS_NAME, class_name)
            logger.debug("ios appium: class_name %s returned %s element(s)", class_name, len(elements))
            for element in elements:
                attrs = self._element_summary(element, index)
                attrs["class_name"] = class_name
                index += 1
                if not self._summary_is_interactable(attrs):
                    continue
                label = " ".join(str(attrs.get(key) or "") for key in ("label", "name", "value")).strip()
                normalized = label.lower()
                if not label:
                    continue
                visible_elements.append(label)
                if any(term in normalized for term in exclude):
                    continue
                if fallback is None:
                    fallback = (element, attrs)
                if include and not any(term in normalized for term in include):
                    continue
                attrs["tap_method"] = self._tap_element_with_fallback(element)
                attrs["matched_by"] = "label_contains" if include else f"first_visible_{element_label}"
                logger.debug("ios appium: tapped %s %r via %s (%s)", element_label, label, attrs["tap_method"], attrs["matched_by"])
                return attrs
        if allow_any and fallback is not None:
            element, attrs = fallback
            attrs["tap_method"] = self._tap_element_with_fallback(element)
            attrs["matched_by"] = "allow_any"
            logger.debug("ios appium: tapped fallback %s via %s (allow_any)", element_label, attrs["tap_method"])
            return attrs
        logger.debug("ios appium: no matching %s; visible candidates %s", element_label, visible_elements[:12])
        if visible_elements:
            note = f" Visible enabled {element_label}s: {visible_elements[:12]}"
        else:
            note = f" No visible enabled {element_label}s were found."
        raise RuntimeError(f"No matching {element_label} was found.{note}")

    # Return an element's readable attributes and rect, skipping unavailable ones.
    def _element_summary(self, element, index: int) -> dict:
        summary = {"index": index}
        for attr in (
            "label",
            "name",
            "value",
            "type",
            "enabled",
            "visible",
            "accessible",
            "placeholderValue",
            "keyboardType",
            "traits",
            "rect",
        ):
            try:
                summary[attr] = element.get_attribute(attr)
            except Exception as exc:
                logger.debug("ios appium: element attribute %s unavailable: %s", attr, exc)
        try:
            summary["rect"] = element.rect
        except Exception as exc:
            logger.debug("ios appium: element rect unavailable: %s", exc)
        return summary

    # Return whether an element summary is visible and enabled.
    def _summary_is_interactable(self, summary: dict) -> bool:
        return self._summary_flag(summary, "visible", default=True) and self._summary_flag(summary, "enabled", default=True)

    # Return whether an element looks like an editable input rather than static text.
    def _summary_is_text_input_candidate(self, class_name: str, summary: dict) -> bool:
        if not self._summary_is_interactable(summary):
            return False
        if class_name != "XCUIElementTypeTextView":
            return True
        traits = str(summary.get("traits") or "").lower()
        if "statictext" in traits or "link" in traits:
            return False
        value = str(summary.get("value") or "")
        has_input_hint = any(summary.get(key) for key in ("name", "label", "placeholderValue"))
        return has_input_hint or len(value) < 80

    # Read a boolean attribute from a summary, using the default when absent.
    def _summary_flag(self, summary: dict, key: str, default: bool) -> bool:
        value = summary.get(key)
        if value is None:
            return default
        return str(value).lower() == "true"

    # Tap the text field matching a selector, or the first interactable one when none is given.
    @_recovers_from_blocking_prompts
    def tap_text_field(self, selector: dict | None = None) -> dict:
        if self.driver is None:
            raise RuntimeError("Appium session is not connected")
        element = None
        element_type = None
        selector = self._normalize_selector(selector)
        if selector:
            from appium.webdriver.common.appiumby import AppiumBy
            from selenium.common.exceptions import NoSuchElementException
            from selenium.webdriver.common.by import By

            strategies = [
                ("accessibility_id", AppiumBy.ACCESSIBILITY_ID),
                ("id", By.ID),
                ("xpath", By.XPATH),
                ("ios_predicate", AppiumBy.IOS_PREDICATE),
                ("ios_class_chain", AppiumBy.IOS_CLASS_CHAIN),
            ]
            for key, strategy in strategies:
                value = selector.get(key)
                if value:
                    logger.debug("ios appium: finding text field by %s %s", key, value)
                    try:
                        element = self.driver.find_element(strategy, value)
                    except NoSuchElementException as exc:
                        raise TextFieldUnavailableError(f"The configured text field was not found: {selector}") from exc
                    element_type = self._element_type(element)
                    break
        else:
            from selenium.webdriver.common.by import By

            for class_name in self.TEXT_FIELD_CLASS_NAMES:
                elements = self.driver.find_elements(By.CLASS_NAME, class_name)
                logger.debug("ios appium: auto text field search class_name %s returned %s element(s)", class_name, len(elements))
                interactable = self._first_interactable(elements, class_name)
                if interactable is not None:
                    element = interactable
                    element_type = class_name
                    break
        if element is None:
            logger.debug("ios appium: no text field found for selector %s", selector or {"auto": True})
            raise TextFieldUnavailableError(f"No visible enabled text field was found. Candidates: {self.describe_text_field_candidates()}")
        tap_method = self._tap_element_with_fallback(element)
        logger.debug("ios appium: tapped text field type %s via %s", element_type, tap_method)
        return {
            "tapped": True,
            "selector": selector or {"auto": True},
            "element_type": element_type,
            "tap_method": tap_method,
            "element": self._element_summary(element, 0),
        }

    # Return the selector without empty values.
    def _normalize_selector(self, selector: dict | None) -> dict:
        if not selector:
            return {}
        return {key: value for key, value in selector.items() if value not in {None, ""}}

    # Return the first element that is a usable text input, or None.
    def _first_interactable(self, elements: list, class_name: str) -> object | None:
        for index, element in enumerate(elements):
            summary = self._element_summary(element, index)
            if self._summary_is_text_input_candidate(class_name, summary):
                return element
        return None

    # Click an element, falling back to a coordinate tap at its centre.
    def _tap_element_with_fallback(self, element) -> str:
        try:
            element.click()
            logger.debug("ios appium: element click succeeded")
            return "element_click"
        except Exception as click_error:
            logger.debug("ios appium: element click failed, trying coordinate tap: %s", click_error, exc_info=True)
            rect = self._element_rect(element)
            if rect is None:
                logger.debug("ios appium: no element rect available for coordinate tap")
                raise RuntimeError(f"Text field was found but element.click() failed: {click_error}") from click_error
            logger.debug("ios appium: coordinate tap at rect %s", rect)
            try:
                self._execute("tap", {"x": int(rect["x"] + rect["width"] / 2), "y": int(rect["y"] + rect["height"] / 2)})
                return "coordinate_tap"
            except Exception as tap_error:
                logger.debug("ios appium: coordinate tap failed: %s", tap_error, exc_info=True)
                raise RuntimeError(
                    "Text field was found but could not be tapped. "
                    f"element.click() failed with: {click_error}; coordinate tap failed with: {tap_error}"
                ) from tap_error

    # Return an element's rect from rect or its x, y, width and height attributes, or None.
    def _element_rect(self, element) -> dict | None:
        try:
            rect = element.rect
            if all(key in rect for key in ("x", "y", "width", "height")):
                return {key: float(rect[key]) for key in ("x", "y", "width", "height")}
        except Exception as exc:
            logger.debug("ios appium: element.rect unavailable, trying attributes: %s", exc, exc_info=True)
        values = {}
        for key in ("x", "y", "width", "height"):
            try:
                values[key] = float(element.get_attribute(key))
            except Exception as exc:
                logger.debug("ios appium: element attribute %s not usable for rect: %s", key, exc, exc_info=True)
                return None
        return values

    # Summarize up to limit text-field elements for diagnostics.
    def describe_text_field_candidates(self, limit: int = 20) -> list[dict]:
        if self.driver is None:
            raise RuntimeError("Appium session is not connected")
        from selenium.webdriver.common.by import By

        candidates = []
        for class_name in self.TEXT_FIELD_CLASS_NAMES:
            for element in self.driver.find_elements(By.CLASS_NAME, class_name):
                summary = self._element_summary(element, len(candidates))
                summary["class_name"] = class_name
                summary["interactable"] = self._summary_is_interactable(summary)
                summary["input_candidate"] = self._summary_is_text_input_candidate(class_name, summary)
                candidates.append(summary)
                if len(candidates) >= limit:
                    logger.debug("ios appium: text field candidate limit %s reached", limit)
                    return candidates
        logger.debug("ios appium: described %s text field candidate(s)", len(candidates))
        return candidates

    # Return an element's type or class name, or None.
    def _element_type(self, element) -> str | None:
        for attr in ("type", "className"):
            try:
                value = element.get_attribute(attr)
                if value:
                    return str(value)
            except Exception as exc:
                logger.debug("ios appium: element attribute %s unavailable for type: %s", attr, exc)
        return None

    # Return the app's Appium state code.
    def query_app_state(self, bundle_id: str) -> int:
        return int(self._execute("queryAppState", {"bundleId": bundle_id}))

    # Read the device clock at whole-second precision for conservative capture alignment.
    def device_time_ms(self) -> int:
        if self.driver is None:
            raise RuntimeError("Appium session is not connected")
        raw = self.driver.get_device_time()
        try:
            value = datetime.fromisoformat(raw)
            if value.tzinfo is None:
                raise ValueError("Device time has no timezone")
            return int(value.timestamp()) * 1000
        except (TypeError, ValueError, OverflowError) as exc:
            raise RuntimeError("Appium returned an invalid device time") from exc

    # Save a device screenshot to a path.
    def screenshot(self, path: Path) -> None:
        if self.driver is None:
            raise RuntimeError("Appium session is not connected")
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        logger.debug("ios appium: saving screenshot to %s", path)
        saved = self.driver.save_screenshot(str(path))
        logger.debug("ios appium: screenshot save returned %s", saved)

    # Pull a folder from the app's Documents, extract it safely into dest and return its files.
    def pull_app_documents(self, bundle_id: str, subpath: str, dest: Path, max_bytes: int) -> list[Path]:
        if self.driver is None:
            raise RuntimeError("Appium session is not connected")
        remote = f"@{bundle_id}:documents/{subpath.strip('/')}"
        logger.debug("ios appium: pulling folder %s to %s (max %s bytes)", remote, dest, max_bytes)
        started = time.monotonic()
        encoded = self.driver.pull_folder(remote)
        raw = base64.b64decode(encoded)
        logger.debug("ios appium: pulled %s bytes from %s in %.2fs", len(raw), remote, time.monotonic() - started)
        if len(raw) > max_bytes:
            logger.debug("ios appium: pulled archive exceeds max_pull_bytes=%s", max_bytes)
            raise RuntimeError(f"Pulled evidence is {len(raw)} bytes, above max_pull_bytes={max_bytes}")
        dest.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=dest) as staging:
            staging_dir = Path(staging)
            safe_extract_zip(io.BytesIO(raw), staging_dir)
            # Appium may zip the folder from its parent, prefixing every entry with the folder's own name.
            entries = list(staging_dir.iterdir())
            root = entries[0] if len(entries) == 1 and entries[0].is_dir() and entries[0].name == Path(subpath).name else staging_dir
            logger.debug("ios appium: extracted %s top-level entr(ies); using root %s", len(entries), root)
            for entry in root.iterdir():
                target = dest / entry.name
                if target.is_dir():
                    shutil.rmtree(target)
                elif target.exists():
                    target.unlink()
                shutil.move(str(entry), target)
                logger.debug("ios appium: moved pulled entry to %s", target)
        pulled = sorted(path for path in dest.rglob("*") if path.is_file())
        logger.debug("ios appium: %s file(s) now under %s", len(pulled), dest)
        return pulled

    # Return the current page source XML.
    def page_source(self) -> str:
        if self.driver is None:
            raise RuntimeError("Appium session is not connected")
        source = self.driver.page_source
        logger.debug("ios appium: page source fetched (%s chars)", len(source) if isinstance(source, str) else None)
        return source
