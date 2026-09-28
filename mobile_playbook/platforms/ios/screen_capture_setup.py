from __future__ import annotations

import json
import time
from pathlib import Path


class ScreenCaptureSetupError(Exception):
    def __init__(self, status: str, message: str, state: dict):
        super().__init__(message)
        self.status = status
        self.state = state


def start_broadcast(device_client, recorder: dict, report_dir: Path) -> dict:
    state: dict = {"stage": "launch"}
    bundle_id = recorder["bundle_id"]
    try:
        state["launch"] = device_client.launch_app(bundle_id)
        sign_in = recorder.get("sign_in") or {}
        if sign_in:
            state["stage"] = "sign_in"
            device_client.set_text_by_accessibility_id(sign_in["username_accessibility_id"], sign_in["username"])
            device_client.set_text_by_accessibility_id(sign_in["password_accessibility_id"], sign_in["password"])
        state["stage"] = "picker"
        device_client.tap_by_accessibility_id(recorder["start_button_accessibility_id"])
        state["stage"] = "start_broadcast"
        state["start_tap"] = device_client.tap_label(recorder["start_broadcast_labels"], recorder.get("sheet_timeout_seconds", 10))
    except Exception as exc:
        state["diagnostics"] = device_client.save_diagnostics(report_dir, f"broadcast-{state['stage']}")
        raise ScreenCaptureSetupError("BROADCAST_NOT_STARTED", f"Could not start the broadcast at {state['stage']}: {exc}", state) from exc
    time.sleep(float(recorder.get("countdown_seconds", 4)))
    state["stage"] = "recording"
    state["started_at_ms"] = int(time.time() * 1000)
    return state


def stop_broadcast(device_client, recorder: dict) -> dict:
    device_client.launch_app(recorder["bundle_id"])
    device_client.tap_by_accessibility_id(recorder["start_button_accessibility_id"])
    return device_client.tap_label(recorder["stop_broadcast_labels"], recorder.get("sheet_timeout_seconds", 10))


def export_and_pull(device_client, recorder: dict, dest: Path, max_bytes: int) -> dict:
    try:
        device_client.launch_app(recorder["bundle_id"])
        device_client.tap_by_accessibility_id(recorder["export_button_accessibility_id"])
    except Exception as exc:
        raise ScreenCaptureSetupError("RECORDING_RETRIEVAL_FAILED", f"Could not export the evidence: {exc}", {}) from exc
    subpath = recorder.get("evidence_documents_path", "Evidence")
    deadline = time.monotonic() + float(recorder.get("export_timeout_seconds", 30))
    manifest_path = dest / "done.json"
    files: list[Path] = []
    last_error: str | None = None
    while True:
        # The Evidence folder may not exist until the export finishes, so a failed pull is retried.
        try:
            files = device_client.pull_app_documents(recorder["bundle_id"], subpath, dest, max_bytes=max_bytes)
            last_error = None
        except Exception as exc:
            last_error = str(exc)
        if manifest_path.exists() or time.monotonic() > deadline:
            break
        time.sleep(1)
    if not manifest_path.exists():
        message = f"Could not pull the evidence: {last_error}" if last_error else "The recorder never wrote done.json"
        raise ScreenCaptureSetupError("RECORDING_RETRIEVAL_FAILED", message, {"files": [str(f) for f in files], "error": last_error})
    manifest = json.loads(manifest_path.read_text())
    recording = dest / manifest["recording"] if manifest.get("recording") else None
    frames = [dest / name for name in manifest.get("screenshots") or [] if (dest / name).exists()]
    return {"manifest": manifest, "recording": recording if recording and recording.exists() else None, "frames": frames}
