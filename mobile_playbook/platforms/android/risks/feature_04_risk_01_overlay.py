"""Tests whether the target permits a focusable application overlay and records synthetic sign-in input."""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from pathlib import Path

from mobile_playbook.platforms.android.acquisition.store import STORE_APPS
from mobile_playbook.platforms.android.device.fingerprint import installed_base_apk_sha256
from mobile_playbook.platforms.android.device.handoff_packages import BROWSER_PACKAGES, LAUNCHER_PACKAGES
from mobile_playbook.platforms.android.device.overlay_attacker import (
    ATTACKER_PACKAGE,
    PASSWORD_FIELD_ID,
    SIGN_IN_BUTTON_ID,
    USERNAME_FIELD_ID,
    OverlayAttacker,
    attacker_apk_path,
)
from mobile_playbook.platforms.android.device.permissions import PERMISSION_CONTROLLER_PACKAGES
from mobile_playbook.platforms.android.device.scripted_steps import StepsOutcome, merged_canaries, run_steps
from mobile_playbook.platforms.android.models import AndroidRiskRunResult
from mobile_playbook.platforms.android.risks.base import AndroidRisk

logger = logging.getLogger(__name__)
POLL_SECONDS = 0.5
OVERLAY_ACTION = "android.settings.action.MANAGE_OVERLAY_PERMISSION"
# topResumedActivity on Android 10+, ResumedActivity on 12+ and mResumedActivity on older builds.
RESUMED_ACTIVITY_RE = re.compile(r"\b(?:topResumedActivity|mResumedActivity|ResumedActivity)\s*[=:]\s*ActivityRecord\{\S+ u\d+ ([\w.]+)/")


class AndroidOverlayRisk(AndroidRisk):
    risk_id = "android-feature-04-risk-01"
    feature_id = "feature-04"
    name = "Android Overlay Test"
    test_case_id = "overlay"
    test_case_type = "appium_overlay"
    requires = ["adb", "appium"]

    # Follow the overlay playbook, preserve evidence, and remove the attacker and permission after every run.
    def run(self, app_config, global_config, device_client, report_writer):
        started_at = datetime.now().astimezone()
        package = app_config.package_name
        report_dir = report_writer.test_report_dir(app_config.id, self.risk_id, self.test_case_id, platform="android")
        shared = getattr(global_config, "overlay", None) or {}
        app_cfg = app_config.risks.get(self.risk_id) or {}
        cfg = {**shared, **app_cfg}
        result = AndroidRiskRunResult(
            run_timestamp=report_writer.run_timestamp, timestamp_start=started_at.isoformat(), timestamp_end=None,
            app_id=app_config.id, app_name=app_config.name, package_name=package, risk_id=self.risk_id,
            test_case_id=self.test_case_id, test_case_type=self.test_case_type,
        )
        performed = result.metadata["steps_performed"] = {
            step: {"outcome": "not_run", "detail": "Not reached."} for step in ("01", "03", "04", "05", "06")
        }
        result.metadata["coverage"] = "launch_only"
        result.metadata["overlay"] = {"added": False, "visible": None, "window_found": False, "shown_count": 0}
        driver = None
        attacker = None
        adb = device_client.adb
        step = "01"
        try:
            result.metadata["installed_base_apk_sha256"] = installed_base_apk_sha256(adb, package)
            driver = device_client.make_driver()
            _adb(adb, ["input", "keyevent", "KEYCODE_HOME"])
            evidence_dir = report_dir / "overlay"
            evidence_dir.mkdir(parents=True, exist_ok=True)
            performed[step] = {"outcome": "completed", "detail": "Recorded installed APK hash, opened Appium and pressed HOME."}

            step = "03"
            attacker = OverlayAttacker(adb, attacker_apk_path(cfg.get("attacker_apk")))
            attacker.ensure_installed()
            timeout = float(cfg.get("overlay_timeout", 15))
            attacker.start(app_config.name, timeout=timeout)
            _adb(adb, ["am", "start", "-a", OVERLAY_ACTION])
            time.sleep(float(cfg.get("launch_wait", 5)))
            _screenshot(driver, evidence_dir / "permission_list.png", "overlay_permission_list", "Display over other apps list", result)
            performed[step] = {"outcome": "completed", "detail": "Installed and started attacker; recorded overlay permission list."}

            step = "04"
            _grant_permission(driver, adb, float(cfg.get("step_timeout_seconds", 10)), result)
            _screenshot(driver, evidence_dir / "permission_toggle.png", "overlay_permission_toggle", "Attacker overlay permission", result)
            attacker.start(app_config.name, timeout=timeout)
            attacker.wait_for_state({"service_started"}, timeout=timeout)
            performed[step] = {"outcome": "completed", "detail": f"Permission confirmed via {result.metadata['permission_granted_via']}; service started."}

            step = "05"
            result.metadata["declares_hide_overlay_windows"] = _declares_hide_overlay_windows(adb, package)
            _adb(adb, ["input", "keyevent", "KEYCODE_HOME"])
            driver.activate_app(package)
            time.sleep(float(cfg.get("launch_wait", 5)))
            verdict = _foreground_verdict(driver, adb, package, result)
            if verdict is None:
                steps = app_cfg.get("steps") or []
                if steps:
                    outcome: StepsOutcome = run_steps(driver, steps, merged_canaries(shared.get("canaries"), app_cfg.get("canaries")), float(cfg.get("step_timeout_seconds", 10)))
                    result.metadata["steps"] = outcome.to_dict()
                    result.metadata["coverage"] = "scripted_partial" if outcome.failure is not None else "scripted"
                verdict = _foreground_verdict(driver, adb, package, result)
            if verdict is None:
                status = _wait_for_added(attacker, timeout)
                shown_count = status.overlay_shown_count if status else 0
                dump = _adb(adb, ["dumpsys", "window", "windows"])
                visible, blocks = _overlay_windows(dump)
                dump_path = evidence_dir / "dumpsys_overlay_window.txt"
                dump_path.write_text("\n\n".join(blocks), encoding="utf-8")
                result.evidence.append({"kind": "overlay_window_dump", "path": str(dump_path), "label": "Attacker window visibility fields"})
                overlay = result.metadata["overlay"] = {"added": shown_count > 0, "visible": visible, "window_found": bool(blocks), "shown_count": shown_count}
                verdict = _foreground_verdict(driver, adb, package, result) or _overlay_verdict(overlay)
            _screenshot(driver, evidence_dir / "target_screen.png", "overlay_target_screen", "Target screen with overlay observation", result)
            performed[step] = {"outcome": "failed" if verdict.startswith("ERROR") else "completed", "detail": verdict}

            step = "06"
            if verdict.startswith("ALLOWED"):
                _confirm_capture(driver, attacker, evidence_dir, float(cfg.get("step_timeout_seconds", 10)), timeout, result)
                performed[step] = {"outcome": "completed", "detail": "Synthetic submission confirmed in captured.json and the attacker launcher."}
            else:
                performed[step] = {"outcome": "skipped", "detail": "No visible overlay over a verified target; no input submitted."}
            result.metadata["verdict"] = verdict
            result.final_status = _status_from_verdict(verdict)
            result.verdict = _security_verdict_from_verdict(verdict)
            if verdict.startswith("ERROR"):
                result.errors.append(verdict)
        except Exception as exc:
            logger.debug("%s[%s]: step %s failed: %s", self.risk_id, app_config.id, step, exc, exc_info=True)
            performed[step] = {"outcome": "failed", "detail": str(exc)}
            result.metadata["verdict"] = f"ERROR ({exc})"
            result.final_status = "FAILED"
            result.verdict = "Inconclusive"
            result.errors.append(str(exc))
        finally:
            try:
                if attacker is None:
                    attacker = OverlayAttacker(adb, attacker_apk_path(cfg.get("attacker_apk")))
                attacker.stop()
            except Exception as exc:
                logger.debug("%s[%s]: attacker stop failed: %s", self.risk_id, app_config.id, exc, exc_info=True)
            try:
                _adb(adb, ["appops", "set", ATTACKER_PACKAGE, "SYSTEM_ALERT_WINDOW", "default"])
            except Exception as exc:
                logger.debug("%s[%s]: resetting overlay appop failed: %s", self.risk_id, app_config.id, exc, exc_info=True)
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
        logger.debug("android overlay: screenshot %s failed: %s", path.name, exc, exc_info=True)
        saved, reason = False, str(exc)
    else:
        reason = "Appium did not save the file"
    if saved:
        result.evidence.append({"kind": kind, "path": str(path), "label": label})
    else:
        result.metadata.setdefault("evidence_errors", []).append(f"{path.name}: {reason}")


# Find the Settings switch by its standard resource id or widget class, allowing the page time to load.
def _permission_switch(driver, timeout: float):
    deadline = time.monotonic() + timeout
    while True:
        for by, value in (("id", "android:id/switch_widget"), ("class name", "android.widget.Switch")):
            found = driver.find_elements(by, value)
            if found:
                return found[0]
        if time.monotonic() >= deadline:
            return None
        time.sleep(POLL_SECONDS)


# Grant overlay access in Settings, falling back to appops only if the switch cannot be found, and confirm the grant.
def _grant_permission(driver, adb, timeout: float, result: AndroidRiskRunResult) -> None:
    _adb(adb, ["am", "start", "-a", OVERLAY_ACTION, "-d", f"package:{ATTACKER_PACKAGE}"])
    switch = _permission_switch(driver, timeout)
    if switch is None:
        _adb(adb, ["appops", "set", ATTACKER_PACKAGE, "SYSTEM_ALERT_WINDOW", "allow"])
        result.metadata["permission_granted_via"] = "appops_fallback"
    else:
        checked = str(switch.get_attribute("checked")).lower()
        if checked == "false":
            switch.click()
        elif checked != "true":
            raise RuntimeError("Overlay Settings switch has no readable checked state")
        result.metadata["permission_granted_via"] = "settings_ui"
    deadline = time.monotonic() + timeout
    while True:
        output = _adb(adb, ["appops", "get", ATTACKER_PACKAGE, "SYSTEM_ALERT_WINDOW"])
        if re.search(r"\ballow\b", output):
            return
        if time.monotonic() >= deadline:
            raise RuntimeError(f"Overlay permission was not allowed: {output}")
        time.sleep(POLL_SECONDS)


# Return an inconclusive hand-off classification or an automation error unless the target is still foreground.
def _foreground_verdict(driver, adb, package: str, result: AndroidRiskRunResult) -> str | None:
    try:
        current = driver.current_package
    except Exception as exc:
        return f"ERROR (Could not determine the foreground app: {exc})"
    result.metadata["foreground_package"] = current
    if current == ATTACKER_PACKAGE:
        # The overlay takes input focus, so Appium may name it; the resumed activity shows the app behind it.
        current = result.metadata["resumed_package"] = _resumed_package(adb)
        if current is None:
            return "INCONCLUSIVE (The overlay held focus and the app behind it could not be read - overlay was not tested)"
    if current == package:
        return None
    if current in PERMISSION_CONTROLLER_PACKAGES:
        reason = f"A system prompt covered the app: {current}"
    elif current in STORE_APPS:
        reason = f"Redirected to {STORE_APPS[current]}"
    elif current in BROWSER_PACKAGES:
        reason = f"Redirected to Browser: {current}"
    elif current in LAUNCHER_PACKAGES:
        reason = "App exited to Home Screen"
    else:
        reason = f"Switched to unknown app: {current}"
    return f"INCONCLUSIVE ({reason} - overlay was not tested)"


# Return the package of the resumed activity from `dumpsys activity activities`, or None when it cannot be read.
def _resumed_package(adb) -> str | None:
    try:
        dump = _adb(adb, ["dumpsys", "activity", "activities"])
    except Exception as exc:
        logger.debug("android overlay: reading the resumed activity failed: %s", exc, exc_info=True)
        return None
    match = RESUMED_ACTIVITY_RE.search(dump)
    return match[1] if match else None


# Wait for evidence of addView without turning an absent window into an automation error.
def _wait_for_added(attacker: OverlayAttacker, timeout: float):
    deadline = time.monotonic() + timeout
    while True:
        status = attacker.status()
        if status is not None and status.overlay_shown_count > 0:
            return status
        if time.monotonic() >= deadline:
            return status
        time.sleep(POLL_SECONDS)


# Read explicit window visibility and suppression fields; unknown formats cannot establish a blocked overlay.
def _window_visibility(block: str) -> bool | None:
    fields = dict(re.findall(r"\b(mForceHideNonSystemOverlayWindow|mHidden|mPermanentlyHidden|mPolicyVisibility|mLegacyPolicyVisibilityAfterAnim|mAppOpVisibility|mHasSurface|mSurfaceShown|isVisible|isOnScreen|isReadyForDisplay)(?:\(\))?\s*=\s*(true|false)\b", block, re.IGNORECASE))
    fields = {name.lower(): value.lower() == "true" for name, value in fields.items()}
    if any(fields.get(name) is True for name in ("mforcehidenonsystemoverlaywindow", "mhidden", "mpermanentlyhidden")):
        return False
    if any(value is False for name, value in fields.items() if name not in ("mforcehidenonsystemoverlaywindow", "mhidden", "mpermanentlyhidden")):
        return False
    policy = re.search(r"\bmPolicyVisibility=(0x[0-9a-fA-F]+|\d+)\b", block)
    if policy and int(policy[1], 16 if policy[1].startswith("0x") else 10) == 0:
        return False
    view = re.search(r"\bmViewVisibility=(0x[0-9a-fA-F]+|\d+)\b", block)
    if view and int(view[1], 16 if view[1].startswith("0x") else 10) != 0:
        return False
    shown = re.findall(r"\b(?:mSurfaceShown|isVisible|isOnScreen|isReadyForDisplay|isShown)(?:\(\))?\s*=\s*(true|false)\b", block, re.IGNORECASE)
    if any(value.lower() == "false" for value in shown):
        return False
    return True if any(value.lower() == "true" for value in shown) else None


# Return the matching raw window blocks and whether any is visible, without matching similarly named packages.
def _overlay_windows(dump: str) -> tuple[bool | None, list[str]]:
    blocks = []
    for block in re.split(r"^  Window #", dump, flags=re.MULTILINE)[1:]:
        if not re.search(r"\bpackage=" + re.escape(ATTACKER_PACKAGE) + r"(?=\s|$)", block):
            continue
        window_type = re.search(r"\bty=([A-Z_]+|0x[0-9a-fA-F]+|\d+)\b", block)
        if window_type and window_type[1] not in {"APPLICATION_OVERLAY", "TYPE_APPLICATION_OVERLAY", "2038", "0x7f6"}:
            continue
        blocks.append("  Window #" + block)
    visible = [_window_visibility(block) for block in blocks]
    state = True if True in visible else False if visible and all(value is False for value in visible) else None
    return state, blocks


# Classify only an added window with known visibility while the target has been verified in the foreground.
def _overlay_verdict(overlay: dict) -> str:
    if not overlay["added"]:
        return "INCONCLUSIVE (The attacker never added its overlay window)"
    if not overlay["window_found"]:
        return "INCONCLUSIVE (No attacker overlay window was found in dumpsys window)"
    if overlay["visible"] is None:
        return "INCONCLUSIVE (The overlay window visibility fields were not recognised)"
    if overlay["visible"]:
        return "ALLOWED (The overlay was added and visible over the foreground target)"
    return "BLOCKED (The overlay was added but hidden while the target was foreground)"


# Submit only shared synthetic canaries, then reopen the attacker without resetting its recorded output.
def _confirm_capture(driver, attacker: OverlayAttacker, evidence_dir: Path, step_timeout: float, timeout: float, result: AndroidRiskRunResult) -> None:
    canaries = merged_canaries()
    steps = [
        {"type": {"id": USERNAME_FIELD_ID, "value": "{canary.email}"}},
        {"type": {"id": PASSWORD_FIELD_ID, "value": "{canary.password}"}},
        {"tap": {"id": SIGN_IN_BUTTON_ID}},
    ]
    outcome = run_steps(driver, steps, canaries, step_timeout)
    result.metadata["capture"] = {"steps": outcome.to_dict(), "confirmed": False, "display_confirmed": False}
    if outcome.failure is not None:
        raise RuntimeError(f"Overlay canary submission failed: {outcome.failure['error']}")
    attacker.wait_for_state({"captured"}, timeout=timeout)
    captured = attacker.captured()
    confirmed = any(record.username == canaries["email"] and record.password == canaries["password"] for record in captured)
    result.metadata["capture"]["confirmed"] = confirmed
    if not confirmed:
        raise RuntimeError("The attacker did not record the submitted synthetic canaries")
    driver.activate_app(ATTACKER_PACKAGE)
    deadline = time.monotonic() + step_timeout
    while True:
        source = driver.page_source
        displayed = canaries["email"] in source and canaries["password"] in source
        if displayed or time.monotonic() >= deadline:
            break
        time.sleep(POLL_SECONDS)
    result.metadata["capture"]["display_confirmed"] = displayed
    _screenshot(driver, evidence_dir / "attacker_captured.png", "overlay_target_screen", "Recorded canaries in attacker launcher", result)
    if not displayed:
        raise RuntimeError("The attacker launcher did not display the recorded synthetic canaries")


# Inspect only the requested-permissions section; a permission name elsewhere does not establish a declaration.
def _declares_hide_overlay_windows(adb, package: str) -> bool | None:
    try:
        dump = _adb(adb, ["dumpsys", "package", package])
        lines = dump.splitlines()
        for index, line in enumerate(lines):
            if line.strip() != "requested permissions:":
                continue
            indent = len(line) - len(line.lstrip())
            for permission in lines[index + 1:]:
                if permission.strip() and (len(permission) - len(permission.lstrip()) <= indent or permission.strip().endswith(":")):
                    break
                if permission.strip() == "android.permission.HIDE_OVERLAY_WINDOWS":
                    return True
            return False
    except Exception as exc:
        logger.debug("android overlay: requested permissions unavailable: %s", exc, exc_info=True)
    return None


# Map the observation to the overlay risk's final status.
def _status_from_verdict(verdict: str) -> str:
    for prefix, status in (("ALLOWED", "OVERLAY_ALLOWED"), ("BLOCKED", "OVERLAY_BLOCKED"), ("INCONCLUSIVE", "OVERLAY_NOT_TESTED"), ("ERROR", "FAILED")):
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
