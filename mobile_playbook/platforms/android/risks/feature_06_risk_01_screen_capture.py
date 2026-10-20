"""
Android risk check for whether an app allows screen recording and capture, recording the screen through
MediaProjection with a helper app as the playbook's malicious app does.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from pathlib import Path

from mobile_playbook.platforms.android.models import AndroidRiskRunResult
from mobile_playbook.common.logging_setup import redacted
from mobile_playbook.platforms.android.acquisition.store import STORE_APPS
from mobile_playbook.platforms.android.device.capture_helper import CaptureHelper, Frame, helper_apk_path
from mobile_playbook.platforms.android.device.fingerprint import installed_base_apk_sha256
from mobile_playbook.platforms.android.device.permissions import PERMISSION_CONTROLLER_PACKAGES
from mobile_playbook.platforms.android.risks.base import AndroidRisk

logger = logging.getLogger(__name__)

BROWSER_PACKAGES = [
    "com.android.chrome",
    "org.mozilla.firefox",
    "com.google.android.browser",
    "com.android.browser",
    "com.sec.android.app.sbrowser",
]
LAUNCHER_PACKAGES = ["com.google.android.apps.nexuslauncher", "com.sec.android.app.launcher", "com.huawei.android.launcher"]
DEBUGGING_KEYWORDS = ["developer options", "debugging", "usb debugging", "debugging detected"]
CAPTURE_KEYWORDS = ["screen recording", "screen sharing", "screen recording/sharing"]
# dumpsys prints window flags by name without the FLAG_ prefix ("fl=SECURE HARDWARE_ACCELERATED"), or as hex on
# some builds ("fl=#2000"); the lookbehind skips the private flags line ("pfl=").
WINDOW_FLAGS_RE = re.compile(r"(?<![A-Za-z])fl=(#[0-9a-fA-F]+|[A-Z_]+(?: [A-Z_]+)*)")
FLAG_SECURE_VALUE = 0x2000
# A recorded frame counts as blank when nearly all of it is black and it holds almost no distinct colours: a
# FLAG_SECURE window is recorded as one flat black, while even a dark-themed screen has text and icons.
BLANK_DARK_RATIO = 0.98
BLANK_MAX_COLORS = 16


class AndroidScreenCaptureRisk(AndroidRisk):
    risk_id = "android-feature-06-risk-01"
    feature_id = "feature-06"
    name = "Android Screen Capture Test"
    test_case_id = "screen_capture"
    test_case_type = "appium_screen_recording"
    requires = ["adb", "appium", "screen_capture_helper"]

    # Record the screen with the helper app while launching the app, classify how it protects itself and write the result.
    def run(self, app_config, global_config, device_client, report_writer):
        started_at = datetime.now().astimezone()
        report_dir = report_writer.test_report_dir(app_config.id, self.risk_id, self.test_case_id, platform="android")
        cfg = {**global_config.screen_capture, **(app_config.risks.get(self.risk_id) or {})}
        result = AndroidRiskRunResult(
            run_timestamp=report_writer.run_timestamp,
            timestamp_start=started_at.isoformat(),
            timestamp_end=None,
            app_id=app_config.id,
            app_name=app_config.name,
            package_name=app_config.package_name,
            risk_id=self.risk_id,
            test_case_id=self.test_case_id,
            test_case_type=self.test_case_type,
        )
        logger.debug("%s[%s]: setup package=%s report_dir=%s", self.risk_id, app_config.id, app_config.package_name, report_dir)
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("%s[%s]: effective config %s", self.risk_id, app_config.id, redacted(cfg))
        driver = None
        helper = None
        recording = False
        try:
            result.metadata["installed_base_apk_sha256"] = installed_base_apk_sha256(device_client.adb, app_config.package_name)
            logger.debug("%s[%s]: creating Appium driver", self.risk_id, app_config.id)
            driver = device_client.make_driver()
            helper = CaptureHelper(device_client.adb, helper_apk_path(cfg.get("helper_apk")))
            helper.ensure_installed()
            # Start from the home screen, so the frame recorded before launch shows ordinary content.
            device_client.adb.run(["shell", "input", "keyevent", "KEYCODE_HOME"])
            helper.start(driver, timeout=float(cfg.get("consent_timeout", 15)))
            recording = True
            time.sleep(float(cfg.get("record_lead_in", 2)))
            before_launch = helper.snap("before_launch")
            verdict, app_open = self._test_app(driver, device_client, helper, before_launch, app_config.package_name, cfg, result)
            result.metadata["verdict"] = verdict
            result.final_status = _status_from_verdict(verdict)
            result.verdict = _security_verdict_from_verdict(verdict)
            logger.debug("%s[%s]: observation %r -> final_status=%s verdict=%s", self.risk_id, app_config.id, verdict, result.final_status, result.verdict)
            self._record_frames(helper, [before_launch, app_open], report_dir, result)
            self._close_app(driver, app_config.package_name)
        except Exception as exc:
            logger.debug("%s[%s]: run failed: %s", self.risk_id, app_config.id, exc, exc_info=True)
            result.final_status = "FAILED"
            result.errors.append(str(exc))
        finally:
            if recording:
                try:
                    helper.stop()
                except Exception as stop_exc:
                    logger.debug("%s[%s]: stopping the capture helper failed: %s", self.risk_id, app_config.id, stop_exc, exc_info=True)
            if driver is not None:
                try:
                    logger.debug("%s[%s]: quitting Appium session %s", self.risk_id, app_config.id, getattr(driver, "session_id", None))
                    driver.quit()
                except Exception as quit_exc:
                    logger.debug("%s[%s]: driver quit failed: %s", self.risk_id, app_config.id, quit_exc, exc_info=True)
            result.timestamp_end = datetime.now().astimezone().isoformat()
            logger.debug("%s[%s]: writing result final_status=%s verdict=%s errors=%s evidence=%s", self.risk_id, app_config.id, result.final_status, result.verdict, result.errors, [item.get("path") for item in result.evidence])
            report_writer.write_result(result, report_dir)
        return result

    # Classify the recorded frame only when recording works and the target remains in the foreground.
    # Record UI keywords and FLAG_SECURE as observations, since neither proves capture was prevented.
    def _test_app(self, driver, device_client, helper: CaptureHelper, before_launch: Frame, package_name: str, cfg: dict, result: AndroidRiskRunResult) -> tuple[str, Frame | None]:
        try:
            logger.debug("%s[%s]: activating app", self.risk_id, package_name)
            driver.activate_app(package_name)
        except Exception as exc:
            logger.debug("%s[%s]: activate_app failed: %s", self.risk_id, package_name, exc, exc_info=True)
            return f"ERROR: failed to launch app - {exc}", None
        time.sleep(float(cfg.get("launch_wait", 4)))
        # Preserve the recorded screen even when the target has exited or redirected.
        app_open = helper.snap("app_open")

        capture_verdict = _capture_verdict(before_launch, app_open)
        if capture_verdict.startswith("ERROR"):
            return capture_verdict, app_open
        verdict = self._check_app_switch(driver, package_name)
        logger.debug("%s[%s]: app switch check -> %s", self.risk_id, package_name, verdict)
        if verdict != "Unknown":
            return verdict, app_open
        flag_check = self._check_flag_secure(device_client, package_name)
        result.metadata["flag_secure"] = True if flag_check.startswith("BLOCKED") else False if flag_check.startswith("ALLOWED") else None
        logger.debug("%s[%s]: FLAG_SECURE check -> %s", self.risk_id, package_name, flag_check)
        result.metadata["ui_observations"] = self._read_ui_observations(driver)
        logger.debug("%s[%s]: recorded frames before=%s app=%s -> %s", self.risk_id, package_name, before_launch, app_open, capture_verdict)
        return capture_verdict, app_open

    # Copy the recorded frames into the report as evidence and record how blank each was.
    def _record_frames(self, helper: CaptureHelper, frames: list[Frame | None], report_dir: Path, result: AndroidRiskRunResult) -> None:
        labels = {"before_launch": "Recorded frame before launching the app", "app_open": "Recorded frame with the app open"}
        capture = {}
        for frame in frames:
            if frame is None:
                continue
            capture[frame.label] = {"dark_ratio": round(frame.dark_ratio, 4), "distinct_colors": frame.distinct_colors, "blank": _is_blank(frame)}
            path = helper.pull(frame, report_dir / "capture")
            if path is not None:
                result.evidence.append({"kind": "screen_capture_frame", "path": str(path), "label": labels.get(frame.label, frame.label)})
        result.metadata["capture"] = capture

    # Terminate the app, ignoring failures.
    def _close_app(self, driver, package_name: str) -> None:
        try:
            logger.debug("%s[%s]: terminating app", self.risk_id, package_name)
            driver.terminate_app(package_name)
        except Exception as exc:
            logger.debug("%s[%s]: terminate_app failed: %s", self.risk_id, package_name, exc, exc_info=True)

    # Treat every foreground hand-off as inconclusive, since an exit or redirect does not establish capture protection.
    # Leave other apps open, and report a failed foreground query as an automation error.
    def _check_app_switch(self, driver, package_name: str) -> str:
        try:
            current_package = driver.current_package
        except Exception as exc:
            logger.debug("%s[%s]: reading current_package failed: %s", self.risk_id, package_name, exc, exc_info=True)
            return f"ERROR (Could not determine the foreground app: {exc})"
        logger.debug("%s[%s]: foreground package after launch is %s", self.risk_id, package_name, current_package)
        if current_package == package_name:
            return "Unknown"
        if current_package in PERMISSION_CONTROLLER_PACKAGES:
            return f"INCONCLUSIVE (A system prompt covered the app: {current_package} - screen capture was not tested)"
        if current_package in STORE_APPS:
            verdict = f"INCONCLUSIVE (Redirected to {STORE_APPS[current_package]} - the app did not open, so screen capture was not tested)"
        elif current_package in BROWSER_PACKAGES:
            verdict = f"INCONCLUSIVE (Redirected to Browser: {current_package} - screen capture was not tested)"
        elif current_package in LAUNCHER_PACKAGES:
            verdict = "INCONCLUSIVE (App exited to Home Screen - the cause was not established, so screen capture was not tested)"
        else:
            verdict = f"INCONCLUSIVE (Switched to unknown app: {current_package} - screen capture was not tested)"
        return verdict

    # Preserve warning-keyword matches as supporting evidence without changing the frame-based verdict.
    def _read_ui_observations(self, driver) -> dict:
        try:
            source = driver.page_source.lower()
        except Exception as exc:
            logger.debug("%s: reading page_source failed: %s", self.risk_id, exc, exc_info=True)
            return {"error": f"Could not read the app UI: {exc}"}
        observations = {
            "debugging_keywords": [keyword for keyword in DEBUGGING_KEYWORDS if keyword in source],
            "capture_keywords": [keyword for keyword in CAPTURE_KEYWORDS if keyword in source],
        }
        logger.debug("%s: page_source %s chars, keyword observations=%s", self.risk_id, len(source), observations)
        return observations

    # Check the app's own windows in the window dump for FLAG_SECURE and report BLOCKED, ALLOWED or an error.
    def _check_flag_secure(self, device_client, package_name: str) -> str:
        code, out, err = device_client.adb.run(["shell", "dumpsys", "window", "windows"])
        if code != 0:
            logger.debug("%s: dumpsys window exit %s, err head=%r", self.risk_id, code, (err or "")[:200])
            return f"ERROR (ADB check failed: {err})"
        window_flags = _window_flags(out or "", package_name)
        logger.debug("%s[%s]: dumpsys window %s chars, app window flags=%s", self.risk_id, package_name, len(out or ""), window_flags)
        if not window_flags:
            return f"ERROR (no window found for {package_name} in dumpsys window)"
        if any(_is_secure(flags) for flags in window_flags):
            return "BLOCKED (FLAG_SECURE detected via ADB)"
        return "ALLOWED (UI visible and no security flags)"


# Return the fl= flags of each window the package owns in a `dumpsys window windows` dump.
def _window_flags(dump: str, package_name: str) -> list[str]:
    flags = []
    for block in re.split(r"^  Window #", dump, flags=re.MULTILINE)[1:]:
        if f"package={package_name} " not in block and not block.rstrip().endswith(f"package={package_name}"):
            continue
        match = WINDOW_FLAGS_RE.search(block)
        if match:
            flags.append(match.group(1))
    return flags


# Report whether a window's flags include FLAG_SECURE, by name or as a hex value.
def _is_secure(flags: str) -> bool:
    if flags.startswith("#"):
        return bool(int(flags[1:], 16) & FLAG_SECURE_VALUE)
    return "SECURE" in flags.split()


# Report whether a recorded frame is blank, as a FLAG_SECURE window records.
def _is_blank(frame: Frame) -> bool:
    return frame.dark_ratio >= BLANK_DARK_RATIO and frame.distinct_colors <= BLANK_MAX_COLORS


# Decide from the recorded frames whether the app's screen was captured; a blank frame before launch means the
# recording itself was not working, so it cannot show anything about the app.
def _capture_verdict(before_launch: Frame, app_open: Frame) -> str:
    if _is_blank(before_launch):
        return "ERROR (the recording was already blank on the home screen before launch, so it cannot show whether the app blocks capture)"
    if _is_blank(app_open):
        return "BLOCKED (Screen recording captured a blank screen while the app was open)"
    return "ALLOWED (Screen recording captured the app's screen)"


# Map an observation to the risk's final status.
def _status_from_verdict(verdict: str) -> str:
    if verdict.startswith("ALLOWED"):
        return "SCREEN_CAPTURE_ALLOWED"
    if verdict.startswith("BLOCKED"):
        return "SCREEN_CAPTURE_BLOCKED"
    if verdict.startswith("ERROR"):
        return "FAILED"
    if verdict.startswith("INCONCLUSIVE"):
        return "SCREEN_CAPTURE_NOT_TESTED"
    return "UNKNOWN"


# Map an observation to the At Risk, Reduced Risk or Inconclusive verdict.
def _security_verdict_from_verdict(verdict: str) -> str:
    if verdict.startswith("ALLOWED"):
        return "At Risk"
    if verdict.startswith("BLOCKED"):
        return "Reduced Risk"
    return "Inconclusive"
