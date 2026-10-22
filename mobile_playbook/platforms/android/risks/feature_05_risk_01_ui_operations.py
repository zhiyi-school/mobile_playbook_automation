"""Tests whether an accessibility service can operate the app's user interface by clicking its logout control."""

from __future__ import annotations

import json
import logging
import re
import time
import xml.etree.ElementTree as ElementTree
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from mobile_playbook.platforms.android.acquisition.store import STORE_APPS
from mobile_playbook.platforms.android.device.accessibility_attacker import (
    ATTACKER_PACKAGE,
    AccessibilityAttacker,
    AccessibilitySettings,
    AttackEvent,
    attacker_apk_path,
    enabled_components,
    names_attacker_service,
)
from mobile_playbook.platforms.android.device.accessibility_settings import (
    ACCESSIBILITY_SETTINGS_ACTION,
    SettingsLabels,
    enable_service,
    find_service_entry,
    service_switch,
)
from mobile_playbook.platforms.android.device.fingerprint import installed_base_apk_sha256
from mobile_playbook.platforms.android.device.handoff_packages import BROWSER_PACKAGES, LAUNCHER_PACKAGES
from mobile_playbook.platforms.android.device.permission_prompts import deny_permission_prompts
from mobile_playbook.platforms.android.device.permissions import PERMISSION_CONTROLLER_PACKAGES
from mobile_playbook.platforms.android.device.scripted_steps import StepError, find_element, merged_canaries, run_steps
from mobile_playbook.platforms.android.models import AndroidRiskRunResult
from mobile_playbook.platforms.android.risks.base import AndroidRisk

logger = logging.getLogger(__name__)
POLL_SECONDS = 0.5
UNBIND_SECONDS = 5.0
# Without this, Appium's UiAutomation connection makes Android unbind other accessibility services, the attacker's too.
DRIVER_CAPABILITIES = {"appium:disableSuppressAccessibilityService": True}
# The playbook service's patterns, used to check through Appium whether the screen shows a logout control.
LOGOUT_ID_PATTERNS = ("logout", "log_out", "logoff", "log_off", "signout", "sign_out", "btn_logout", "button_logout", "btnLogout", "btnSignOut")
LOGOUT_TEXT_PATTERNS = ("Logout", "Log out", "LOG OUT", "Sign Out", "SIGN OUT", "Sign out", "Signout", "Log Off", "Logoff", "Exit")
LOGOUT_CONTENT_DESC_PATTERNS = ("logout", "log out", "sign out", "signout", "exit")
# Android's BiometricPrompt window, and the Settings activities that confirm the device credential.
AUTH_PROMPT_WINDOW_RE = re.compile(r"Window\{[0-9a-f]+ u\d+ BiometricPrompt\}")
AUTH_ACTIVITY_MARKERS = ("ConfirmDeviceCredential", "ConfirmLock")
RESUMED_ACTIVITY_RE = re.compile(r"\b(?:topResumedActivity|mResumedActivity|ResumedActivity)\s*[=:]\s*ActivityRecord\{\S+ u\d+ ([\w.]+)/([\w.$]+)")
STEPS = ("01", "02", "03", "precondition", "04", "05", "06")


class AndroidAccessibilityUiOperationsRisk(AndroidRisk):
    risk_id = "android-feature-05-risk-01"
    feature_id = "feature-05"
    name = "Android Accessibility UI Operations Test"
    test_case_id = "accessibility_ui_operations"
    test_case_type = "appium_accessibility"
    requires = ["adb", "appium", "accessibility_attacker"]

    # Follow the playbook's accessibility logout attack, preserve evidence, and turn the attacker's service off afterwards.
    def run(self, app_config, global_config, device_client, report_writer):
        started_at = datetime.now().astimezone()
        package = app_config.package_name
        report_dir = report_writer.test_report_dir(app_config.id, self.risk_id, self.test_case_id, platform="android")
        shared = getattr(global_config, "accessibility", None) or {}
        app_cfg = app_config.risks.get(self.risk_id) or {}
        cfg = {**shared, **app_cfg}
        launch_wait = float(cfg.get("launch_wait", 5))
        step_timeout = float(cfg.get("step_timeout_seconds", 10))
        attack_timeout = float(cfg.get("attack_timeout", 15))
        settle = float(cfg.get("settle_seconds", 3))
        labels = _settings_labels(cfg.get("settings_labels"))
        result = AndroidRiskRunResult(
            run_timestamp=report_writer.run_timestamp, timestamp_start=started_at.isoformat(), timestamp_end=None,
            app_id=app_config.id, app_name=app_config.name, package_name=package, risk_id=self.risk_id,
            test_case_id=self.test_case_id, test_case_type=self.test_case_type,
        )
        performed = result.metadata["steps_performed"] = {step: {"outcome": "not_run", "detail": "Not reached."} for step in STEPS}
        result.metadata["coverage"] = "launch_only"
        result.metadata["permission_prompts"] = []
        cleanup = result.metadata["cleanup"] = {"settings_restored": False, "service_off": False, "errors": []}
        evidence_dir = report_dir / "accessibility"
        driver = None
        adb = device_client.adb
        attacker = AccessibilityAttacker(adb, attacker_apk_path(cfg.get("attacker_apk")))
        before: AccessibilitySettings | None = None
        step = "01"
        try:
            result.metadata["installed_base_apk_sha256"] = installed_base_apk_sha256(adb, package)
            before = _without_attacker(attacker.read_settings(), result)
            result.metadata["other_services_enabled"] = len(enabled_components(before.enabled_services))
            driver = device_client.make_driver(extra_capabilities=DRIVER_CAPABILITIES)
            _adb(adb, ["input", "keyevent", "KEYCODE_HOME"])
            evidence_dir.mkdir(parents=True, exist_ok=True)
            performed[step] = {"outcome": "completed", "detail": "Recorded the installed APK hash and the accessibility settings, opened Appium and pressed HOME."}

            step = "02"
            attacker.ensure_installed()
            declared = result.metadata["attacker_service_declared"] = _service_declared(adb)
            performed[step] = {"outcome": "provided", "detail": f"The provided attacker app {ATTACKER_PACKAGE} declares the playbook's accessibility service" + ("." if declared else " (declaration not confirmed by the package manager).")}
            performed["03"] = {"outcome": "provided", "detail": "The provided attacker app's service configuration and logout logic follow the playbook."}

            step = "precondition"
            attacker.prepare(package, timeout=step_timeout)
            verdict = _launch(driver, adb, package, launch_wait, step_timeout, evidence_dir, result)
            if verdict is None:
                steps = app_cfg.get("steps") or []
                if steps:
                    outcome = run_steps(driver, steps, merged_canaries(shared.get("canaries"), app_cfg.get("canaries")), step_timeout)
                    result.metadata["steps"] = outcome.to_dict()
                    result.metadata["coverage"] = "scripted_partial" if outcome.failure is not None else "scripted"
                    _deny_prompts(driver, package, step_timeout, launch_wait, evidence_dir, result)
                verdict = _foreground_verdict(driver, adb, package, result)
            if verdict is None and app_cfg.get("logged_out") and _locator_present(driver, app_cfg["logged_out"], step_timeout):
                verdict = "INCONCLUSIVE (The app was already signed out before the attack)"
            if verdict is None:
                result.metadata["logout_control"] = _logout_control(driver, package, app_cfg.get("logout_control"), step_timeout)
                _screenshot(driver, evidence_dir / "target_before.png", "accessibility_target_before", "App screen before the attack", result)
                performed[step] = {"outcome": "completed", "detail": "The app is in the foreground on the screen tested" + (" after its scripted steps." if app_cfg.get("steps") else ".")}

                step = "04"
                enabled = enable_service(driver, attacker, before, evidence_dir, timeout=max(step_timeout, 20), labels=labels)
                result.metadata["service_enable"] = enabled.to_dict()
                result.evidence.extend(enabled.evidence)
                result.metadata.setdefault("evidence_errors", []).extend(enabled.evidence_errors)
                bound = result.metadata["service_bound"] = _wait_for_bound(attacker, step_timeout)
                _adb(adb, ["input", "keyevent", "KEYCODE_HOME"])
                driver.activate_app(package)
                events = _wait_for_attack(attacker, package, attack_timeout, settle) if bound else []
                _save_events(attacker, evidence_dir, result)
                observation = _observe(driver, adb, package, events, app_cfg.get("logged_out"), step_timeout, bound, result)
                _screenshot(driver, evidence_dir / "target_after.png", "accessibility_target_after", "App screen after the attack", result)
                verdict, rule = _decide(observation)
                result.metadata["verdict_rule"] = rule
                performed[step] = {"outcome": "completed", "detail": f"Service turned on via {enabled.method}; {verdict}"}

                step = "05"
                performed[step] = _view_list(driver, adb, labels, step_timeout, evidence_dir, result)
                step = "06"
                performed[step] = _check_switch(driver, labels, step_timeout, evidence_dir, result)
            else:
                performed[step] = {"outcome": "failed" if verdict.startswith("ERROR") else "skipped", "detail": verdict}
            _finish(result, verdict)
        except Exception as exc:
            logger.debug("%s[%s]: step %s failed: %s", self.risk_id, app_config.id, step, exc, exc_info=True)
            performed[step] = {"outcome": "failed", "detail": str(exc)}
            _finish(result, f"ERROR ({exc})")
        finally:
            _turn_service_off(attacker, before, cleanup)
            if not (cleanup["settings_restored"] and cleanup["service_off"]):
                result.final_status = "FAILED"
                result.verdict = "Inconclusive"
                result.errors.append("The attacker's accessibility service could not be confirmed off: " + "; ".join(cleanup["errors"]))
            if driver is not None:
                try:
                    driver.terminate_app(package)
                except Exception as exc:
                    logger.debug("%s[%s]: target termination failed: %s", self.risk_id, app_config.id, exc, exc_info=True)
                try:
                    driver.quit()
                except Exception as exc:
                    logger.debug("%s[%s]: driver quit failed: %s", self.risk_id, app_config.id, exc, exc_info=True)
            result.timestamp_end = datetime.now().astimezone().isoformat()
            report_writer.write_result(result, report_dir)
        return result


# Run a device shell command and raise on failures rather than treating missing evidence as protection.
def _adb(adb, args: list[str]) -> str:
    code, out, err = adb.run(["shell", *args])
    if code != 0:
        raise RuntimeError(f"adb {' '.join(args)} failed: {err or out}")
    return out or ""


# Add a screenshot to the evidence when Appium saves it, otherwise record why it is missing; evidence never decides
# the verdict.
def _screenshot(driver, path: Path, kind: str, label: str, result: AndroidRiskRunResult) -> None:
    try:
        saved = driver.get_screenshot_as_file(str(path))
    except Exception as exc:
        logger.debug("android accessibility: screenshot %s failed: %s", path.name, exc, exc_info=True)
        saved, reason = False, str(exc)
    else:
        reason = "Appium did not save the file"
    if saved:
        result.evidence.append({"kind": kind, "path": str(path), "label": label})
    else:
        result.metadata.setdefault("evidence_errors", []).append(f"{path.name}: {reason}")


# Build the Settings labels from the configuration, keeping the defaults for any label it leaves out.
def _settings_labels(configured) -> SettingsLabels:
    defaults = SettingsLabels()
    configured = configured or {}
    return SettingsLabels(
        service=configured.get("service") or defaults.service,
        categories=tuple(configured.get("categories") or defaults.categories),
        allow=tuple(configured.get("allow") or defaults.allow),
    )


# Drop an attacker service left enabled by an interrupted run from the settings to restore, so cleanup turns it off.
def _without_attacker(before: AccessibilitySettings, result: AndroidRiskRunResult) -> AccessibilitySettings:
    components = enabled_components(before.enabled_services)
    stale = result.metadata["stale_attacker_service"] = names_attacker_service(components)
    if not stale:
        return before
    others = [component for component in components if not names_attacker_service([component])]
    return AccessibilitySettings(enabled_services=":".join(others), accessibility_enabled=before.accessibility_enabled if others else "0")


# Report whether the package manager lists the attacker's accessibility service, or None when it cannot be asked.
def _service_declared(adb) -> bool | None:
    code, out, _ = adb.run(["shell", "pm", "query-services", "-a", "android.accessibilityservice.AccessibilityService"])
    if code != 0:
        return None
    return f"packageName={ATTACKER_PACKAGE}" in (out or "") or f"{ATTACKER_PACKAGE}/" in (out or "")


# Launch the app, deny any permission dialogs covering it, and return a hand-off verdict unless it is in the foreground.
def _launch(driver, adb, package: str, launch_wait: float, timeout: float, evidence_dir: Path, result: AndroidRiskRunResult) -> str | None:
    driver.activate_app(package)
    time.sleep(launch_wait)
    _deny_prompts(driver, package, timeout, launch_wait, evidence_dir, result)
    return _foreground_verdict(driver, adb, package, result)


# Deny any permission dialogs covering the app, screenshotting each first, and record them in the metadata.
def _deny_prompts(driver, package: str, timeout: float, launch_wait: float, evidence_dir: Path, result: AndroidRiskRunResult) -> None:
    prompts = result.metadata["permission_prompts"]
    offset = len(prompts)
    prompts.extend(deny_permission_prompts(
        driver, package, timeout, launch_wait,
        before_deny=lambda index: _screenshot(driver, evidence_dir / f"permission_prompt_{offset + index + 1}.png", "accessibility_permission_prompt", "System permission dialog before denial", result),
    ))


# Return an inconclusive hand-off classification or an automation error unless the app is in the foreground.
def _foreground_verdict(driver, adb, package: str, result: AndroidRiskRunResult) -> str | None:
    try:
        current = driver.current_package
    except Exception as exc:
        return f"ERROR (Could not determine the foreground app: {exc})"
    result.metadata["foreground_package"] = current
    if current == package:
        return None
    return f"INCONCLUSIVE ({_handoff_reason(current, result)} - the attack was not tested)"


# Describe which kind of app is in front instead of the target.
def _handoff_reason(current: str | None, result: AndroidRiskRunResult) -> str:
    if current in PERMISSION_CONTROLLER_PACKAGES:
        prompts = result.metadata.get("permission_prompts") or []
        title = prompts[-1]["title"] if prompts and prompts[-1]["title"] else None
        return f"A system prompt covered the app: {current}" + (f" ({title})" if title else "")
    if current in STORE_APPS:
        return f"Redirected to {STORE_APPS[current]}"
    if current in BROWSER_PACKAGES:
        return f"Redirected to Browser: {current}"
    if current in LAUNCHER_PACKAGES:
        return "App exited to Home Screen"
    return f"Switched to unknown app: {current}"


# Report whether the locator names an element on the current screen within the timeout.
def _locator_present(driver, locator: dict, timeout: float) -> bool:
    try:
        find_element(driver, locator, timeout)
    except StepError:
        return False
    return True


# Check through Appium, before the service runs, whether the screen shows a logout control the playbook service would
# look for; a configured locator replaces the playbook's patterns.
def _logout_control(driver, package: str, locator: dict | None, timeout: float) -> dict:
    if locator:
        return {"visible": _locator_present(driver, locator, timeout), "match": {"locator": locator}}
    try:
        root = ElementTree.fromstring(driver.page_source)
    except (ElementTree.ParseError, ValueError) as exc:
        logger.debug("android accessibility: page source unreadable: %s", exc)
        return {"visible": None, "match": None}
    ids = {f"{package}:id/{pattern}" for pattern in LOGOUT_ID_PATTERNS}
    texts = [pattern.lower() for pattern in LOGOUT_TEXT_PATTERNS]
    descriptions = list(LOGOUT_CONTENT_DESC_PATTERNS)
    for node in root.iter():
        if node.get("package") not in (None, package):
            continue
        resource_id = node.get("resource-id") or ""
        text = (node.get("text") or "").lower()
        description = (node.get("content-desc") or "").lower()
        if resource_id in ids:
            return {"visible": True, "match": {"resource_id": resource_id}}
        for pattern in texts:
            if pattern in text or pattern in description:
                return {"visible": True, "match": {"text": pattern}}
        for pattern in descriptions:
            if pattern in description:
                return {"visible": True, "match": {"content_description": pattern}}
    return {"visible": False, "match": None}


# Wait for Android to bind the attacker's service, which it does only while Appium leaves accessibility services running.
def _wait_for_bound(attacker: AccessibilityAttacker, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if attacker.is_bound():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(POLL_SECONDS)


# Collect the attacker's events for the app until they stop changing for the settle time after a scan, or the timeout.
def _wait_for_attack(attacker: AccessibilityAttacker, package: str, timeout: float, settle: float) -> list[AttackEvent]:
    deadline = time.monotonic() + timeout
    events: list[AttackEvent] = []
    changed_at = time.monotonic()
    while True:
        latest = [event for event in attacker.events() if event.package == package]
        if len(latest) != len(events):
            events, changed_at = latest, time.monotonic()
        now = time.monotonic()
        if (any(event.event == "scan" for event in events) and now - changed_at >= settle) or now >= deadline:
            return events
        time.sleep(POLL_SECONDS)


# Save the attacker's whole event log as evidence when it can be read.
def _save_events(attacker: AccessibilityAttacker, evidence_dir: Path, result: AndroidRiskRunResult) -> None:
    events = attacker.events()
    path = evidence_dir / "attacker_events.json"
    path.write_text(json.dumps([asdict(event) for event in events], indent=2), encoding="utf-8")
    result.evidence.append({"kind": "accessibility_attacker_events", "path": str(path), "label": "Attacker service event log"})


# Gather what the verdict depends on: the attacker's scans and clicks, any authentication prompt, the logged-out
# locator, and the app still being in front.
def _observe(driver, adb, package: str, events: list[AttackEvent], logged_out: dict | None, timeout: float, bound: bool, result: AndroidRiskRunResult) -> dict:
    clicks = [index for index, event in enumerate(events) if event.event == "click"]
    first_click = clicks[0] if clicks else None
    successes = [event for event in events if event.event == "attack_successful"]
    observation = {
        "bound": bound,
        "scanned": any(event.event == "scan" for event in events),
        "searched": any(event.event == "no_logout_found" for event in events),
        "clicked": first_click is not None,
        "changed": first_click is not None and any(event.event == "scan" for event in events[first_click + 1:]),
        "control_visible": (result.metadata.get("logout_control") or {}).get("visible"),
        "auth_prompt": _auth_prompt(adb),
        "logged_out": None,
        "foreground_reason": None,
    }
    result.metadata["attack"] = {
        "scan_count": sum(event.event == "scan" for event in events),
        "click_count": len(clicks),
        "successes": [{"strategy": event.strategy, "pattern": event.pattern} for event in successes],
        "clicked": [{"view_id": events[index].view_id, "class_name": events[index].class_name, "text": events[index].text} for index in clicks],
    }
    if logged_out:
        observation["logged_out"] = _locator_present(driver, logged_out, timeout)
    if not observation["auth_prompt"]:
        try:
            current = driver.current_package
        except Exception as exc:
            raise RuntimeError(f"Could not determine the foreground app: {exc}") from exc
        result.metadata["foreground_after_attack"] = current
        if current != package:
            observation["foreground_reason"] = _handoff_reason(current, result)
    result.metadata["observation"] = observation
    return observation


# Report whether a biometric prompt window or a device-credential confirmation screen is showing.
def _auth_prompt(adb) -> bool:
    if AUTH_PROMPT_WINDOW_RE.search(_adb(adb, ["dumpsys", "window", "windows"])):
        return True
    match = RESUMED_ACTIVITY_RE.search(_adb(adb, ["dumpsys", "activity", "activities"]))
    return bool(match and any(marker in match[2] for marker in AUTH_ACTIVITY_MARKERS))


# Decide the verdict and the rule that decided it from the observation.
def _decide(observation: dict) -> tuple[str, str | None]:
    if not observation["bound"]:
        return "INCONCLUSIVE (The attacker's service was enabled but Android did not bind it - the attack was not tested)", None
    if not observation["scanned"]:
        return "INCONCLUSIVE (The attacker never scanned the app's windows)", None
    if observation["foreground_reason"]:
        return f"INCONCLUSIVE ({observation['foreground_reason']} while the attacker's service was on)", None
    clicked, logged_out = observation["clicked"], observation["logged_out"]
    if logged_out is True:
        if clicked:
            return "ALLOWED (The attacker clicked the app's logout control and the app showed its signed-out screen)", "logged_out_locator"
        return "INCONCLUSIVE (The app showed its signed-out screen although the attacker clicked nothing)", "logged_out_locator"
    if clicked and observation["auth_prompt"]:
        return "BLOCKED (The attacker's click opened a biometric or device-credential prompt and the app stayed signed in)", "auth_prompt"
    if clicked and logged_out is False:
        return "INCONCLUSIVE (The attacker clicked the app, but it stayed signed in without asking for authentication)", "logged_out_locator"
    if clicked and observation["changed"]:
        return "ALLOWED (The attacker clicked the app's logout control and the app opened a new window)", "click_and_window_change"
    if clicked:
        return "INCONCLUSIVE (The attacker clicked the app, but its windows did not change)", "click_and_window_change"
    if observation["control_visible"] and observation["searched"]:
        return "BLOCKED (Appium saw a logout control, but the attacker's search of the app found none)", "hidden_logout_control"
    return "INCONCLUSIVE (The screen tested has no logout control the attacker looks for)", None


# Show the Accessibility list with the service in it, as playbook step 05 does.
def _view_list(driver, adb, labels: SettingsLabels, timeout: float, evidence_dir: Path, result: AndroidRiskRunResult) -> dict:
    try:
        _adb(adb, ["am", "start", "-a", ACCESSIBILITY_SETTINGS_ACTION])
        entry = find_service_entry(driver, labels, timeout)
    except Exception as exc:
        logger.debug("android accessibility: step 05 failed: %s", exc, exc_info=True)
        return {"outcome": "failed", "detail": str(exc)}
    _screenshot(driver, evidence_dir / "accessibility_list_after.png", "accessibility_list_after", "Accessibility list with the service on", result)
    if entry is None:
        return {"outcome": "failed", "detail": f"{labels.service!r} was not found in Accessibility settings."}
    entry.click()
    return {"outcome": "completed", "detail": f"{labels.service!r} is listed in Accessibility settings."}


# Confirm the service page's switch is on, as playbook step 06 does, without flipping it again.
def _check_switch(driver, labels: SettingsLabels, timeout: float, evidence_dir: Path, result: AndroidRiskRunResult) -> dict:
    try:
        switch = service_switch(driver, timeout)
        checked = None if switch is None else str(switch.get_attribute("checked")).lower()
    except Exception as exc:
        logger.debug("android accessibility: step 06 failed: %s", exc, exc_info=True)
        return {"outcome": "failed", "detail": str(exc)}
    _screenshot(driver, evidence_dir / "accessibility_service_on.png", "accessibility_service_on", "Attacker service switch on", result)
    if checked == "true":
        return {"outcome": "completed", "detail": f"The {labels.service!r} switch is on."}
    return {"outcome": "failed", "detail": "No service switch was found." if switch is None else f"The {labels.service!r} switch reads {checked!r}."}


# Restore the recorded accessibility settings, stop the attacker and confirm its service is no longer enabled or bound.
def _turn_service_off(attacker: AccessibilityAttacker, before: AccessibilitySettings | None, cleanup: dict) -> None:
    if before is not None:
        try:
            attacker.restore_settings(before)
            cleanup["settings_restored"] = True
        except Exception as exc:
            logger.debug("android accessibility: restoring settings failed: %s", exc, exc_info=True)
            cleanup["errors"].append(f"restoring settings failed: {exc}")
    else:
        cleanup["settings_restored"] = True
    # After restoring: Android drops a force-stopped app's service from the enabled list, which restoring would undo.
    try:
        attacker.stop()
    except Exception as exc:
        logger.debug("android accessibility: stopping the attacker failed: %s", exc, exc_info=True)
        cleanup["errors"].append(f"stopping the attacker failed: {exc}")
    deadline = time.monotonic() + UNBIND_SECONDS
    try:
        while True:
            cleanup["service_off"] = not attacker.is_enabled() and not attacker.is_bound()
            if cleanup["service_off"] or time.monotonic() >= deadline:
                break
            time.sleep(POLL_SECONDS)
        if not cleanup["service_off"]:
            cleanup["errors"].append("the service is still enabled or bound")
    except Exception as exc:
        logger.debug("android accessibility: checking the service failed: %s", exc, exc_info=True)
        cleanup["errors"].append(f"checking the service failed: {exc}")


# Set the result's verdict, status and errors from the verdict text.
def _finish(result: AndroidRiskRunResult, verdict: str) -> None:
    result.metadata["verdict"] = verdict
    result.final_status = _status_from_verdict(verdict)
    result.verdict = _security_verdict_from_verdict(verdict)
    if verdict.startswith("ERROR"):
        result.errors.append(verdict)


# Map the observation to the risk's final status.
def _status_from_verdict(verdict: str) -> str:
    for prefix, status in (("ALLOWED", "A11Y_ACTION_PERFORMED"), ("BLOCKED", "A11Y_ACTION_BLOCKED"), ("INCONCLUSIVE", "A11Y_NOT_TESTED"), ("ERROR", "FAILED")):
        if verdict.startswith(prefix):
            return status
    return "UNKNOWN"


# Map the observation to the shared security verdict.
def _security_verdict_from_verdict(verdict: str) -> str:
    if verdict.startswith("ALLOWED"):
        return "At Risk"
    if verdict.startswith("BLOCKED"):
        return "Reduced Risk"
    return "Inconclusive"
