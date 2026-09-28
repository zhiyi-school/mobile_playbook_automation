"""
Observes baseline and repackaged app runs and compares them to judge whether repackaging survived.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# iOS Appium app-state codes: 1 not running, 2 background suspended, 3 background, 4 foreground.
APP_STATE_FOREGROUND = 4
APP_STATE_NOT_RUNNING = 1


@dataclass
class RunObservation:
    label: str
    drivable: bool = False
    app_states: list[int] = field(default_factory=list)
    foreground_samples: int = 0
    terminated_midwindow: bool = False
    tamper_markers_found: list[str] = field(default_factory=list)
    sensitive_path_reached: bool | None = None
    fingerprint: dict[str, Any] = field(default_factory=dict)
    exercise_steps: list[dict[str, Any]] = field(default_factory=list)
    screenshots: list[str] = field(default_factory=list)
    recording_path: str | None = None
    device_log_path: str | None = None
    errors: list[str] = field(default_factory=list)

    # Return the observation as a JSON-compatible dict.
    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "drivable": self.drivable,
            "app_states": self.app_states,
            "foreground_samples": self.foreground_samples,
            "terminated_midwindow": self.terminated_midwindow,
            "tamper_markers_found": self.tamper_markers_found,
            "sensitive_path_reached": self.sensitive_path_reached,
            "fingerprint": self.fingerprint,
            "exercise_steps": self.exercise_steps,
            "screenshots": self.screenshots,
            "recording_path": self.recording_path,
            "device_log_path": self.device_log_path,
            "errors": self.errors,
        }


@dataclass
class EvidenceVerdict:
    verdict: str
    divergences: list[str] = field(default_factory=list)

    # Return the verdict and divergences as a dict.
    def to_dict(self) -> dict[str, Any]:
        return {"verdict": self.verdict, "divergences": self.divergences}


# Return the sorted unique non-blank tamper markers from config and expected-behavior markers.
def tamper_markers(cfg: dict[str, Any], expected_behavior_markers: list[str] | None = None) -> list[str]:
    markers = [str(m) for m in (cfg.get("tamper_markers") or []) if str(m).strip()]
    markers += [str(m) for m in (expected_behavior_markers or []) if str(m).strip()]
    return sorted(set(markers))


# Return how many app-state samples to take, from an explicit count or the window and interval.
def _sample_count(exercise_cfg: dict[str, Any]) -> int:
    explicit = exercise_cfg.get("state_samples")
    if explicit is not None:
        return max(1, int(explicit))
    interval = float(exercise_cfg.get("sample_interval_seconds", 1) or 0)
    window = float(exercise_cfg.get("sample_window_seconds", 15))
    if interval <= 0:
        return 1
    return max(1, round(window / interval))


# Sample the app state repeatedly, recording foreground samples and termination after foreground.
def _sample_states(device_client, bundle_id: str, exercise_cfg: dict[str, Any], obs: RunObservation) -> None:
    interval = float(exercise_cfg.get("sample_interval_seconds", 1) or 0)
    samples = _sample_count(exercise_cfg)
    reached_foreground = False
    logger.debug("ios repackaging[%s]: sampling app state of %s samples=%d interval=%s", obs.label, bundle_id, samples, interval)
    for index in range(samples):
        try:
            state = int(device_client.query_app_state(bundle_id))
        except Exception as exc:
            logger.debug("ios repackaging[%s]: query_app_state failed: %s", obs.label, exc, exc_info=True)
            obs.errors.append(f"query_app_state failed: {exc}")
            state = -1
        obs.app_states.append(state)
        if state == APP_STATE_FOREGROUND:
            obs.foreground_samples += 1
            reached_foreground = True
        elif reached_foreground and state == APP_STATE_NOT_RUNNING:
            obs.terminated_midwindow = True
            logger.debug("ios repackaging[%s]: app stopped running after reaching foreground at sample %d", obs.label, index)
        logger.debug("ios repackaging[%s]: sample %d/%d state=%s", obs.label, index + 1, samples, state)
        if interval > 0 and index < samples - 1:
            time.sleep(interval)
    logger.debug(
        "ios repackaging[%s]: sampling done states=%s foreground_samples=%d terminated_midwindow=%s",
        obs.label, obs.app_states, obs.foreground_samples, obs.terminated_midwindow,
    )


# Run the configured text-field and button steps; return them and whether the sensitive path was reached.
def _exercise(device_client, exercise_cfg: dict[str, Any]) -> tuple[list[dict[str, Any]], bool | None]:
    steps: list[dict[str, Any]] = []
    reached: bool | None = None
    if exercise_cfg.get("tap_text_field"):
        logger.debug("ios repackaging: exercise tap_text_field selector=%s", exercise_cfg.get("text_field_selector"))
        steps.append(_safe_step("tap_text_field", lambda: device_client.tap_text_field(exercise_cfg.get("text_field_selector"))))
    labels = [str(label) for label in (exercise_cfg.get("button_label_contains") or []) if str(label).strip()]
    if labels:
        logger.debug("ios repackaging: exercise tap_button label_contains=%s", labels)
        step = _safe_step("tap_button", lambda: device_client.tap_first_button_matching(labels))
        steps.append(step)
        reached = bool(step["ok"])
    logger.debug("ios repackaging: exercise steps=%d sensitive_path_reached=%s", len(steps), reached)
    return steps, reached


# Run an action and return a step record with its result or error.
def _safe_step(name: str, action) -> dict[str, Any]:
    try:
        result = action()
        logger.debug("ios repackaging: step %s ok result=%.200s", name, result)
        return {"step": name, "ok": True, "result": result}
    except Exception as exc:
        logger.debug("ios repackaging: step %s failed: %s", name, exc, exc_info=True)
        return {"step": name, "ok": False, "error": str(exc)}


# Launch and exercise the app while recording evidence, returning its behavioral fingerprint.
def exercise_and_observe(device_client, bundle_id: str, cfg: dict[str, Any], evidence_dir: Path, label: str) -> RunObservation:
    obs = RunObservation(label=label)
    evidence_dir = Path(evidence_dir)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    exercise_cfg = cfg.get("exercise") or {}
    logger.debug("ios repackaging[%s]: observing %s evidence_dir=%s", label, bundle_id, evidence_dir)

    recording = _start_recording(device_client)
    logger.debug("ios repackaging[%s]: screen recording started=%s; launching %s", label, recording is not None, bundle_id)
    try:
        device_client.launch_app(bundle_id)
    except Exception as exc:
        logger.debug("ios repackaging[%s]: launch failed: %s", label, exc, exc_info=True)
        obs.errors.append(f"launch failed: {exc}")

    _sample_states(device_client, bundle_id, exercise_cfg, obs)
    obs.drivable = obs.foreground_samples > 0

    try:
        source = device_client.page_source()
    except Exception as exc:
        logger.debug("ios repackaging[%s]: page_source failed: %s", label, exc, exc_info=True)
        obs.errors.append(f"page_source failed: {exc}")
        source = ""
    obs.tamper_markers_found = [marker for marker in tamper_markers(cfg, cfg.get("source_not_contains")) if marker in source]
    logger.debug(
        "ios repackaging[%s]: page source chars=%d tamper markers checked=%s found=%s",
        label, len(source) if isinstance(source, str) else -1, tamper_markers(cfg, cfg.get("source_not_contains")), obs.tamper_markers_found,
    )

    obs.exercise_steps, obs.sensitive_path_reached = _exercise(device_client, exercise_cfg)

    obs.fingerprint = {
        "reached_foreground": obs.drivable,
        "terminated_midwindow": obs.terminated_midwindow,
        "sensitive_path_reached": obs.sensitive_path_reached,
        "tamper_markers": obs.tamper_markers_found,
    }

    screenshot_path = evidence_dir / f"{label}_screen.png"
    try:
        device_client.screenshot(screenshot_path)
        obs.screenshots.append(str(screenshot_path))
        logger.debug("ios repackaging[%s]: screenshot captured %s", label, screenshot_path)
    except Exception as exc:
        logger.debug("ios repackaging[%s]: screenshot failed: %s", label, exc, exc_info=True)
        obs.errors.append(f"screenshot failed: {exc}")
    (evidence_dir / f"{label}_page_source.xml").write_text(source)
    logger.debug("ios repackaging[%s]: page source written %s", label, evidence_dir / f"{label}_page_source.xml")

    obs.recording_path = _stop_recording(recording, evidence_dir / f"{label}_recording.mp4")
    obs.device_log_path = _capture_device_log(device_client, evidence_dir / f"{label}_device.log")
    logger.debug(
        "ios repackaging[%s]: observation drivable=%s fingerprint=%s recording=%s device_log=%s errors=%s",
        label, obs.drivable, obs.fingerprint, obs.recording_path, obs.device_log_path, obs.errors,
    )
    return obs


# Return SURVIVED only when the repackaged build matches the clean baseline on every channel.
def compare(baseline: RunObservation, repackaged: RunObservation, cfg: dict[str, Any]) -> EvidenceVerdict:
    divergences: list[str] = []
    logger.debug(
        "ios repackaging: comparing baseline fingerprint=%s with repackaged fingerprint=%s",
        baseline.fingerprint, repackaged.fingerprint,
    )
    if baseline.drivable and not repackaged.drivable:
        divergences.append("Repackaged build did not stay in the foreground while the clean baseline did")
    if repackaged.terminated_midwindow and not baseline.terminated_midwindow:
        divergences.append("Repackaged build was terminated during the sampling window")
    new_markers = sorted(set(repackaged.tamper_markers_found) - set(baseline.tamper_markers_found))
    if new_markers:
        divergences.append(f"Tamper markers surfaced only after repackaging: {', '.join(new_markers)}")
    if baseline.sensitive_path_reached and repackaged.sensitive_path_reached is False:
        divergences.append("Sensitive path was reachable on the clean baseline but not after repackaging")
    logger.debug("ios repackaging: comparison verdict=%s divergences=%s", "SURVIVED" if not divergences else "BLOCKED", divergences)
    return EvidenceVerdict("SURVIVED" if not divergences else "BLOCKED", divergences)


# Attach to the injected Frida Gadget and run the configured script as proof it loaded.
def confirm_gadget(cfg: dict[str, Any], bundle_id: str, evidence_dir: Path) -> dict[str, Any]:
    frida_cfg = cfg.get("frida") or {}
    target = str(frida_cfg.get("attach_target") or bundle_id)
    timeout = float(frida_cfg.get("attach_timeout_seconds", 15))
    script_path = frida_cfg.get("script")
    messages: list[Any] = []
    logger.debug("ios repackaging: confirming gadget target=%s timeout=%s script=%s", target, timeout, script_path)
    try:
        import frida
    except Exception as exc:
        logger.debug("ios repackaging: frida import failed: %s", exc, exc_info=True)
        return {"ok": False, "errors": [f"frida is not installed: {exc}"], "target": target, "messages": messages}
    session = None
    try:
        device = frida.get_usb_device(timeout=timeout)
        logger.debug("ios repackaging: frida usb device acquired; attaching to %s", target)
        session = device.attach(target)
        logger.debug("ios repackaging: attached to %s", target)
        if script_path:
            script = session.create_script(Path(script_path).read_text())
            script.on("message", lambda message, data: messages.append(message))
            script.load()
            time.sleep(float(frida_cfg.get("script_wait_seconds", 2)))
            logger.debug("ios repackaging: script %s loaded; messages received=%d", script_path, len(messages))
        (Path(evidence_dir) / "frida_messages.json").write_text(json.dumps(messages, indent=2, default=str))
        logger.debug("ios repackaging: gadget confirmed; messages written %s", Path(evidence_dir) / "frida_messages.json")
        return {"ok": True, "target": target, "script": str(script_path) if script_path else None, "messages": messages}
    except Exception as exc:
        logger.debug("ios repackaging: gadget attach failed for %s: %s", target, exc, exc_info=True)
        return {"ok": False, "errors": [str(exc)], "target": target, "messages": messages}
    finally:
        if session is not None:
            try:
                session.detach()
            except Exception:
                logger.debug("ios repackaging: frida session detach failed", exc_info=True)


# Start Appium screen recording and return the driver, or None when unavailable.
def _start_recording(device_client):
    driver = getattr(device_client, "driver", None)
    if driver is None or not hasattr(driver, "start_recording_screen"):
        logger.debug("ios repackaging: screen recording unavailable on this device client")
        return None
    try:
        driver.start_recording_screen()
        return driver
    except Exception:
        logger.debug("ios repackaging: start_recording_screen failed", exc_info=True)
        return None


# Stop screen recording and save the video, returning its path or None on failure.
def _stop_recording(driver, out_path: Path) -> str | None:
    if driver is None:
        return None
    try:
        encoded = driver.stop_recording_screen()
        out_path.write_bytes(base64.b64decode(encoded))
        logger.debug("ios repackaging: screen recording saved %s encoded_chars=%d", out_path, len(encoded))
        return str(out_path)
    except Exception:
        logger.debug("ios repackaging: stop_recording_screen failed for %s", out_path, exc_info=True)
        return None


# Save the device syslog through Appium, returning its path or None when unavailable.
def _capture_device_log(device_client, out_path: Path) -> str | None:
    driver = getattr(device_client, "driver", None)
    if driver is None or not hasattr(driver, "get_log"):
        logger.debug("ios repackaging: device syslog unavailable on this device client")
        return None
    try:
        entries = driver.get_log("syslog")
    except Exception:
        logger.debug("ios repackaging: get_log(syslog) failed", exc_info=True)
        return None
    try:
        out_path.write_text("\n".join(str(entry.get("message", entry)) for entry in entries))
    except Exception:
        logger.debug("ios repackaging: writing device log %s failed", out_path, exc_info=True)
        return None
    logger.debug("ios repackaging: device log written %s", out_path)
    return str(out_path)
