from __future__ import annotations

from pathlib import Path

from mobile_playbook.platforms.ios.models import InstallResult


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
        try:
            self.driver = webdriver.Remote(self.device_config.appium_server_url, options=options)
        except Exception as exc:
            raise RuntimeError(
                f"failed to start Appium session at {self.device_config.appium_server_url}: {exc}"
            ) from exc
        return self

    def quit(self) -> None:
        if self.driver is not None:
            self.driver.quit()
            self.driver = None

    def _execute(self, command: str, args: dict):
        if self.driver is None:
            raise RuntimeError("Appium session is not connected")
        return self.driver.execute_script(f"mobile: {command}", args)

    def is_installed(self, bundle_id: str) -> bool:
        return bool(self._execute("isAppInstalled", {"bundleId": bundle_id}))

    def remove_app(self, bundle_id: str) -> bool:
        return bool(self._execute("removeApp", {"bundleId": bundle_id}))

    def terminate_app(self, bundle_id: str) -> bool:
        return bool(self._execute("terminateApp", {"bundleId": bundle_id}))

    def install_app(self, ipa_path: Path, timeout_ms: int) -> InstallResult:
        try:
            self._execute("installApp", {"app": str(ipa_path), "timeout": timeout_ms})
            return InstallResult(status="INSTALLED", ipa_path=Path(ipa_path))
        except Exception as exc:
            return InstallResult(status="INSTALL_FAILED", ipa_path=Path(ipa_path), errors=[str(exc)])

    def launch_app(self, bundle_id: str) -> dict:
        return {"result": self._execute("launchApp", {"bundleId": bundle_id})}

    def handle_permission_alerts(self, config: dict | None = None) -> list[dict]:
        if self.driver is None:
            raise RuntimeError("Appium session is not connected")
        config = config or {}
        if not bool(config.get("enabled", True)):
            return [{"status": "SKIPPED", "reason": "permission alert handling is disabled"}]

        import time

        max_alerts = int(config.get("max_alerts", 3))
        wait_seconds = float(config.get("wait_seconds", 2))
        action = str(config.get("action", "dismiss")).lower()
        action = action if action in {"dismiss", "accept", "alert_only"} else "dismiss"
        results: list[dict] = []
        deadline = time.monotonic() + wait_seconds
        while len(results) < max_alerts and time.monotonic() <= deadline:
            result = self._handle_one_permission_alert(action)
            if result["status"] == "NO_ALERT":
                if results:
                    break
                time.sleep(0.2)
                continue
            results.append(result)
            if action == "alert_only":
                break
            time.sleep(0.2)
        return results or [{"status": "NO_ALERT"}]

    def _handle_one_permission_alert(self, action: str) -> dict:
        try:
            alert = self.driver.switch_to.alert
            text = getattr(alert, "text", "") or ""
            if action == "alert_only":
                return {"status": "ALERT_PRESENT", "action": action, "text": text}
            if action == "accept":
                alert.accept()
                return {"status": "HANDLED", "action": action, "text": text, "button": "accept"}
            try:
                button = self._tap_permission_alert_button(prefer_negative=True)
                button.update({"status": "HANDLED", "action": action, "text": text})
                return button
            except Exception:
                alert.dismiss()
                return {"status": "HANDLED", "action": action, "text": text, "button": "dismiss"}
        except Exception:
            try:
                if action == "alert_only":
                    button = self._find_permission_alert_button(prefer_negative=True)
                    return {"status": "ALERT_PRESENT", "action": action, "button": button}
                button = self._tap_permission_alert_button(prefer_negative=(action != "accept"))
                button.update({"status": "HANDLED", "action": action})
                return button
            except Exception:
                return {"status": "NO_ALERT"}

    def _tap_permission_alert_button(self, prefer_negative: bool) -> dict:
        button = self._find_permission_alert_button(prefer_negative)
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
        for labels in (preferred, fallback):
            for button, summary in summaries:
                label = " ".join(str(summary.get(key) or "") for key in ("label", "name", "value")).strip()
                normalized = label.lower()
                if label and any(candidate in normalized for candidate in labels):
                    summary["button"] = label
                    summary["matched_by"] = "permission_alert_button"
                    summary["element"] = button
                    return summary
        raise RuntimeError("No permission alert button was found")

    def set_text_by_accessibility_id(self, accessibility_id: str, text: str, clear_first: bool = True) -> dict:
        if self.driver is None:
            raise RuntimeError("Appium session is not connected")
        from appium.webdriver.common.appiumby import AppiumBy

        element = self.driver.find_element(AppiumBy.ACCESSIBILITY_ID, accessibility_id)
        element.click()
        if clear_first:
            try:
                element.clear()
            except Exception:
                current = element.get_attribute("value") or ""
                if current:
                    element.send_keys("\b" * len(current))
        element.send_keys(text)
        return {"accessibility_id": accessibility_id, "text": text, "clear_first": clear_first}

    def tap_by_accessibility_id(self, accessibility_id: str) -> dict:
        if self.driver is None:
            raise RuntimeError("Appium session is not connected")
        from appium.webdriver.common.appiumby import AppiumBy

        self.driver.find_element(AppiumBy.ACCESSIBILITY_ID, accessibility_id).click()
        return {"accessibility_id": accessibility_id, "tapped": True}

    def type_text(self, text: str, config: dict | None = None) -> dict:
        if self.driver is None:
            raise RuntimeError("Appium session is not connected")
        config = config or {}
        method = str(config.get("method") or "active_element_send_keys")
        if method == "keyboard_buttons":
            key_map = config.get("key_accessibility_ids") or {}
            template = str(config.get("key_accessibility_id_template") or "{char}")
            return_key = str(config.get("return_key_accessibility_id") or "Return")
            taps = []
            for char in text:
                if char in {"\n", "\r"}:
                    accessibility_id = key_map.get(char) or return_key
                else:
                    accessibility_id = key_map.get(char) or template.format(char=char)
                taps.append(self.tap_by_accessibility_id(accessibility_id))
            return {"method": method, "text": text, "taps": taps}
        active = self.driver.switch_to.active_element
        active.send_keys(text)
        return {"method": method, "text": text}

    def ensure_keyboard_selected(self, config: dict | None = None) -> dict:
        if self.driver is None:
            raise RuntimeError("Appium session is not connected")
        import time

        config = config or {}
        if not bool(config.get("enabled", True)):
            return {"status": "SKIPPED", "reason": "keyboard selection is disabled"}
        expected_terms = [str(value) for value in (config.get("expected_source_contains") or []) if value]
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
        if self._page_source_contains_any(expected_terms):
            return {"status": "SELECTED", "matched": expected_terms, "attempts": []}

        tap_attempts = []
        for attempt in range(1, attempts + 1):
            tapped = self._tap_keyboard_switcher(switch_button_ids, switch_button_labels)
            tapped["attempt"] = attempt
            tap_attempts.append(tapped)
            time.sleep(settle_seconds)
            if expected_terms and self._page_source_contains_any(expected_terms):
                return {"status": "SELECTED", "matched": expected_terms, "attempts": tap_attempts}
            if not expected_terms and tapped.get("tapped"):
                return {"status": "ATTEMPTED", "reason": "no expected_source_contains configured", "attempts": tap_attempts}
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
            except Exception:
                continue
        try:
            result = self.tap_first_button_matching(label_contains, allow_any=False)
            result["matched_by"] = "label_contains"
            return result
        except Exception as exc:
            return {"tapped": False, "error": str(exc)}

    def _page_source_contains_any(self, expected_terms: list[str]) -> bool:
        if not expected_terms:
            return False
        source = self.page_source()
        return any(term in source for term in expected_terms)

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
        fallback = None
        visible_elements = []
        index = 0
        for class_name in classes:
            elements = self.driver.find_elements(By.CLASS_NAME, class_name)
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
                return attrs
        if allow_any and fallback is not None:
            element, attrs = fallback
            attrs["tap_method"] = self._tap_element_with_fallback(element)
            attrs["matched_by"] = "allow_any"
            return attrs
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
            except Exception:
                pass
        try:
            summary["rect"] = element.rect
        except Exception:
            pass
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
                    element = self.driver.find_element(strategy, value)
                    element_type = self._element_type(element)
                    break
        else:
            from selenium.webdriver.common.by import By

            for class_name in self.TEXT_FIELD_CLASS_NAMES:
                elements = self.driver.find_elements(By.CLASS_NAME, class_name)
                interactable = self._first_interactable(elements, class_name)
                if interactable is not None:
                    element = interactable
                    element_type = class_name
                    break
        if element is None:
            raise RuntimeError(f"No visible enabled text field was found. Candidates: {self.describe_text_field_candidates()}")
        tap_method = self._tap_element_with_fallback(element)
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
            return "element_click"
        except Exception as click_error:
            rect = self._element_rect(element)
            if rect is None:
                raise RuntimeError(f"Text field was found but element.click() failed: {click_error}") from click_error
            try:
                self._execute("tap", {"x": int(rect["x"] + rect["width"] / 2), "y": int(rect["y"] + rect["height"] / 2)})
                return "coordinate_tap"
            except Exception as tap_error:
                raise RuntimeError(
                    "Text field was found but could not be tapped. "
                    f"element.click() failed with: {click_error}; coordinate tap failed with: {tap_error}"
                ) from tap_error

    def _element_rect(self, element) -> dict | None:
        try:
            rect = element.rect
            if all(key in rect for key in ("x", "y", "width", "height")):
                return {key: float(rect[key]) for key in ("x", "y", "width", "height")}
        except Exception:
            pass
        values = {}
        for key in ("x", "y", "width", "height"):
            try:
                values[key] = float(element.get_attribute(key))
            except Exception:
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
                    return candidates
        return candidates

    def _element_type(self, element) -> str | None:
        for attr in ("type", "className"):
            try:
                value = element.get_attribute(attr)
                if value:
                    return str(value)
            except Exception:
                pass
        return None

    def query_app_state(self, bundle_id: str) -> int:
        return int(self._execute("queryAppState", {"bundleId": bundle_id}))

    def screenshot(self, path: Path) -> None:
        if self.driver is None:
            raise RuntimeError("Appium session is not connected")
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.driver.save_screenshot(str(path))

    def page_source(self) -> str:
        if self.driver is None:
            raise RuntimeError("Appium session is not connected")
        return self.driver.page_source
