from __future__ import annotations

import json
import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)


class ScreenCaptureSetupError(Exception):
    def __init__(self, status: str, message: str, state: dict):
        super().__init__(message)
        self.status = status
        self.state = state


def start_broadcast(device_client, recorder: dict, report_dir: Path) -> dict:
    state: dict = {"stage": "launch"}
    bundle_id = recorder["bundle_id"]
    try:
        logger.debug("ios screen capture: launching recorder %s", bundle_id)
        state["launch"] = device_client.launch_app(bundle_id)
        logger.debug("ios screen capture: recorder launch -> %s", state["launch"])
        sign_in = recorder.get("sign_in") or {}
        if sign_in:
            state["stage"] = "sign_in"
            logger.debug("ios screen capture: signing in to recorder (username field %s)", sign_in.get("username_accessibility_id"))
            device_client.set_text_by_accessibility_id(sign_in["username_accessibility_id"], sign_in["username"])
            device_client.set_text_by_accessibility_id(sign_in["password_accessibility_id"], sign_in["password"])
        state["stage"] = "picker"
        logger.debug("ios screen capture: opening broadcast picker via %s", recorder["start_button_accessibility_id"])
        device_client.tap_by_accessibility_id(recorder["start_button_accessibility_id"])
        state["stage"] = "start_broadcast"
        logger.debug("ios screen capture: tapping start broadcast labels %s", recorder["start_broadcast_labels"])
        state["start_tap"] = device_client.tap_label(recorder["start_broadcast_labels"], recorder.get("sheet_timeout_seconds", 10))
        logger.debug("ios screen capture: start tap -> %s", state["start_tap"])
    except Exception as exc:
        logger.debug("ios screen capture: broadcast start failed at stage %s: %s", state["stage"], exc, exc_info=True)
        state["diagnostics"] = device_client.save_diagnostics(report_dir, f"broadcast-{state['stage']}")
        raise ScreenCaptureSetupError("BROADCAST_NOT_STARTED", f"Could not start the broadcast at {state['stage']}: {exc}", state) from exc
    logger.debug("ios screen capture: waiting %ss for broadcast countdown", recorder.get("countdown_seconds", 4))
    time.sleep(float(recorder.get("countdown_seconds", 4)))
    state["stage"] = "recording"
    state["started_at_ms"] = int(time.time() * 1000)
    logger.debug("ios screen capture: recording since %s ms", state["started_at_ms"])
    return state


def stop_broadcast(device_client, recorder: dict) -> dict:
    logger.debug("ios screen capture: stopping broadcast via %s", recorder["bundle_id"])
    device_client.launch_app(recorder["bundle_id"])
    device_client.tap_by_accessibility_id(recorder["start_button_accessibility_id"])
    result = device_client.tap_label(recorder["stop_broadcast_labels"], recorder.get("sheet_timeout_seconds", 10))
    logger.debug("ios screen capture: stop tap -> %s", result)
    return result


def export_and_pull(device_client, recorder: dict, dest: Path, max_bytes: int) -> dict:
    try:
        logger.debug("ios screen capture: exporting evidence via %s", recorder["export_button_accessibility_id"])
        device_client.launch_app(recorder["bundle_id"])
        device_client.tap_by_accessibility_id(recorder["export_button_accessibility_id"])
    except Exception as exc:
        logger.debug("ios screen capture: export tap failed: %s", exc, exc_info=True)
        raise ScreenCaptureSetupError("RECORDING_RETRIEVAL_FAILED", f"Could not export the evidence: {exc}", {}) from exc
    subpath = recorder.get("evidence_documents_path", "Evidence")
    deadline = time.monotonic() + float(recorder.get("export_timeout_seconds", 30))
    manifest_path = dest / "done.json"
    files: list[Path] = []
    last_error: str | None = None
    logger.debug(
        "ios screen capture: pulling Documents/%s to %s (max_bytes=%s, timeout=%ss)",
        subpath,
        dest,
        max_bytes,
        recorder.get("export_timeout_seconds", 30),
    )
    attempts = 0
    while True:
        attempts += 1
        # The Evidence folder may not exist until the export finishes, so a failed pull is retried.
        try:
            files = device_client.pull_app_documents(recorder["bundle_id"], subpath, dest, max_bytes=max_bytes)
            last_error = None
            logger.debug("ios screen capture: pull attempt %s got %s files", attempts, len(files))
        except Exception as exc:
            logger.debug("ios screen capture: pull attempt %s failed: %s", attempts, exc, exc_info=True)
            last_error = str(exc)
        if manifest_path.exists() or time.monotonic() > deadline:
            break
        time.sleep(1)
    if not manifest_path.exists():
        logger.debug("ios screen capture: done.json missing after %s attempts (last_error=%s)", attempts, last_error)
        message = f"Could not pull the evidence: {last_error}" if last_error else "The recorder never wrote done.json"
        raise ScreenCaptureSetupError("RECORDING_RETRIEVAL_FAILED", message, {"files": [str(f) for f in files], "error": last_error})
    manifest = json.loads(manifest_path.read_text())
    recording = dest / manifest["recording"] if manifest.get("recording") else None
    frames = [dest / name for name in manifest.get("screenshots") or [] if (dest / name).exists()]
    logger.debug(
        "ios screen capture: manifest recording=%s (present=%s), %s of %s listed screenshots present",
        manifest.get("recording"),
        bool(recording and recording.exists()),
        len(frames),
        len(manifest.get("screenshots") or []),
    )
    return {"manifest": manifest, "recording": recording if recording and recording.exists() else None, "frames": frames}
