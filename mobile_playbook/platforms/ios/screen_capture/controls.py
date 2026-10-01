"""
Records and checks target visibility and recorder output before and after the target capture window.
"""

from __future__ import annotations

import math
import shutil
import time
from pathlib import Path
from uuid import uuid4

from selenium.common.exceptions import WebDriverException

from mobile_playbook.platforms.ios.screen_capture import ocr

_DEVICE_ERRORS = (OSError, RuntimeError, WebDriverException)


# Use a unique dummy recorder username as known text without changing stored configuration.
def prepare(recorder: dict, capture: dict, report_dir: Path) -> tuple[dict, dict]:
    enabled = (capture.get("controls") or {}).get("enabled", True) and capture.get("ocr_provider", "tesseract") != "none"
    if not enabled:
        return recorder, {"enabled": False, "status": "DISABLED"}
    sign_in = recorder.get("sign_in") or {}
    token = "CTL" + uuid4().hex[:12].upper() if sign_in.get("username_accessibility_id") and sign_in.get("password") else None
    configured = {**recorder, "sign_in": {**sign_in, "username": token}} if token else recorder
    state = {"enabled": True, "status": "NOT_ANALYSED", "recorder_text": token,
             "evidence_path": str(report_dir / "capture_controls.json"),
             "target_reference": {"capture_status": "NOT_CAPTURED"}}
    return configured, state


# Save a fresh reference and verify the expected app is foreground; keep device failures diagnostic.
def record_reference(device, bundle_id: str, path: Path) -> dict:
    reference = {"path": str(path), "capture_status": "CAPTURE_FAILED"}
    try:
        previous = path.stat().st_mtime_ns if path.exists() else None
        reference["app_state"] = device.query_app_state(bundle_id)
        device.screenshot(path)
        if not path.is_file() or path.stat().st_size == 0 or path.stat().st_mtime_ns == previous:
            raise RuntimeError("The reference screenshot was not freshly saved")
        reference["app_state_after"] = device.query_app_state(bundle_id)
        reference["capture_status"] = "CAPTURED" if reference["app_state"] == reference["app_state_after"] == 4 else "NOT_FOREGROUND"
    except _DEVICE_ERRORS as exc:
        reference["error"] = str(exc)
    reference["captured_at_ms"] = int(time.time() * 1000)
    return reference


# Bound host/device clock offset using the whole-second response and request latency.
def _clock(device) -> dict:
    read = getattr(device, "device_time_ms", None)
    if read is None:
        return {"status": "UNAVAILABLE"}
    start_ms = int(time.time() * 1000)
    try:
        device_ms = read()
    except _DEVICE_ERRORS as exc:
        return {"status": "UNAVAILABLE", "error": str(exc)}
    end_ms = int(time.time() * 1000)
    return {"status": "MEASURED", "offset_min_ms": device_ms - end_ms,
            "offset_max_ms": device_ms + 1000 - start_ms}


# Verify target visibility at both ends and poll foreground state during the canary hold.
def record_target(device, bundle_id: str, state: dict, report_dir: Path, hold_seconds: float) -> None:
    if not math.isfinite(hold_seconds) or hold_seconds <= 0:
        raise ValueError("The target hold must be finite and greater than zero")
    state["target_start_reference"] = record_reference(device, bundle_id, report_dir / "control_target_start_reference.png")
    window = {"clock_before": _clock(device), "foreground": [], "start_ms": int(time.time() * 1000)}
    state["target_window"] = window
    remaining = hold_seconds
    while True:
        observation = {"at_ms": int(time.time() * 1000)}
        try:
            observation["app_state"] = device.query_app_state(bundle_id)
        except _DEVICE_ERRORS as exc:
            observation["error"] = str(exc)
        window["foreground"].append(observation)
        if remaining <= 0:
            break
        step = min(1.0, remaining)
        time.sleep(step)
        remaining -= step
    window["end_ms"] = int(time.time() * 1000)
    window["clock_after"] = _clock(device)
    state["target_reference"] = record_reference(device, bundle_id, report_dir / "reference_screen.png")


# Hold the recorder onscreen within a separate control window and retain its Appium reference.
def record_recorder(device, recorder: dict, state: dict, phase: str, report_dir: Path, hold_seconds: float) -> None:
    if not state.get("recorder_text"):
        state[phase] = {"status": "NOT_CONFIGURED"}
        return
    window = {"start_ms": int(time.time() * 1000)}
    state[phase] = window
    try:
        device.activate_app(recorder["bundle_id"])
        window["start_ms"] = int(time.time() * 1000)
        time.sleep(hold_seconds)
        window["reference"] = record_reference(device, recorder["bundle_id"], report_dir / f"control_{phase}_reference.png")
    except _DEVICE_ERRORS as exc:
        window["reference"] = {"capture_status": "CAPTURE_FAILED", "error": str(exc)}
    window["end_ms"] = int(time.time() * 1000)


# Confirm expected text in a reference captured while the expected app was foreground.
def analyse_reference(reference: dict, expected: str, recogniser) -> dict:
    if reference.get("capture_status") != "CAPTURED":
        return {**reference, "status": reference.get("capture_status", "NOT_CAPTURED")}
    scan = ocr.scan_frames([Path(reference["path"])], {"control": expected}, recogniser)
    status = "OCR_FAILED" if scan["failed_frames"] else "PASSED" if scan["matches"] else "CANARY_NOT_OBSERVED"
    return {**reference, **scan, "status": status}


# Check independent references and same-broadcast recorder samples without treating controls as risk matches.
def analyse(state: dict, pulled: dict, recording: Path | None, plain_canary: str, capture: dict, recogniser, report_dir: Path) -> None:
    state["target_reference"] = analyse_reference(state["target_reference"], plain_canary, recogniser)
    if "target_start_reference" in state:
        state["target_start_reference"] = analyse_reference(state["target_start_reference"], plain_canary, recogniser)
    processing_failed = bool(state["target_reference"].get("failed_frames") or (state.get("target_start_reference") or {}).get("failed_frames"))
    for phase in ("before", "after"):
        window = state[phase]
        if window.get("status") == "NOT_CONFIGURED":
            continue
        window["reference"] = analyse_reference(window["reference"], state["recorder_text"], recogniser)
        # Strict bounds prevent a sample on a phase boundary from proving two different controls.
        start_ms, end_ms = window["start_ms"] + 1, window["end_ms"] - 1
        frames = ocr.frames_in_window(pulled["frames"], start_ms, end_ms)
        source, errors, unavailable = "screenshots", [], None
        if not frames and recording and capture.get("video_frame_fallback", True):
            source = "video"
            unavailable = ocr.video_unavailable_reason()
            if not unavailable:
                try:
                    started_ms = int(pulled["manifest"].get("recordingStartedAt") or 0) * 1000
                    extracted = ocr.extract_frames(recording, started_ms, start_ms, end_ms,
                                                   float(capture.get("video_frame_interval_seconds", 1)), report_dir / f"control_{phase}_video")
                    frames, errors = extracted["frames"], extracted["failures"]
                except ocr.OCRProcessingError as exc:
                    errors = [{"recording": str(recording), "stage": "video_setup", "error": str(exc)}]
        scan = ocr.scan_frames(frames, {"recorder": state["recorder_text"]}, recogniser)
        if unavailable:
            status = "UNAVAILABLE"
        elif errors or scan["failed_frames"]:
            status = "OCR_FAILED"
        elif scan["matches"]:
            status = "PASSED"
        else:
            status = "CANARY_NOT_OBSERVED" if frames else "NO_FRAMES"
        window["broadcast"] = {**scan, "status": status, "frames_source": source,
                               "video_errors": errors, "unavailable_reason": unavailable, "evidence_frames": []}
        window["status"] = status if window["reference"]["status"] == "PASSED" else window["reference"]["status"]
        processing_failed |= bool(errors or scan["failed_frames"] or window["reference"].get("failed_frames"))
        for frame in dict.fromkeys(match["frame"] for match in scan["matches"]):
            dest = report_dir / "control_frames" / f"{phase}-{Path(frame).name}"
            dest.parent.mkdir(exist_ok=True)
            shutil.copy2(frame, dest)
            window["broadcast"]["evidence_frames"].append(str(dest))
    visible = state["target_reference"]["status"] == "PASSED" and (state.get("target_start_reference") or {"status": "PASSED"})["status"] == "PASSED"
    recorder_passed = all(state[phase]["status"] == "PASSED" for phase in ("before", "after"))
    state["status"] = "PASSED" if visible and recorder_passed else "UNVERIFIED"
    state["processing_failed"] = processing_failed
    reference_status = (state.get("target_start_reference") or {}).get("status") if state["target_reference"]["status"] == "PASSED" else state["target_reference"]["status"]
    target_note = "Target visibility verified" if visible else f"Target visibility unverified ({reference_status})"
    failed_phases = ", ".join(f"{phase}: {state[phase]['status']}" for phase in ("before", "after") if state[phase]["status"] != "PASSED")
    recorder_note = "recorder controls passed" if recorder_passed else f"recorder controls unverified ({failed_phases})"
    state["diagnostic"] = f"{target_note}; {recorder_note}; capture protection unverified"
