from __future__ import annotations

import base64
import io
import logging
import shutil
import tempfile
import time
from pathlib import Path

from mobile_playbook.logging_setup import redacted
from mobile_playbook.platforms.ios.ipa.unpacker import safe_extract_zip
from mobile_playbook.platforms.ios.models import InstallResult

logger = logging.getLogger(__name__)

UNTRUSTED_DEVELOPER_CERT_MARKERS = (
    "developer app certificate is not trusted",
    "profile has not been explicitly trusted",
)


def is_untrusted_developer_cert_error(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in UNTRUSTED_DEVELOPER_CERT_MARKERS)


class AppiumDeviceClient:
    TEXT_FIELD_CLASS_NAMES = [
        "XCUIElementTypeTextField",
        "XCUIElementTypeSearchField",
        "XCUIElementTypeTextView",
        "XCUIElementTypeSecureTextField",
    ]

    def __init__(self, device_config):
        self.device_config = device_config
        self.driver = None

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

    def quit(self) -> None:
        if self.driver is not None:
            logger.debug("ios appium: quitting session %s", getattr(self.driver, "session_id", None))
            self.driver.quit()
            self.driver = None
            logger.debug("ios appium: session quit")
        else:
            logger.debug("ios appium: quit requested with no active session")

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

    def is_installed(self, bundle_id: str) -> bool:
        return bool(self._execute("isAppInstalled", {"bundleId": bundle_id}))

    def remove_app(self, bundle_id: str) -> bool:
        return bool(self._execute("removeApp", {"bundleId": bundle_id}))

    def remove_app_verified(self, bundle_id: str, timeout_seconds: float = 20.0) -> dict:
        """Remove, then poll until the app is actually gone."""
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

    def terminate_app(self, bundle_id: str) -> bool:
        return bool(self._execute("terminateApp", {"bundleId": bundle_id}))

    def install_app(self, ipa_path: Path, timeout_ms: int) -> InstallResult:
        # Resolve to an absolute path before handing it to Appium: the Appium server is a
        # separate process, and a relative path would be resolved against *its* working
        # directory (whatever that happened to be when it was started), not ours.
        ipa_path = Path(ipa_path).expanduser().resolve()
        logger.debug("ios appium: installing %s (timeout %sms)", ipa_path, timeout_ms)
        try:
            self._execute("installApp", {"app": str(ipa_path), "timeout": timeout_ms})
            logger.debug("ios appium: installed %s", ipa_path)
            return InstallResult(status="INSTALLED", ipa_path=ipa_path)
        except Exception as exc:
            logger.debug("ios appium: install of %s failed: %s", ipa_path, exc, exc_info=True)
            return InstallResult(status="INSTALL_FAILED", ipa_path=ipa_path, errors=[str(exc)])

    def launch_app(self, bundle_id: str) -> dict:
        return {"result": self._execute("launchApp", {"bundleId": bundle_id})}

    def activate_app(self, bundle_id: str) -> dict:
        return {"result": self._execute("activateApp", {"bundleId": bundle_id})}

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

    def tap_switch_by_label(self, labels: list[str], timeout_seconds: float):
        logger.debug("ios appium: tap switch by labels %s", labels)
        element = self.find_switch_by_label(labels, timeout_seconds)
        return self._tap_element_with_fallback(element)

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

    def element_value_by_label(self, labels: list[str]) -> str | None:
        try:
            element = self.find_element_by_label(labels, 1)
        except Exception as exc:
            logger.debug("ios appium: no element for labels %s when reading value: %s", labels, exc, exc_info=True)
            return None
        value = element.get_attribute("value")
        logger.debug("ios appium: element value for labels %s is %s", labels, "absent" if value is None else "present")
        return str(value) if value is not None else None

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

    def unlock(self) -> dict:
        logger.debug("ios appium: checking device lock state")
        was_locked = self.driver.is_locked()
        self.driver.unlock()  # no-ops on its own if the device wasn't actually locked
        logger.debug("ios appium: unlock issued, was_locked=%s", was_locked)
        return {"was_locked": was_locked}

    def open_url(self, url: str, bundle_id: str = "com.apple.mobilesafari") -> dict:
        logger.debug("ios appium: opening deep link %s in %s", str(url).split("?", 1)[0][:200], bundle_id)
        # mobile: deepLink avoids driving Safari's own address-bar UI directly.
        return {"result": self._execute("deepLink", {"url": url, "bundleId": bundle_id})}

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
        results: list[dict] = []
        logger.debug(
            "ios appium: handling permission alerts action=%s max_alerts=%s wait_seconds=%s accept_if=%s",
            action,
            max_alerts,
            wait_seconds,
            accept_if,
        )
        deadline = time.monotonic() + wait_seconds
        while len(results) < max_alerts and time.monotonic() <= deadline:
            result = self._handle_one_permission_alert(action, accept_if)
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
            if action == "alert_only":
                break
            time.sleep(0.2)
        logger.debug("ios appium: permission alert handling finished with %s handled alert(s)", len(results))
        return results or [{"status": "NO_ALERT"}]

    def _handle_one_permission_alert(self, action: str, accept_if: list[str] | None = None) -> dict:
        try:
            alert = self.driver.switch_to.alert
            text = getattr(alert, "text", "") or ""
            effective = self._effective_alert_action(action, text, accept_if)
            logger.debug("ios appium: alert via switch_to text=%.200r effective_action=%s", text, effective)
            if effective == "alert_only":
                return {"status": "ALERT_PRESENT", "action": effective, "text": text}
            if effective == "accept":
                try:
                    button = self._tap_permission_alert_button(prefer_negative=False)
                    button.update({"status": "HANDLED", "action": effective, "text": text})
                    return button
                except Exception:
                    logger.debug("ios appium: accept button tap failed, using alert.accept()", exc_info=True)
                    alert.accept()
                    return {"status": "HANDLED", "action": effective, "text": text, "button": "accept"}
            try:
                button = self._tap_permission_alert_button(prefer_negative=True)
                button.update({"status": "HANDLED", "action": effective, "text": text})
                return button
            except Exception:
                logger.debug("ios appium: negative button tap failed, using alert.dismiss()", exc_info=True)
                alert.dismiss()
                return {"status": "HANDLED", "action": effective, "text": text, "button": "dismiss"}
        except Exception:
            logger.debug("ios appium: switch_to.alert handling failed, trying element-based alert", exc_info=True)
            try:
                text = self._alert_text()
                effective = self._effective_alert_action(action, text, accept_if)
                logger.debug("ios appium: element-based alert text=%.200r effective_action=%s", text, effective)
                if effective == "alert_only":
                    button = self._find_permission_alert_button(prefer_negative=True)
                    return {"status": "ALERT_PRESENT", "action": effective, "button": button}
                button = self._tap_permission_alert_button(prefer_negative=(effective != "accept"))
                button.update({"status": "HANDLED", "action": effective, "text": text})
                return button
            except Exception:
                logger.debug("ios appium: no permission alert handled", exc_info=True)
                return {"status": "NO_ALERT"}

    @staticmethod
    def _effective_alert_action(action: str, text: str, accept_if: list[str] | None) -> str:
        if accept_if and text and any(needle in text.lower() for needle in accept_if):
            return "accept"
        return action

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

    def _tap_permission_alert_button(self, prefer_negative: bool) -> dict:
        button = self._find_permission_alert_button(prefer_negative)
        logger.debug("ios appium: tapping permission alert button %r", button.get("button"))
        button["element"].click()
        button.pop("element", None)
        return button

    def _find_permission_alert_button(self, prefer_negative: bool) -> dict:
        from selenium.webdriver.common.by import By

        negative_labels = (
            "don't allow",
            "don’t allow",
            "dont allow",
            "ask app not to track",
            "ask not to track",
            "not now",
            "cancel",
            "deny",
            "deny permission",
            "no",
        )
        positive_labels = (
            "allow while using app",
            "allow once",
            "allow",
            "ok",
            "continue",
        )
        preferred = negative_labels if prefer_negative else positive_labels
        fallback = positive_labels if prefer_negative else negative_labels
        buttons = self.driver.find_elements(By.CLASS_NAME, "XCUIElementTypeButton")
        summaries = [(button, self._element_summary(button, index)) for index, button in enumerate(buttons)]
        logger.debug(
            "ios appium: searching %s button(s) by class_name for permission alert (prefer_negative=%s)",
            len(summaries),
            prefer_negative,
        )
        for labels in (preferred, fallback):
            for button, summary in summaries:
                label = " ".join(str(summary.get(key) or "") for key in ("label", "name", "value")).strip()
                normalized = label.lower()
                if not label:
                    continue
                if labels is positive_labels and any(candidate in normalized for candidate in negative_labels):
                    continue
                if any(candidate in normalized for candidate in labels):
                    summary["button"] = label
                    summary["matched_by"] = "permission_alert_button"
                    summary["element"] = button
                    logger.debug("ios appium: matched permission alert button %r", label)
                    return summary
        logger.debug("ios appium: no permission alert button matched among %s button(s)", len(summaries))
        raise RuntimeError("No permission alert button was found")

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

    def tap_by_accessibility_id(self, accessibility_id: str) -> dict:
        if self.driver is None:
            raise RuntimeError("Appium session is not connected")
        from appium.webdriver.common.appiumby import AppiumBy

        logger.debug("ios appium: tap by accessibility_id %r", accessibility_id)
        self.driver.find_element(AppiumBy.ACCESSIBILITY_ID, accessibility_id).click()
        logger.debug("ios appium: tapped accessibility_id %r", accessibility_id)
        return {"accessibility_id": accessibility_id, "tapped": True}

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

    def _page_source_contains_any(self, expected_terms: list[str]) -> bool:
        if not expected_terms:
            return False
        source = self.page_source()
        found = any(term in source for term in expected_terms)
        logger.debug("ios appium: page source (%s chars) contains expected terms=%s", len(source), found)
        return found

    def _keyboard_marker_present(self, markers: list[str], switcher_names: list[str]) -> bool:
        markers = [str(marker).strip() for marker in markers if str(marker).strip()]
        if not markers:
            return False
        from appium.webdriver.common.appiumby import AppiumBy

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

    def _summary_is_interactable(self, summary: dict) -> bool:
        return self._summary_flag(summary, "visible", default=True) and self._summary_flag(summary, "enabled", default=True)

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

    def _summary_flag(self, summary: dict, key: str, default: bool) -> bool:
        value = summary.get(key)
        if value is None:
            return default
        return str(value).lower() == "true"

    def tap_text_field(self, selector: dict | None = None) -> dict:
        if self.driver is None:
            raise RuntimeError("Appium session is not connected")
        element = None
        element_type = None
        selector = self._normalize_selector(selector)
        if selector:
            from appium.webdriver.common.appiumby import AppiumBy
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
                    element = self.driver.find_element(strategy, value)
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
            raise RuntimeError(f"No visible enabled text field was found. Candidates: {self.describe_text_field_candidates()}")
        tap_method = self._tap_element_with_fallback(element)
        logger.debug("ios appium: tapped text field type %s via %s", element_type, tap_method)
        return {
            "tapped": True,
            "selector": selector or {"auto": True},
            "element_type": element_type,
            "tap_method": tap_method,
            "element": self._element_summary(element, 0),
        }

    def _normalize_selector(self, selector: dict | None) -> dict:
        if not selector:
            return {}
        return {key: value for key, value in selector.items() if value not in {None, ""}}

    def _first_interactable(self, elements: list, class_name: str) -> object | None:
        for index, element in enumerate(elements):
            summary = self._element_summary(element, index)
            if self._summary_is_text_input_candidate(class_name, summary):
                return element
        return None

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

    def _element_type(self, element) -> str | None:
        for attr in ("type", "className"):
            try:
                value = element.get_attribute(attr)
                if value:
                    return str(value)
            except Exception as exc:
                logger.debug("ios appium: element attribute %s unavailable for type: %s", attr, exc)
        return None

    def query_app_state(self, bundle_id: str) -> int:
        return int(self._execute("queryAppState", {"bundleId": bundle_id}))

    def screenshot(self, path: Path) -> None:
        if self.driver is None:
            raise RuntimeError("Appium session is not connected")
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        logger.debug("ios appium: saving screenshot to %s", path)
        saved = self.driver.save_screenshot(str(path))
        logger.debug("ios appium: screenshot save returned %s", saved)

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

    def page_source(self) -> str:
        if self.driver is None:
            raise RuntimeError("Appium session is not connected")
        source = self.driver.page_source
        logger.debug("ios appium: page source fetched (%s chars)", len(source) if isinstance(source, str) else None)
        return source
