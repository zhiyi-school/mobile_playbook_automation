from __future__ import annotations

import json
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from mobile_playbook.platforms.ios import screen_capture_ocr as ocr
from mobile_playbook.platforms.ios.config import effective_risk_config
from mobile_playbook.platforms.ios.models import RiskRunResult
from mobile_playbook.platforms.ios.risks.companion_app_base import CompanionAppRiskBase
from mobile_playbook.platforms.ios.screen_capture_setup import (
    ScreenCaptureSetupError, export_and_pull, start_broadcast, stop_broadcast,
)

SECURE_FIELD_SELECTOR = {"ios_class_chain": "**/XCUIElementTypeSecureTextField"}


class Feature03Risk01(CompanionAppRiskBase):
    risk_id = "ios-feature-03-risk-01"
    feature_id = "feature-03"
    name = "Capture on-screen content"
    requires_ipa_artifact = False
    companion_config_key = "recorder_app"
    companion_label = "recorder"

    def __init__(self, recogniser=None):
        self.recogniser = recogniser  # injected by tests

    def run(self, app_config, global_config, device_client, report_writer):
        result = self._base_result(report_writer.run_timestamp, app_config)
        report_dir = report_writer.test_report_dir(app_config.id, self.risk_id, "screen_capture")
        cfg = effective_risk_config(global_config, self.risk_id, app_config.risks.get(self.risk_id))
        recorder, capture = cfg.get("recorder_app") or {}, cfg.get("capture") or {}
        result.launch_result = {}
        installed_recorder = installed_target = False
        broadcast = None
        try:
            # Uninstalling the recorder also wipes its App Group, so no earlier recording can match this run.
            recorder_bundle_id = recorder.get("bundle_id")
            require_clean = bool(recorder.get("require_clean_state", True))
            preexisting = bool(recorder_bundle_id and device_client.is_installed(recorder_bundle_id))
            result.launch_result["starting_state"] = {"recorder_preinstalled": preexisting, "require_clean_state": require_clean}
            if preexisting and require_clean:
                outcome = device_client.remove_app_verified(recorder_bundle_id)
                result.launch_result["starting_state"]["reset"] = outcome
                if not outcome["verified"]:
                    result.final_status, result.verdict = "DIRTY_STARTING_STATE", "Inconclusive"
                    result.errors.append(f"{recorder_bundle_id} was left installed by an earlier run and could not be removed")
                    return result

            recorder_setup = self._install_or_verify_companion_app(recorder, global_config, device_client)
            result.launch_result["recorder_app"] = recorder_setup
            if recorder_setup.get("status") not in {"INSTALLED", "INSTALLED_APP_VERIFIED", "SKIPPED"}:
                result.final_status = "INSTALL_FAILED" if recorder_setup.get("status") == "INSTALL_FAILED" else "ARTIFACT_REQUIRED"
                result.errors.extend(recorder_setup.get("errors") or [])
                return result
            installed_recorder = bool(recorder_setup.get("installed_by_risk"))

            acquisition = self._prepare_app(app_config, global_config, device_client, report_writer.run_timestamp)
            result.artifact_result = acquisition
            if acquisition.status not in {"ACQUIRED", "INSTALLED_APP_VERIFIED"}:
                result.final_status = self._artifact_status_to_final(acquisition.status)
                result.errors.extend(acquisition.errors)
                return result
            if acquisition.ipa_path is not None:
                install = device_client.install_app(acquisition.ipa_path, global_config.runner.app_install_timeout_ms)
                result.install_result = install
                installed_target = install.status == "INSTALLED"
                if install.status != "INSTALLED":
                    result.final_status = "INSTALL_FAILED"
                    result.errors.extend(install.errors)
                    return result

            broadcast = start_broadcast(device_client, recorder, report_dir)
            result.launch_result["broadcast"] = broadcast

            canaries = self._canaries(report_writer.run_timestamp, capture)
            window_start = int(time.time() * 1000)
            device_client.launch_app(app_config.bundle_id)
            result.launch_result["app_permission_alerts"] = self._handle_permission_alerts(device_client, global_config)
            result.launch_result["probe"] = self._reveal(device_client, report_dir, capture, global_config, canaries)
            time.sleep(float(capture.get("capture_window_seconds", 12)))
            device_client.screenshot(report_dir / "reference_screen.png")
            window_end = int(time.time() * 1000)

            result.launch_result["broadcast_stop"] = stop_broadcast(device_client, recorder)
            broadcast = None
            pulled = export_and_pull(device_client, recorder, report_dir / "pulled", int(capture.get("max_pull_bytes", 209_715_200)))
            recording = self._keep_recording(pulled["recording"], report_dir)
            result.launch_result["recording"] = {
                "path": str(recording) if recording else None,
                "bytes": recording.stat().st_size if recording else 0,
                "valid": bool(recording and self._looks_like_mp4(recording)),
                "manifest": pulled["manifest"],
            }
            result.final_status, result.verdict = self._verdict(
                pulled, recording, window_start, window_end, canaries, capture, report_dir, result
            )
            return result
        except ScreenCaptureSetupError as exc:
            result.launch_result["broadcast_error"] = exc.state
            result.final_status = exc.status
            result.errors.append(str(exc))
            return result
        except Exception as exc:
            result.final_status = "FAILED"
            result.errors.append(str(exc))
            return result
        finally:
            if broadcast is not None:
                try:
                    stop_broadcast(device_client, recorder)
                except Exception as exc:
                    result.errors.append(f"Could not stop the broadcast: {exc}")
            result.cleanup_result = self._cleanup(app_config, global_config, device_client, installed_target,
                                                  recorder, installed_recorder)
            result.timestamp_end = datetime.now(timezone.utc).isoformat()
            report_writer.write_result(result, report_dir)

    def _verdict(self, pulled, recording, start_ms, end_ms, canaries, capture, report_dir, result):
        if not recording and not pulled["frames"]:
            return "BROADCAST_NOT_STARTED", "Inconclusive"
        if capture.get("ocr_provider", "vision") == "none" or (self.recogniser is None and not ocr.ocr_available()):
            result.errors.append("OCR unavailable; the screen recording is attached for manual review")
            return "OCR_UNAVAILABLE", "Inconclusive"
        frames, source = ocr.frames_in_window(pulled["frames"], start_ms, end_ms), "screenshots"
        if not frames and recording and capture.get("video_frame_fallback", True):
            started_ms = int(pulled["manifest"].get("recordingStartedAt") or 0) * 1000
            frames = ocr.extract_frames(recording, started_ms, start_ms, end_ms,
                                        float(capture.get("video_frame_interval_seconds", 1)), report_dir / "video_frames")
            source = "video"
        scan = ocr.scan_frames(frames, canaries, self.recogniser or ocr.recognise_text)
        scan.update({"frames_source": source,
                     "secure_field_exposed": any(m["canary_kind"] == "secure" for m in scan["matches"])})
        (report_dir / "ocr_matches.json").write_text(json.dumps(scan, indent=2))
        result.launch_result["ocr"] = {k: v for k, v in scan.items() if k != "matches"} | {"match_count": len(scan["matches"])}
        self._keep_matched_frames(scan, report_dir)
        if scan["matches"]:
            return "RISK_EXISTS", "At Risk"
        if frames and len(scan["obscured_frames"]) == len(frames):
            return "SCREEN_CAPTURE_OBSCURED", "Reduced Risk"
        if not frames:
            return "BROADCAST_NOT_STARTED", "Inconclusive"
        result.errors.append("Recorded frames were readable but no canary was recognised; the screen recording is attached for manual review")
        return "CANARY_NOT_OBSERVED", "Inconclusive"

    def _canaries(self, run_timestamp: str, capture: dict) -> dict[str, str]:
        suffix = "".join(ch for ch in run_timestamp if ch.isalnum())[-6:]
        canaries = {"plain": f"{capture.get('canary_prefix', 'SCR')}{suffix}"}
        if capture.get("type_secure_canary", True):
            canaries["secure"] = f"PWD{suffix}"
        return canaries

    def _reveal(self, device_client, report_dir, capture, global_config, canaries) -> dict:
        """Type the plain canary into a normal field and, where there is one, the secure canary into a SecureField."""
        focus = self._focus_text_field_with_navigation(device_client, report_dir, capture, global_config)
        typed = {"plain": device_client.type_text(canaries["plain"], capture.get("input") or {})}
        if "secure" in canaries:
            try:
                secure_focus = device_client.tap_text_field(SECURE_FIELD_SELECTOR)
            except Exception as exc:
                typed["secure"] = {"status": "NO_SECURE_FIELD", "error": str(exc)}
            else:
                typed["secure"] = {"focus": secure_focus, "input": device_client.type_text(canaries["secure"], capture.get("input") or {})}
        return {"focus": focus, "typed": typed}

    def _keep_recording(self, recording: Path | None, report_dir: Path) -> Path | None:
        if recording is None:
            return None
        kept = report_dir / "recording.mp4"
        shutil.move(str(recording), kept)
        return kept

    def _looks_like_mp4(self, path: Path) -> bool:
        head = path.read_bytes()[:4096]
        return b"ftyp" in head and path.stat().st_size > 1024

    def _keep_matched_frames(self, scan: dict, report_dir: Path) -> None:
        frames_dir = report_dir / "frames"
        for frame in dict.fromkeys(match["frame"] for match in scan["matches"]):
            frames_dir.mkdir(exist_ok=True)
            shutil.copy2(frame, frames_dir / Path(frame).name)

    def _base_result(self, run_timestamp: str, app_config) -> RiskRunResult:
        return RiskRunResult(
            run_timestamp=run_timestamp,
            timestamp_start=datetime.now(timezone.utc).isoformat(),
            timestamp_end=None,
            app_id=app_config.id,
            app_name=app_config.name,
            original_bundle_id=app_config.bundle_id,
            test_bundle_id=app_config.test_bundle_id,
            risk_id=self.risk_id,
            feature_id=self.feature_id,
            test_case_id="screen_capture",
            test_case_type="replaykit_broadcast",
            artifact_source=app_config.artifact.get("source", ""),
        )
