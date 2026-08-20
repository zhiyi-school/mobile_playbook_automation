from __future__ import annotations

import base64
import time
from datetime import datetime
from pathlib import Path

from mobile_playbook.platforms.android.models import AndroidRiskRunResult
from mobile_playbook.platforms.android.risks.base import AndroidRisk

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
    risk_id = "android-feature6-risk1"
    name = "Android Screen Capture Test"
    test_case_id = "screen_capture"
    test_case_type = "appium_screen_recording"
    requires = ["adb", "appium"]

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
        driver = None
        recording_started = False
        try:
            driver = device_client.make_driver()
            recordings_dir = report_dir / "recordings"
            recordings_dir.mkdir(parents=True, exist_ok=True)
            recording_started = self._start_recording(driver)
            if recording_started:
                time.sleep(float(cfg.get("record_lead_in", 2)))
            verdict = self._test_app(driver, device_client, app_config.package_name, cfg)
            result.metadata["verdict"] = verdict
            result.final_status = _status_from_verdict(verdict)
            result.verdict = _security_verdict_from_verdict(verdict)
            if recording_started:
                time.sleep(float(cfg.get("record_tail", 5)))
                recording_path = self._stop_recording(driver, app_config.package_name, recordings_dir)
                if recording_path is not None:
                    result.evidence.append({"kind": "screen_recording", "path": str(recording_path), "label": "Screen recording"})
            self._close_app(driver, app_config.package_name)
        except Exception as exc:
            result.final_status = "FAILED"
            result.errors.append(str(exc))
            if recording_started and driver is not None:
                try:
                    driver.stop_recording_screen()
                except Exception:
                    pass
        finally:
            if driver is not None:
                try:
                    driver.quit()
                except Exception:
                    pass
            result.timestamp_end = datetime.now().astimezone().isoformat()
            report_writer.write_result(result, report_dir)
        return result

    def _test_app(self, driver, device_client, package_name: str, cfg: dict) -> str:
        try:
            driver.activate_app(package_name)
        except Exception as exc:
            return f"ERROR: failed to launch app - {exc}"
        time.sleep(float(cfg.get("launch_wait", 4)))

        verdict = self._check_app_switch(driver, package_name)
        if verdict == "Unknown":
            verdict = self._check_ui_keywords(driver, DEBUGGING_KEYWORDS, "BLOCKED (USB debugging detected in UI)")
        if verdict == "Unknown":
            verdict = self._check_ui_keywords(driver, CAPTURE_KEYWORDS, "BLOCKED (Screen capture warning detected in UI)")
        if verdict == "Unknown":
            verdict = self._check_flag_secure(device_client)
        return verdict

    def _start_recording(self, driver) -> bool:
        try:
            driver.start_recording_screen()
            return True
        except Exception:
            return False

    def _stop_recording(self, driver, package_name: str, recordings_dir: Path) -> Path | None:
        try:
            encoded = driver.stop_recording_screen()
            path = recordings_dir / f"{package_name}.mp4"
            path.write_bytes(base64.b64decode(encoded))
            return path
        except Exception:
            return None

    def _close_app(self, driver, package_name: str) -> None:
        try:
            driver.terminate_app(package_name)
        except Exception:
            pass

    def _check_app_switch(self, driver, package_name: str) -> str:
        try:
            current_package = driver.current_package
        except Exception:
            return "Unknown"
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

    def _check_ui_keywords(self, driver, keywords: list[str], block_message: str) -> str:
        try:
            source = driver.page_source.lower()
        except Exception:
            return "Unknown"
        return block_message if any(keyword in source for keyword in keywords) else "Unknown"

    def _check_flag_secure(self, device_client) -> str:
        code, out, err = device_client.adb.run(["shell", "dumpsys", "window", "windows"])
        if code != 0:
            return f"ERROR (ADB check failed: {err})"
        if "FLAG_SECURE" in out:
            return "BLOCKED (FLAG_SECURE detected via ADB)"
        return "ALLOWED (UI visible and no security flags)"


def _status_from_verdict(verdict: str) -> str:
    if verdict.startswith("ALLOWED"):
        return "SCREEN_CAPTURE_ALLOWED"
    if verdict.startswith("BLOCKED"):
        return "SCREEN_CAPTURE_BLOCKED"
    if verdict.startswith("ERROR"):
        return "FAILED"
    return "UNKNOWN"


def _security_verdict_from_verdict(verdict: str) -> str:
    if verdict.startswith("ALLOWED"):
        return "At Risk"
    if verdict.startswith("BLOCKED"):
        return "Reduced Risk"
    return "Inconclusive"
