"""Runs the playbook's on-screen text extraction test, checking whether Text Scraper reads the text an app shows."""

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
from mobile_playbook.platforms.android.device.captured_events import CapturedEvent
from mobile_playbook.platforms.android.device.fingerprint import installed_base_apk_sha256
from mobile_playbook.platforms.android.device.handoff_packages import BROWSER_PACKAGES, LAUNCHER_PACKAGES
from mobile_playbook.platforms.android.device.permission_prompts import deny_permission_prompts
from mobile_playbook.platforms.android.device.permissions import PERMISSION_CONTROLLER_PACKAGES
from mobile_playbook.platforms.android.device.phone_role import phone_model
from mobile_playbook.platforms.android.device.scripted_steps import merged_canaries, run_steps
from mobile_playbook.platforms.android.device.text_scraper import TEXT_SCRAPER_APP, TEXT_SCRAPER_PACKAGE, TextScraper
from mobile_playbook.platforms.android.models import AndroidRiskRunResult
from mobile_playbook.platforms.android.risks.base import AndroidRisk

logger = logging.getLogger(__name__)
POLL_SECONDS = 0.5
# Without this, Appium's UiAutomation connection makes Android unbind other accessibility services, Text Scraper's too.
DRIVER_CAPABILITIES = {"appium:disableSuppressAccessibilityService": True}
# Rows Text Scraper saves from walking a window's views; its TEXT_INPUT and PASSWORD_INPUT rows record typing instead.
SCRAPED_EVENT_TYPES = ("UI_TEXT", "BUTTON", "INPUT_FIELD", "PASSWORD_FIELD")
# A screenshot opens this window on HarmonyOS, and Text Scraper saves the app's text under its package name.
DEFAULT_SCREENSHOT_PACKAGES = ("com.huawei.HwMultiScreenShot",)
# Text Scraper's service configuration, which differs from the playbook's step 03 example.
SERVICE_CONFIG = {
    "service": TEXT_SCRAPER_APP.service_class,
    "config_resource": "@xml/accessibility_service_config_event",
    "event_types": ["typeViewFocused", "typeViewTextChanged", "typeWindowStateChanged"],
    "flags": ["flagReportViewIds", "flagRetrieveInteractiveWindows"],
    "can_retrieve_window_content": True,
    "screenshot_interval_seconds": 5,
}
# Apps reformat what they show, such as grouping a card number or an amount, so values are compared without
# separators or case.
SEPARATORS_RE = re.compile(r"[\s\-/.(),]")
# Android reports a password field's text as these characters, one per character.
MASK_CHARACTERS = frozenset("•*●")
STEPS = ("01", "02", "03", "04", "05", "06")


class AndroidAccessibilityScreenTextRisk(AndroidRisk):
    risk_id = "android-feature-05-risk-03"
    feature_id = "feature-05"
    name = "Android Accessibility On-Screen Text Extraction Test"
    test_case_id = "accessibility_screen_text"
    test_case_type = "appium_accessibility"
    requires = ["adb", "appium", "text_scraper_attacker"]

    # Follow the playbook's on-screen text extraction steps with Text Scraper, preserve evidence, then turn its
    # service off, clear what it captured, delete the screenshots it took and uninstall it.
    def run(self, app_config, global_config, device_client, report_writer):
        started_at = datetime.now().astimezone()
        package = app_config.package_name
        report_dir = report_writer.test_report_dir(app_config.id, self.risk_id, self.test_case_id, platform="android")
        shared = getattr(global_config, "accessibility", None) or {}
        app_cfg = app_config.risks.get(self.risk_id) or {}
        cfg = {**shared, **app_cfg}
        launch_wait = float(cfg.get("launch_wait", 5))
        step_timeout = float(cfg.get("step_timeout_seconds", 10))
        read_timeout = float(cfg.get("attack_timeout", 15))
        settle = float(cfg.get("settle_seconds", 3))
        dwell = float(cfg.get("capture_seconds", 12))
        labels = _settings_labels(cfg.get("settings_labels"))
        canaries = merged_canaries(shared.get("canaries"), app_cfg.get("canaries"))
        on_screen_text = [str(value) for value in app_cfg.get("on_screen_text") or [] if str(value).strip()]
        screenshot_packages = tuple(cfg.get("screenshot_packages") or DEFAULT_SCREENSHOT_PACKAGES)
        result = AndroidRiskRunResult(
            run_timestamp=report_writer.run_timestamp, timestamp_start=started_at.isoformat(), timestamp_end=None,
            app_id=app_config.id, app_name=app_config.name, package_name=package, risk_id=self.risk_id,
            test_case_id=self.test_case_id, test_case_type=self.test_case_type,
        )
        performed = result.metadata["steps_performed"] = {step: {"outcome": "not_run", "detail": "Not reached."} for step in STEPS}
        result.metadata["coverage"] = "launch_only"
        result.metadata["permission_prompts"] = []
        shots = result.metadata["screenshots"] = {"capture_started": False, "error": None, "taken": 0, "pulled": 0, "pull_errors": []}
        cleanup = result.metadata["cleanup"] = {
            "settings_restored": False, "service_off": False, "data_cleared": False, "screenshots_deleted": False,
            "screenshots_left": [], "attacker_uninstalled": False, "errors": [],
        }
        evidence_dir = report_dir / "accessibility"
        driver = None
        adb = device_client.adb
        scraper = TextScraper(adb, TEXT_SCRAPER_APP.apk_path(cfg.get("text_scraper_apk")))
        before: AccessibilitySettings | None = None
        baseline: tuple[int, set[str]] | None = None
        step = "01"
        try:
            result.metadata["device"] = _device(adb)
            result.metadata["installed_base_apk_sha256"] = installed_base_apk_sha256(adb, package)
            before = _without_scraper(scraper.read_settings(), result)
            result.metadata["other_services_enabled"] = len(enabled_components(before.enabled_services))
            driver = device_client.make_driver(extra_capabilities=DRIVER_CAPABILITIES)
            _adb(adb, ["input", "keyevent", "KEYCODE_HOME"])
            evidence_dir.mkdir(parents=True, exist_ok=True)
            performed[step] = {"outcome": "completed", "detail": "Recorded the device, the installed APK hash and the accessibility settings, opened Appium and pressed HOME."}

            step = "02"
            result.metadata["text_scraper_apk_sha256"] = _file_sha256(scraper.apk)
            scraper.ensure_installed()
            # pm clear plays the app's Clear button, so only this run's rows are read.
            scraper.clear()
            declared = result.metadata["attacker_service_declared"] = _service_declared(adb)
            performed[step] = {"outcome": "provided", "detail": f"The provided malicious app {TEXT_SCRAPER_PACKAGE} declares an accessibility service with BIND_ACCESSIBILITY_SERVICE" + ("." if declared else " (declaration not confirmed by the package manager).")}
            result.metadata["attacker_service_config"] = SERVICE_CONFIG
            performed["03"] = {"outcome": "provided", "detail": "Text Scraper's service walks the active window's views on window state changes, saves their text to its local database and takes a screenshot every 5 seconds once started; its event types and flags differ from the playbook's example (see attacker_service_config)."}

            step = "04"
            enabled = enable_service(driver, scraper, before, evidence_dir, timeout=max(step_timeout, 20), labels=labels)
            result.metadata["service_enable"] = enabled.to_dict()
            result.evidence.extend(enabled.evidence)
            result.metadata.setdefault("evidence_errors", []).extend(enabled.evidence_errors)
            bound = result.metadata["service_bound"] = scraper.wait_until_bound(step_timeout)
            _adb(adb, ["input", "keyevent", "KEYCODE_HOME"])
            verdict = None
            if not bound:
                verdict = "INCONCLUSIVE (Text Scraper's service was enabled but Android did not bind it - the extraction was not tested)"
            else:
                baseline = (_device_epoch(adb), scraper.screenshots())
                _start_screenshots(driver, scraper, launch_wait, step_timeout, shots)
                window_start = _device_epoch(adb)
                verdict = _launch(driver, adb, package, launch_wait, step_timeout, evidence_dir, result)
            if verdict is None:
                typed = _use_app(driver, adb, package, app_cfg.get("steps") or [], canaries, step_timeout, launch_wait, evidence_dir, result)
                steps_end = _device_epoch(adb)
                # Stay on the screen being tested long enough for the service to take its screenshots.
                time.sleep(dwell)
                _screenshot(driver, evidence_dir / "target_screen.png", "accessibility_target_screen", "App screen being read", result)
                window_end = _device_epoch(adb) + 1
                rows = _scraped(_wait_for_rows(scraper, read_timeout, settle), package, screenshot_packages, window_start, window_end)
                _save_rows(rows, evidence_dir, result)
                _show_text_scraper(driver, scraper, launch_wait, step_timeout, evidence_dir, result)
                wanted = {f"on_screen_text[{index}]": value for index, value in enumerate(on_screen_text)}
                # Text Scraper only reads a screen when its window changes, so a typed value is judged only if it read
                # the app again after the typing.
                rescanned = any(row.timestamp >= steps_end * 1000 for row in rows)
                result.metadata["canaries_judged"] = rescanned
                if rescanned:
                    wanted.update({f"canary.{name}": value for name, value in typed.items()})
                verdict, rule = _decide(_observe(rows, wanted, result))
                result.metadata["verdict_rule"] = rule
                performed[step] = {"outcome": "completed", "detail": f"Service turned on via {enabled.method} and the app used; {verdict}"}

                step = "05"
                performed[step] = show_service_listed(driver, adb, labels, step_timeout, lambda: _screenshot(driver, evidence_dir / "accessibility_list_after.png", "accessibility_list_after", "Accessibility list with Text Scraper on", result))
                step = "06"
                performed[step] = confirm_switch_on(driver, labels, step_timeout, lambda: _screenshot(driver, evidence_dir / "accessibility_service_on.png", "accessibility_service_on", "Text Scraper service switch on", result))
            else:
                performed[step] = {"outcome": "failed" if verdict.startswith("ERROR") else "skipped", "detail": verdict}
            _finish(result, verdict)
        except Exception as exc:
            logger.debug("%s[%s]: step %s failed: %s", self.risk_id, app_config.id, step, exc, exc_info=True)
            performed[step] = {"outcome": "failed", "detail": str(exc)}
            _finish(result, f"ERROR ({exc})")
        finally:
            # Turning the service off also stops the app, which ends its screenshot timer.
            turn_service_off(scraper, before, cleanup)
            if not (cleanup["settings_restored"] and cleanup["service_off"]):
                result.final_status = "FAILED"
                result.verdict = "Inconclusive"
                result.errors.append("Text Scraper's accessibility service could not be confirmed off: " + "; ".join(cleanup["errors"]))
            _clear_captures(scraper, cleanup)
            if baseline is not None:
                _collect_screenshots(scraper, baseline, evidence_dir, result)
            else:
                cleanup["screenshots_deleted"] = True
            remove_attacker_app(scraper, cleanup)
            if not cleanup["attacker_uninstalled"]:
                result.errors.append(f"Text Scraper could not be uninstalled; remove {TEXT_SCRAPER_PACKAGE} by hand.")
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


# Return the time on the phone's clock in whole seconds, which Text Scraper's rows and screenshots are stamped with.
def _device_epoch(adb) -> int:
    out = _adb(adb, ["date", "+%s"]).strip()
    if not out.isdigit():
        raise RuntimeError(f"could not read the phone's clock: {out!r}")
    return int(out)


# Add a screenshot to the evidence when Appium saves it, otherwise record why it is missing; evidence never decides
# the verdict.
def _screenshot(driver, path: Path, kind: str, label: str, result: AndroidRiskRunResult) -> None:
    try:
        saved = driver.get_screenshot_as_file(str(path))
    except Exception as exc:
        logger.debug("android screen text: screenshot %s failed: %s", path.name, exc, exc_info=True)
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


# Build the Settings labels from the configuration; Text Scraper is always found by its own name, while the category
# and consent labels are shared with the other accessibility risks.
def _settings_labels(configured) -> SettingsLabels:
    defaults = SettingsLabels(service=TEXT_SCRAPER_APP.label)
    configured = configured or {}
    return SettingsLabels(
        service=TEXT_SCRAPER_APP.label,
        categories=tuple(configured.get("categories") or defaults.categories),
        allow=tuple(configured.get("allow") or defaults.allow),
    )


# Drop a Text Scraper service left enabled by an interrupted run from the settings to restore, so cleanup turns it off.
def _without_scraper(before: AccessibilitySettings, result: AndroidRiskRunResult) -> AccessibilitySettings:
    components = enabled_components(before.enabled_services)
    stale = result.metadata["stale_attacker_service"] = TEXT_SCRAPER_APP.names_service(components)
    if not stale:
        return before
    others = [component for component in components if not TEXT_SCRAPER_APP.names_service([component])]
    return AccessibilitySettings(enabled_services=":".join(others), accessibility_enabled=before.accessibility_enabled if others else "0")


# Report whether the package manager lists Text Scraper's accessibility service, or None when it cannot be asked.
def _service_declared(adb) -> bool | None:
    code, out, _ = adb.run(["shell", "pm", "query-services", "-a", "android.accessibilityservice.AccessibilityService"])
    if code != 0:
        return None
    return f"packageName={TEXT_SCRAPER_PACKAGE}" in (out or "") or f"{TEXT_SCRAPER_PACKAGE}/" in (out or "")


# Start Text Scraper's screenshot capture; a failure is recorded, as the screenshots do not decide the verdict.
def _start_screenshots(driver, scraper: TextScraper, launch_wait: float, timeout: float, shots: dict) -> None:
    try:
        scraper.set_screenshot_capture(driver, True, launch_wait, timeout)
        shots["capture_started"] = True
    except Exception as exc:
        logger.debug("android screen text: starting screenshot capture failed: %s", exc, exc_info=True)
        shots["error"] = f"starting screenshot capture failed: {exc}"


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
    return f"INCONCLUSIVE ({_handoff_reason(current, result)} - the extraction was not tested)"


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
        result.metadata["foreground_after_steps"] = driver.current_package
    except Exception as exc:
        logger.debug("android screen text: foreground after steps unknown: %s", exc, exc_info=True)
    return {name: canaries[name] for name in dict.fromkeys(outcome.canaries_typed)}


# Read Text Scraper's rows until they stop changing for the settle time, or the timeout passes.
def _wait_for_rows(scraper: TextScraper, timeout: float, settle: float) -> list[CapturedEvent]:
    started = time.monotonic()
    rows: list[CapturedEvent] = []
    changed_at = started
    while True:
        latest = scraper.events()
        if len(latest) != len(rows):
            rows, changed_at = latest, time.monotonic()
        now = time.monotonic()
        if now - changed_at >= settle or now - started >= timeout:
            return rows
        time.sleep(POLL_SECONDS)


# Keep the rows Text Scraper saved from walking the app's views while it was in front: those under the app's package
# or a screenshot window's, stamped between its launch and the end of the test.
def _scraped(rows: list[CapturedEvent], package: str, screenshot_packages: tuple[str, ...], start: int, end: int) -> list[CapturedEvent]:
    return [
        row for row in rows
        if row.event_type in SCRAPED_EVENT_TYPES
        and (row.package == package or row.package in screenshot_packages)
        and start * 1000 <= row.timestamp <= end * 1000
    ]


# Save the rows Text Scraper scraped from the app as evidence; other apps' rows are left out of the report.
def _save_rows(rows: list[CapturedEvent], evidence_dir: Path, result: AndroidRiskRunResult) -> None:
    path = evidence_dir / "captured_text.json"
    path.write_text(json.dumps([asdict(row) for row in rows], indent=2), encoding="utf-8")
    result.evidence.append({"kind": "accessibility_captured_text", "path": str(path), "label": "On-screen text Text Scraper saved from the app"})


# Stop the screenshots from Text Scraper's own screen and screenshot it, as the playbook's step 04 screenshot shows it.
def _show_text_scraper(driver, scraper: TextScraper, launch_wait: float, timeout: float, evidence_dir: Path, result: AndroidRiskRunResult) -> None:
    try:
        scraper.set_screenshot_capture(driver, False, launch_wait, timeout)
    except Exception as exc:
        logger.debug("android screen text: opening Text Scraper failed: %s", exc, exc_info=True)
        result.metadata.setdefault("evidence_errors", []).append(f"text_scraper_screen.png: {exc}")
        return
    _screenshot(driver, evidence_dir / "text_scraper_screen.png", "accessibility_text_scraper_screen", "Captured interface text in the Text Scraper application", result)


# Return a value without separators or case, so a reformatted display of it still matches.
def _normalised(text: str | None) -> str:
    return SEPARATORS_RE.sub("", text or "").lower()


# Report whether a scraped text is a password field's masked text for a value of this length.
def _masked(text: str | None, value: str) -> bool:
    return bool(text) and len(text) == len(value) and set(text) <= MASK_CHARACTERS


# Gather what the verdict depends on: the rows scraped from the app and which wanted values they contain.
def _observe(rows: list[CapturedEvent], wanted: dict[str, str], result: AndroidRiskRunResult) -> dict:
    found = {}
    for name, value in wanted.items():
        target = _normalised(value)
        match = next((row for row in rows if target and (target in _normalised(row.text) or target in _normalised(row.content_description))), None)
        found[name] = {
            "captured": match is not None,
            "event_type": match.event_type if match else None,
            "view_id": match.view_id if match else None,
            "masked": match is None and any(_masked(row.text, value) for row in rows),
        }
    counts = {event_type: sum(row.event_type == event_type for row in rows) for event_type in SCRAPED_EVENT_TYPES}
    result.metadata["capture"] = {"row_counts": counts, "values": found}
    foreground = result.metadata.get("foreground_after_steps")
    observation = {
        "rows": len(rows),
        "row_counts": counts,
        "wanted": list(wanted),
        "captured": [name for name, entry in found.items() if entry["captured"]],
        "masked": [name for name, entry in found.items() if entry["masked"]],
        "steps_failed": (result.metadata.get("steps") or {}).get("failure") is not None,
        "left_app": foreground is not None and foreground != result.package_name,
    }
    result.metadata["observation"] = observation
    return observation


# Decide the verdict and the rule that decided it from the observation.
def _decide(observation: dict) -> tuple[str, str | None]:
    counts = ", ".join(f"{count} {event_type.lower().replace('_', ' ')}" for event_type, count in observation["row_counts"].items())
    if observation["captured"]:
        return f"CAPTURED (Text Scraper read {', '.join(observation['captured'])} from the app's screen in plain text)", "value_captured"
    if observation["steps_failed"]:
        return "INCONCLUSIVE (The scripted steps stopped early and Text Scraper read none of the values looked for)", None
    if not observation["wanted"]:
        return f"INCONCLUSIVE (No on-screen value was configured and no typed canary could be judged; Text Scraper read {counts} row(s) from the app)", None
    if observation["left_app"]:
        return "INCONCLUSIVE (The app was no longer in front after the steps and Text Scraper read none of the values looked for)", None
    # Android masks every password field for every accessibility service, so masking alone is not the app's protection.
    if set(observation["wanted"]) <= set(observation["masked"]):
        return f"INCONCLUSIVE (Only password fields held the values looked for, and Android masked them: {', '.join(observation['masked'])})", None
    if observation["rows"]:
        return f"PROTECTED (Text Scraper read the app's screen ({counts} row(s)) but none of the values looked for)", "screen_read_without_values"
    return "INCONCLUSIVE (Text Scraper read nothing from the app's screen, so its extraction cannot be judged)", None


# Clear what Text Scraper captured, as its Clear button does, recording the outcome in cleanup.
def _clear_captures(scraper: TextScraper, cleanup: dict) -> None:
    try:
        scraper.clear()
        cleanup["data_cleared"] = True
    except Exception as exc:
        logger.debug("android screen text: clearing Text Scraper's data failed: %s", exc, exc_info=True)
        cleanup["errors"].append(f"clearing Text Scraper's data failed: {exc}")


# Pull the screenshots Text Scraper took during the test as evidence, then delete them from the phone; only files that
# were not there before and were modified since are touched.
def _collect_screenshots(scraper: TextScraper, baseline: tuple[int, set[str]], evidence_dir: Path, result: AndroidRiskRunResult) -> None:
    shots, cleanup = result.metadata["screenshots"], result.metadata["cleanup"]
    started, existing = baseline
    try:
        taken = scraper.taken_since(scraper.screenshots() - existing, started)
        shots["taken"] = len(taken)
        pulled, errors = scraper.pull_screenshots(taken, evidence_dir / "screenshots")
        shots["pulled"], shots["pull_errors"] = len(pulled), errors
        result.evidence.extend({"kind": "accessibility_attacker_screenshot", "path": str(path), "label": "Screenshot Text Scraper took"} for path in pulled)
        left = scraper.delete_screenshots(taken)
    except Exception as exc:
        logger.debug("android screen text: collecting screenshots failed: %s", exc, exc_info=True)
        cleanup["errors"].append(f"collecting Text Scraper's screenshots failed: {exc}")
        result.errors.append("Text Scraper's screenshots could not be checked; look for them in the phone's Screenshots folder.")
        return
    cleanup["screenshots_left"] = sorted(left)
    cleanup["screenshots_deleted"] = not left
    if left:
        cleanup["errors"].append(f"{len(left)} screenshot(s) could not be deleted")
        result.errors.append("Screenshots Text Scraper took are still on the phone: " + ", ".join(sorted(left)))


# Set the result's verdict, status and errors from the verdict text.
def _finish(result: AndroidRiskRunResult, verdict: str) -> None:
    result.metadata["verdict"] = verdict
    result.final_status = _status_from_verdict(verdict)
    result.verdict = _security_verdict_from_verdict(verdict)
    if verdict.startswith("ERROR"):
        result.errors.append(verdict)


# Map the observation to the risk's final status.
def _status_from_verdict(verdict: str) -> str:
    for prefix, status in (("CAPTURED", "A11Y_TEXT_CAPTURED"), ("PROTECTED", "A11Y_TEXT_PROTECTED"), ("INCONCLUSIVE", "A11Y_NOT_TESTED"), ("ERROR", "FAILED")):
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
