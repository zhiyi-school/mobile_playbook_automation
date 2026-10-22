"""Runs the playbook's accessibility event capture test, checking whether Event Spy records what is typed into an app."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from mobile_playbook.platforms.android.acquisition.store import STORE_APPS
from mobile_playbook.platforms.android.device.accessibility_service import (
    AccessibilitySettings,
    enabled_components,
    remove_attacker_app,
    turn_service_off,
)
from mobile_playbook.platforms.android.device.accessibility_settings import (
    SettingsLabels,
    confirm_switch_on,
    enable_service,
    show_service_listed,
)
from mobile_playbook.platforms.android.device.event_spy import EVENT_SPY_APP, EVENT_SPY_PACKAGE, CapturedEvent, EventSpy
from mobile_playbook.platforms.android.device.fingerprint import installed_base_apk_sha256
from mobile_playbook.platforms.android.device.handoff_packages import BROWSER_PACKAGES, LAUNCHER_PACKAGES
from mobile_playbook.platforms.android.device.permission_prompts import deny_permission_prompts
from mobile_playbook.platforms.android.device.permissions import PERMISSION_CONTROLLER_PACKAGES
from mobile_playbook.platforms.android.device.phone_role import phone_model
from mobile_playbook.platforms.android.device.scripted_steps import merged_canaries, run_steps
from mobile_playbook.platforms.android.models import AndroidRiskRunResult
from mobile_playbook.platforms.android.risks.base import AndroidRisk

logger = logging.getLogger(__name__)
POLL_SECONDS = 0.5
# Without this, Appium's UiAutomation connection makes Android unbind other accessibility services, Event Spy's too.
DRIVER_CAPABILITIES = {"appium:disableSuppressAccessibilityService": True}
CAPTURE_EVENT_TYPES = ("FOCUS", "TEXT_CHANGE", "CLICK")
# Apps reformat what is typed, such as grouping a card number, so values are compared without separators or case.
SEPARATORS_RE = re.compile(r"[\s\-/.()]")
# Android reports a password field's text as these characters, one per character typed.
MASK_CHARACTERS = frozenset("•*●")
STEPS = ("01", "02", "03", "04", "05", "06")


class AndroidAccessibilityActivityCaptureRisk(AndroidRisk):
    risk_id = "android-feature-05-risk-02"
    feature_id = "feature-05"
    name = "Android Accessibility Activity Capture Test"
    test_case_id = "accessibility_activity_capture"
    test_case_type = "appium_accessibility"
    requires = ["adb", "appium", "event_spy_attacker"]

    # Follow the playbook's event capture steps with Event Spy, preserve evidence, then turn its service off, clear
    # what it captured and uninstall it.
    def run(self, app_config, global_config, device_client, report_writer):
        started_at = datetime.now().astimezone()
        package = app_config.package_name
        report_dir = report_writer.test_report_dir(app_config.id, self.risk_id, self.test_case_id, platform="android")
        shared = getattr(global_config, "accessibility", None) or {}
        app_cfg = app_config.risks.get(self.risk_id) or {}
        cfg = {**shared, **app_cfg}
        launch_wait = float(cfg.get("launch_wait", 5))
        step_timeout = float(cfg.get("step_timeout_seconds", 10))
        capture_timeout = float(cfg.get("attack_timeout", 15))
        settle = float(cfg.get("settle_seconds", 3))
        labels = _settings_labels(cfg.get("settings_labels"))
        canaries = merged_canaries(shared.get("canaries"), app_cfg.get("canaries"))
        result = AndroidRiskRunResult(
            run_timestamp=report_writer.run_timestamp, timestamp_start=started_at.isoformat(), timestamp_end=None,
            app_id=app_config.id, app_name=app_config.name, package_name=package, risk_id=self.risk_id,
            test_case_id=self.test_case_id, test_case_type=self.test_case_type,
        )
        performed = result.metadata["steps_performed"] = {step: {"outcome": "not_run", "detail": "Not reached."} for step in STEPS}
        result.metadata["coverage"] = "launch_only"
        result.metadata["permission_prompts"] = []
        cleanup = result.metadata["cleanup"] = {"settings_restored": False, "service_off": False, "data_cleared": False, "attacker_uninstalled": False, "errors": []}
        evidence_dir = report_dir / "accessibility"
        driver = None
        adb = device_client.adb
        spy = EventSpy(adb, EVENT_SPY_APP.apk_path(cfg.get("event_spy_apk")))
        before: AccessibilitySettings | None = None
        step = "01"
        try:
            result.metadata["device"] = _device(adb)
            result.metadata["installed_base_apk_sha256"] = installed_base_apk_sha256(adb, package)
            before = _without_spy(spy.read_settings(), result)
            result.metadata["other_services_enabled"] = len(enabled_components(before.enabled_services))
            driver = device_client.make_driver(extra_capabilities=DRIVER_CAPABILITIES)
            _adb(adb, ["input", "keyevent", "KEYCODE_HOME"])
            evidence_dir.mkdir(parents=True, exist_ok=True)
            performed[step] = {"outcome": "completed", "detail": "Recorded the device, the installed APK hash and the accessibility settings, opened Appium and pressed HOME."}

            step = "02"
            result.metadata["event_spy_apk_sha256"] = _file_sha256(spy.apk)
            spy.ensure_installed()
            # pm clear plays the app's Clear Logs button, so only this run's events are read.
            spy.clear()
            declared = result.metadata["attacker_service_declared"] = _service_declared(adb)
            performed[step] = {"outcome": "provided", "detail": f"The provided malicious app {EVENT_SPY_PACKAGE} declares the playbook's accessibility service with BIND_ACCESSIBILITY_SERVICE" + ("." if declared else " (declaration not confirmed by the package manager).")}
            performed["03"] = {"outcome": "provided", "detail": "Event Spy's service listens for focus, text-change and click events and saves them to its local database, as the playbook defines."}

            step = "04"
            enabled = enable_service(driver, spy, before, evidence_dir, timeout=max(step_timeout, 20), labels=labels)
            result.metadata["service_enable"] = enabled.to_dict()
            result.evidence.extend(enabled.evidence)
            result.metadata.setdefault("evidence_errors", []).extend(enabled.evidence_errors)
            bound = result.metadata["service_bound"] = spy.wait_until_bound(step_timeout)
            _adb(adb, ["input", "keyevent", "KEYCODE_HOME"])
            verdict = None
            if not bound:
                verdict = "INCONCLUSIVE (Event Spy's service was enabled but Android did not bind it - the capture was not tested)"
            else:
                verdict = _launch(driver, adb, package, launch_wait, step_timeout, evidence_dir, result)
            if verdict is None:
                typed = _use_app(driver, adb, package, app_cfg.get("steps") or [], canaries, step_timeout, launch_wait, evidence_dir, result)
                _screenshot(driver, evidence_dir / "target_after_input.png", "accessibility_target_after", "App screen after the input", result)
                events = _wait_for_capture(spy, package, capture_timeout, settle)
                _save_events(events, evidence_dir, result)
                _show_event_spy(driver, launch_wait, evidence_dir, result)
                verdict, rule = _decide(_observe(events, typed, result))
                result.metadata["verdict_rule"] = rule
                performed[step] = {"outcome": "completed", "detail": f"Service turned on via {enabled.method} and the app used; {verdict}"}

                step = "05"
                performed[step] = show_service_listed(driver, adb, labels, step_timeout, lambda: _screenshot(driver, evidence_dir / "accessibility_list_after.png", "accessibility_list_after", "Accessibility list with Event Spy on", result))
                step = "06"
                performed[step] = confirm_switch_on(driver, labels, step_timeout, lambda: _screenshot(driver, evidence_dir / "accessibility_service_on.png", "accessibility_service_on", "Event Spy service switch on", result))
            else:
                performed[step] = {"outcome": "failed" if verdict.startswith("ERROR") else "skipped", "detail": verdict}
            _finish(result, verdict)
        except Exception as exc:
            logger.debug("%s[%s]: step %s failed: %s", self.risk_id, app_config.id, step, exc, exc_info=True)
            performed[step] = {"outcome": "failed", "detail": str(exc)}
            _finish(result, f"ERROR ({exc})")
        finally:
            turn_service_off(spy, before, cleanup)
            if not (cleanup["settings_restored"] and cleanup["service_off"]):
                result.final_status = "FAILED"
                result.verdict = "Inconclusive"
                result.errors.append("Event Spy's accessibility service could not be confirmed off: " + "; ".join(cleanup["errors"]))
            _clear_captures(spy, cleanup)
            remove_attacker_app(spy, cleanup)
            if not cleanup["attacker_uninstalled"]:
                result.errors.append(f"Event Spy could not be uninstalled; remove {EVENT_SPY_PACKAGE} by hand.")
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
        logger.debug("android event capture: screenshot %s failed: %s", path.name, exc, exc_info=True)
        saved, reason = False, str(exc)
    else:
        reason = "Appium did not save the file"
    if saved:
        result.evidence.append({"kind": kind, "path": str(path), "label": label})
    else:
        result.metadata.setdefault("evidence_errors", []).append(f"{path.name}: {reason}")


# Return the phone's model and Android API level, as playbook step 01 names the test devices by them.
def _device(adb) -> dict:
    code, out, _ = adb.run(["shell", "getprop", "ro.build.version.sdk"])
    return {"model": phone_model(adb), "sdk": out.strip() if code == 0 and out.strip() else None}


# Return the SHA-256 of a local file, or None when it is absent because the device already had the app.
def _file_sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


# Build the Settings labels from the configuration; Event Spy is always found by its own name, while the category and
# consent labels are shared with the other accessibility risk.
def _settings_labels(configured) -> SettingsLabels:
    defaults = SettingsLabels(service=EVENT_SPY_APP.label)
    configured = configured or {}
    return SettingsLabels(
        service=EVENT_SPY_APP.label,
        categories=tuple(configured.get("categories") or defaults.categories),
        allow=tuple(configured.get("allow") or defaults.allow),
    )


# Drop an Event Spy service left enabled by an interrupted run from the settings to restore, so cleanup turns it off.
def _without_spy(before: AccessibilitySettings, result: AndroidRiskRunResult) -> AccessibilitySettings:
    components = enabled_components(before.enabled_services)
    stale = result.metadata["stale_attacker_service"] = EVENT_SPY_APP.names_service(components)
    if not stale:
        return before
    others = [component for component in components if not EVENT_SPY_APP.names_service([component])]
    return AccessibilitySettings(enabled_services=":".join(others), accessibility_enabled=before.accessibility_enabled if others else "0")


# Report whether the package manager lists Event Spy's accessibility service, or None when it cannot be asked.
def _service_declared(adb) -> bool | None:
    code, out, _ = adb.run(["shell", "pm", "query-services", "-a", "android.accessibilityservice.AccessibilityService"])
    if code != 0:
        return None
    return f"packageName={EVENT_SPY_PACKAGE}" in (out or "") or f"{EVENT_SPY_PACKAGE}/" in (out or "")


# Launch the app, deny any permission dialogs covering it, and return a hand-off verdict unless it is in the foreground.
def _launch(driver, adb, package: str, launch_wait: float, timeout: float, evidence_dir: Path, result: AndroidRiskRunResult) -> str | None:
    driver.activate_app(package)
    time.sleep(launch_wait)
    _deny_prompts(driver, package, timeout, launch_wait, evidence_dir, result)
    return _foreground_verdict(driver, package, result)


# Deny any permission dialogs covering the app, screenshotting each first, and record them in the metadata.
def _deny_prompts(driver, package: str, timeout: float, launch_wait: float, evidence_dir: Path, result: AndroidRiskRunResult) -> None:
    prompts = result.metadata["permission_prompts"]
    offset = len(prompts)
    prompts.extend(deny_permission_prompts(
        driver, package, timeout, launch_wait,
        before_deny=lambda index: _screenshot(driver, evidence_dir / f"permission_prompt_{offset + index + 1}.png", "accessibility_permission_prompt", "System permission dialog before denial", result),
    ))


# Return an inconclusive hand-off classification or an automation error unless the app is in the foreground.
def _foreground_verdict(driver, package: str, result: AndroidRiskRunResult) -> str | None:
    try:
        current = driver.current_package
    except Exception as exc:
        return f"ERROR (Could not determine the foreground app: {exc})"
    result.metadata["foreground_package"] = current
    if current == package:
        return None
    return f"INCONCLUSIVE ({_handoff_reason(current, result)} - the capture was not tested)"


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


# Use the app as the playbook's user does, through its scripted steps, and return the canaries typed by name and value.
def _use_app(driver, adb, package: str, steps: list, canaries: dict[str, str], timeout: float, launch_wait: float, evidence_dir: Path, result: AndroidRiskRunResult) -> dict[str, str]:
    if not steps:
        return {}
    outcome = run_steps(driver, steps, canaries, timeout)
    result.metadata["steps"] = outcome.to_dict()
    result.metadata["coverage"] = "scripted_partial" if outcome.failure is not None else "scripted"
    _deny_prompts(driver, package, timeout, launch_wait, evidence_dir, result)
    try:
        result.metadata["foreground_after_input"] = driver.current_package
    except Exception as exc:
        logger.debug("android event capture: foreground after input unknown: %s", exc, exc_info=True)
    return {name: canaries[name] for name in dict.fromkeys(outcome.canaries_typed)}


# Read Event Spy's events for the app until they stop changing for the settle time, or the timeout passes.
def _wait_for_capture(spy: EventSpy, package: str, timeout: float, settle: float) -> list[CapturedEvent]:
    started = time.monotonic()
    events: list[CapturedEvent] = []
    changed_at = started
    while True:
        latest = spy.events(package)
        if len(latest) != len(events):
            events, changed_at = latest, time.monotonic()
        now = time.monotonic()
        if now - changed_at >= settle or now - started >= timeout:
            return events
        time.sleep(POLL_SECONDS)


# Save the events Event Spy captured from the app as evidence; other apps' events are left out of the report.
def _save_events(events: list[CapturedEvent], evidence_dir: Path, result: AndroidRiskRunResult) -> None:
    path = evidence_dir / "captured_events.json"
    path.write_text(json.dumps([asdict(event) for event in events], indent=2), encoding="utf-8")
    result.evidence.append({"kind": "accessibility_captured_events", "path": str(path), "label": "Events Event Spy captured from the app"})


# Open Event Spy's own screen, as the playbook's step 04 screenshot shows it, and go back to the home screen.
def _show_event_spy(driver, launch_wait: float, evidence_dir: Path, result: AndroidRiskRunResult) -> None:
    try:
        driver.activate_app(EVENT_SPY_PACKAGE)
        time.sleep(launch_wait)
    except Exception as exc:
        logger.debug("android event capture: opening Event Spy failed: %s", exc, exc_info=True)
        result.metadata.setdefault("evidence_errors", []).append(f"event_spy_screen.png: {exc}")
        return
    _screenshot(driver, evidence_dir / "event_spy_screen.png", "accessibility_event_spy_screen", "Captured events in the Event Spy interface", result)


# Return a value without separators or case, so a reformatted capture of it still matches.
def _normalised(text: str | None) -> str:
    return SEPARATORS_RE.sub("", text or "").lower()


# Report whether a captured text is a password field's masked text for a value of this length.
def _masked(text: str | None, value: str) -> bool:
    return bool(text) and len(text) == len(value) and set(text) <= MASK_CHARACTERS


# Gather what the verdict depends on: the events seen from the app and which typed canaries they contain.
def _observe(events: list[CapturedEvent], typed: dict[str, str], result: AndroidRiskRunResult) -> dict:
    texts = [event for event in events if event.event_type == "TEXT_CHANGE"]
    canaries = {}
    for name, value in typed.items():
        wanted = _normalised(value)
        match = next((event for event in texts if wanted and wanted in _normalised(event.text)), None)
        canaries[name] = {
            "captured": match is not None,
            "view_id": match.view_id if match else None,
            "masked": match is None and any(_masked(event.text, value) for event in texts),
        }
    counts = {event_type: sum(event.event_type == event_type for event in events) for event_type in CAPTURE_EVENT_TYPES}
    result.metadata["capture"] = {"event_counts": counts, "canaries": canaries}
    package = result.package_name
    foreground = result.metadata.get("foreground_after_input")
    observation = {
        "events": len(events),
        "event_counts": counts,
        "typed": list(typed),
        "captured": [name for name, entry in canaries.items() if entry["captured"]],
        "masked": [name for name, entry in canaries.items() if entry["masked"]],
        "steps_failed": (result.metadata.get("steps") or {}).get("failure") is not None,
        "left_app": foreground is not None and foreground != package,
    }
    result.metadata["observation"] = observation
    return observation


# Decide the verdict and the rule that decided it from the observation.
def _decide(observation: dict) -> tuple[str, str | None]:
    counts = ", ".join(f"{count} {event_type.lower().replace('_', ' ')}" for event_type, count in observation["event_counts"].items())
    if observation["captured"]:
        return f"CAPTURED (Event Spy recorded the typed canary values {', '.join(observation['captured'])} from the app in plain text)", "canary_captured"
    if observation["steps_failed"]:
        return "INCONCLUSIVE (The scripted steps stopped early and Event Spy recorded none of the canaries typed)", None
    if not observation["typed"]:
        return f"INCONCLUSIVE (No canary was typed into the app; Event Spy captured {counts} event(s) from it)", None
    if observation["left_app"]:
        return "INCONCLUSIVE (The app was no longer in front after the input and Event Spy recorded none of the canaries typed)", None
    # Android masks every password field for every accessibility service, so masking alone is not the app's protection.
    if set(observation["typed"]) <= set(observation["masked"]):
        return f"INCONCLUSIVE (Only password fields were typed into, and Android masked them: {', '.join(observation['masked'])})", None
    if observation["events"]:
        return f"PROTECTED (Event Spy saw the app ({counts} event(s)) but recorded none of the typed canary values)", "target_events_without_canaries"
    return "INCONCLUSIVE (Event Spy recorded no events from the app, so its capture of the input cannot be judged)", None


# Clear what Event Spy captured, as its Clear Logs button does, recording the outcome in cleanup.
def _clear_captures(spy: EventSpy, cleanup: dict) -> None:
    try:
        spy.clear()
        cleanup["data_cleared"] = True
    except Exception as exc:
        logger.debug("android event capture: clearing Event Spy's data failed: %s", exc, exc_info=True)
        cleanup["errors"].append(f"clearing Event Spy's data failed: {exc}")


# Set the result's verdict, status and errors from the verdict text.
def _finish(result: AndroidRiskRunResult, verdict: str) -> None:
    result.metadata["verdict"] = verdict
    result.final_status = _status_from_verdict(verdict)
    result.verdict = _security_verdict_from_verdict(verdict)
    if verdict.startswith("ERROR"):
        result.errors.append(verdict)


# Map the observation to the risk's final status.
def _status_from_verdict(verdict: str) -> str:
    for prefix, status in (("CAPTURED", "A11Y_INPUT_CAPTURED"), ("PROTECTED", "A11Y_INPUT_PROTECTED"), ("INCONCLUSIVE", "A11Y_NOT_TESTED"), ("ERROR", "FAILED")):
        if verdict.startswith(prefix):
            return status
    return "UNKNOWN"


# Map the observation to the shared security verdict.
def _security_verdict_from_verdict(verdict: str) -> str:
    if verdict.startswith("CAPTURED"):
        return "At Risk"
    if verdict.startswith("PROTECTED"):
        return "Reduced Risk"
    return "Inconclusive"
