"""
Android risk check for whether an app allows screen recording and capture.
"""

from __future__ import annotations

import base64
import logging
import time
from datetime import datetime
from pathlib import Path

from mobile_playbook.platforms.android.models import AndroidRiskRunResult
from mobile_playbook.logging_setup import redacted
from mobile_playbook.platforms.android.risks.base import AndroidRisk

logger = logging.getLogger(__name__)

BROWSER_PACKAGES = [
    "com.android.chrome",
    "org.mozilla.firefox",
    "com.google.android.browser",
    "com.android.browser",
    "com.sec.android.app.sbrowser",
]
LAUNCHER_PACKAGES = ["com.google.android.apps.nexuslauncher", "com.sec.android.app.launcher"]
DEBUGGING_KEYWORDS = ["developer options", "debugging", "usb debugging", "debugging detected"]
CAPTURE_KEYWORDS = ["screen recording", "screen sharing", "screen recording/sharing"]


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
            recording_started = self._start_recording(driver)
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
            verdict = self._check_flag_secure(device_client)
            logger.debug("%s[%s]: FLAG_SECURE check -> %s", self.risk_id, package_name, verdict)
        return verdict

    # Start Appium screen recording, returning False when unsupported.
    def _start_recording(self, driver) -> bool:
        try:
            driver.start_recording_screen()
            return True
        except Exception as exc:
            logger.debug("%s: start_recording_screen failed: %s", self.risk_id, exc, exc_info=True)
            return False

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

    # Return a BLOCKED observation when the app hands the foreground to a browser, launcher or other app.
    def _check_app_switch(self, driver, package_name: str) -> str:
        try:
            current_package = driver.current_package
        except Exception as exc:
            logger.debug("%s[%s]: reading current_package failed: %s", self.risk_id, package_name, exc, exc_info=True)
            return "Unknown"
        logger.debug("%s[%s]: foreground package after launch is %s", self.risk_id, package_name, current_package)
        if current_package == package_name:
            return "Unknown"
        if current_package in BROWSER_PACKAGES:
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

    # Check the window dump for FLAG_SECURE and report BLOCKED, ALLOWED or an adb error.
    def _check_flag_secure(self, device_client) -> str:
        code, out, err = device_client.adb.run(["shell", "dumpsys", "window", "windows"])
        logger.debug("%s: dumpsys window exit %s, %s chars, FLAG_SECURE present=%s, err head=%r", self.risk_id, code, len(out or ""), "FLAG_SECURE" in (out or ""), (err or "")[:200])
        if code != 0:
            return f"ERROR (ADB check failed: {err})"
        if "FLAG_SECURE" in out:
            return "BLOCKED (FLAG_SECURE detected via ADB)"
        return "ALLOWED (UI visible and no security flags)"


# Map an observation to the risk's final status.
def _status_from_verdict(verdict: str) -> str:
    if verdict.startswith("ALLOWED"):
        return "SCREEN_CAPTURE_ALLOWED"
    if verdict.startswith("BLOCKED"):
        return "SCREEN_CAPTURE_BLOCKED"
    if verdict.startswith("ERROR"):
        return "FAILED"
    return "UNKNOWN"


# Map an observation to the At Risk, Reduced Risk or Inconclusive verdict.
def _security_verdict_from_verdict(verdict: str) -> str:
    if verdict.startswith("ALLOWED"):
        return "At Risk"
    if verdict.startswith("BLOCKED"):
        return "Reduced Risk"
    return "Inconclusive"
