"""
Android risk check for whether an app allows screen recording and capture.
"""

from __future__ import annotations

import base64
import logging
import re
import time
from datetime import datetime
from pathlib import Path

from mobile_playbook.platforms.android.models import AndroidRiskRunResult
from mobile_playbook.common.logging_setup import redacted
from mobile_playbook.platforms.android.acquisition.store import STORE_APPS
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
# System dialogs Android shows over the app, such as a runtime permission prompt; they are not the app protecting itself.
SYSTEM_PROMPT_PACKAGES = ["com.android.permissioncontroller", "com.google.android.permissioncontroller"]
DEBUGGING_KEYWORDS = ["developer options", "debugging", "usb debugging", "debugging detected"]
CAPTURE_KEYWORDS = ["screen recording", "screen sharing", "screen recording/sharing"]
# dumpsys prints window flags by name without the FLAG_ prefix ("fl=SECURE HARDWARE_ACCELERATED"), or as hex on
# some builds ("fl=#2000"); the lookbehind skips the private flags line ("pfl=").
WINDOW_FLAGS_RE = re.compile(r"(?<![A-Za-z])fl=(#[0-9a-fA-F]+|[A-Z_]+(?: [A-Z_]+)*)")
FLAG_SECURE_VALUE = 0x2000


class AndroidScreenCaptureRisk(AndroidRisk):
    risk_id = "android-feature-06-risk-01"
    feature_id = "feature-06"
    name = "Android Screen Capture Test"
    test_case_id = "screen_capture"
    test_case_type = "appium_screen_recording"
    requires = ["adb", "appium"]

    # Record the screen while launching the app, classify how it protects itself and write the result.
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
        recording_started = False
        try:
            logger.debug("%s[%s]: creating Appium driver", self.risk_id, app_config.id)
            driver = device_client.make_driver()
            recordings_dir = report_dir / "recordings"
            recordings_dir.mkdir(parents=True, exist_ok=True)
            recording_started, recording_error = self._start_recording(driver)
            if not recording_started:
                result.metadata["recording"] = f"not started: {recording_error}"
            logger.debug("%s[%s]: screen recording started=%s (recordings_dir=%s)", self.risk_id, app_config.id, recording_started, recordings_dir)
            if recording_started:
                time.sleep(float(cfg.get("record_lead_in", 2)))
            verdict = self._test_app(driver, device_client, app_config.package_name, cfg)
            result.metadata["verdict"] = verdict
            result.final_status = _status_from_verdict(verdict)
            result.verdict = _security_verdict_from_verdict(verdict)
            logger.debug("%s[%s]: observation %r -> final_status=%s verdict=%s", self.risk_id, app_config.id, verdict, result.final_status, result.verdict)
            if recording_started:
                time.sleep(float(cfg.get("record_tail", 5)))
                recording_path = self._stop_recording(driver, app_config.package_name, recordings_dir)
                if recording_path is not None:
                    logger.debug("%s[%s]: evidence screen recording at %s", self.risk_id, app_config.id, recording_path)
                    result.evidence.append({"kind": "screen_recording", "path": str(recording_path), "label": "Screen recording"})
                else:
                    logger.debug("%s[%s]: no screen recording evidence saved", self.risk_id, app_config.id)
            self._close_app(driver, app_config.package_name)
        except Exception as exc:
            logger.debug("%s[%s]: run failed: %s", self.risk_id, app_config.id, exc, exc_info=True)
            result.final_status = "FAILED"
            result.errors.append(str(exc))
            if recording_started and driver is not None:
                try:
                    driver.stop_recording_screen()
                except Exception as stop_exc:
                    logger.debug("%s[%s]: stopping recording after failure failed: %s", self.risk_id, app_config.id, stop_exc, exc_info=True)
        finally:
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

    # Launch the app and return the first conclusive observation from app switch, UI keyword and FLAG_SECURE checks.
    def _test_app(self, driver, device_client, package_name: str, cfg: dict) -> str:
        try:
            logger.debug("%s[%s]: activating app", self.risk_id, package_name)
            driver.activate_app(package_name)
        except Exception as exc:
            logger.debug("%s[%s]: activate_app failed: %s", self.risk_id, package_name, exc, exc_info=True)
            return f"ERROR: failed to launch app - {exc}"
        time.sleep(float(cfg.get("launch_wait", 4)))

        verdict = self._check_app_switch(driver, package_name)
        logger.debug("%s[%s]: app switch check -> %s", self.risk_id, package_name, verdict)
        if verdict == "Unknown":
            verdict = self._check_ui_keywords(driver, DEBUGGING_KEYWORDS, "BLOCKED (USB debugging detected in UI)")
            logger.debug("%s[%s]: debugging keyword check -> %s", self.risk_id, package_name, verdict)
        if verdict == "Unknown":
            verdict = self._check_ui_keywords(driver, CAPTURE_KEYWORDS, "BLOCKED (Screen capture warning detected in UI)")
            logger.debug("%s[%s]: capture keyword check -> %s", self.risk_id, package_name, verdict)
        if verdict == "Unknown":
            verdict = self._check_flag_secure(device_client, package_name)
            logger.debug("%s[%s]: FLAG_SECURE check -> %s", self.risk_id, package_name, verdict)
        return verdict

    # Start Appium screen recording, returning whether it started and, when it did not, why.
    def _start_recording(self, driver) -> tuple[bool, str | None]:
        try:
            driver.start_recording_screen()
            return True, None
        except Exception as exc:
            logger.debug("%s: start_recording_screen failed: %s", self.risk_id, exc, exc_info=True)
            # Appium errors carry a server stack trace after the message; the report keeps only the message's first line.
            message = (getattr(exc, "msg", None) or str(exc)).strip()
            return False, message.splitlines()[0] if message else type(exc).__name__

    # Stop recording and save the MP4, returning its path or None on failure.
    def _stop_recording(self, driver, package_name: str, recordings_dir: Path) -> Path | None:
        try:
            encoded = driver.stop_recording_screen()
            path = recordings_dir / f"{package_name}.mp4"
            data = base64.b64decode(encoded)
            path.write_bytes(data)
            logger.debug("%s[%s]: saved recording %s (%s bytes)", self.risk_id, package_name, path, len(data))
            return path
        except Exception as exc:
            logger.debug("%s[%s]: stopping or saving recording failed: %s", self.risk_id, package_name, exc, exc_info=True)
            return None

    # Terminate the app, ignoring failures.
    def _close_app(self, driver, package_name: str) -> None:
        try:
            logger.debug("%s[%s]: terminating app", self.risk_id, package_name)
            driver.terminate_app(package_name)
        except Exception as exc:
            logger.debug("%s[%s]: terminate_app failed: %s", self.risk_id, package_name, exc, exc_info=True)

    # Return a BLOCKED observation when the app hands the foreground to a browser, launcher or other app; an app
    # store redirect or a system prompt over the app is INCONCLUSIVE, since the app's screen was never examined.
    def _check_app_switch(self, driver, package_name: str) -> str:
        try:
            current_package = driver.current_package
        except Exception as exc:
            logger.debug("%s[%s]: reading current_package failed: %s", self.risk_id, package_name, exc, exc_info=True)
            return "Unknown"
        logger.debug("%s[%s]: foreground package after launch is %s", self.risk_id, package_name, current_package)
        if current_package == package_name:
            return "Unknown"
        if current_package in SYSTEM_PROMPT_PACKAGES:
            return f"INCONCLUSIVE (A system prompt covered the app: {current_package} - screen capture was not tested)"
        if current_package in STORE_APPS:
            verdict = f"INCONCLUSIVE (Redirected to {STORE_APPS[current_package]} - the app did not open, so screen capture was not tested)"
        elif current_package in BROWSER_PACKAGES:
            verdict = f"BLOCKED (Redirected to Browser: {current_package})"
        elif current_package in LAUNCHER_PACKAGES:
            verdict = "BLOCKED (App exited to Home Screen - likely security check)"
        else:
            verdict = f"BLOCKED (Switched to unknown app: {current_package})"
        self._close_app(driver, current_package)
        return verdict

    # Return the block message when the page source contains any keyword, else Unknown.
    def _check_ui_keywords(self, driver, keywords: list[str], block_message: str) -> str:
        try:
            source = driver.page_source.lower()
        except Exception as exc:
            logger.debug("%s: reading page_source failed: %s", self.risk_id, exc, exc_info=True)
            return "Unknown"
        matched = [keyword for keyword in keywords if keyword in source]
        logger.debug("%s: page_source %s chars, keywords matched=%s of %s", self.risk_id, len(source), matched, keywords)
        return block_message if any(keyword in source for keyword in keywords) else "Unknown"

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
