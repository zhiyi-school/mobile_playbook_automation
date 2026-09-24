from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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

    def to_dict(self) -> dict[str, Any]:
        return {"verdict": self.verdict, "divergences": self.divergences}


def tamper_markers(cfg: dict[str, Any], expected_behavior_markers: list[str] | None = None) -> list[str]:
    markers = [str(m) for m in (cfg.get("tamper_markers") or []) if str(m).strip()]
    markers += [str(m) for m in (expected_behavior_markers or []) if str(m).strip()]
    return sorted(set(markers))


def _sample_count(exercise_cfg: dict[str, Any]) -> int:
    explicit = exercise_cfg.get("state_samples")
    if explicit is not None:
        return max(1, int(explicit))
    interval = float(exercise_cfg.get("sample_interval_seconds", 1) or 0)
    window = float(exercise_cfg.get("sample_window_seconds", 15))
    if interval <= 0:
        return 1
    return max(1, round(window / interval))


def _sample_states(device_client, bundle_id: str, exercise_cfg: dict[str, Any], obs: RunObservation) -> None:
    interval = float(exercise_cfg.get("sample_interval_seconds", 1) or 0)
    samples = _sample_count(exercise_cfg)
    reached_foreground = False
    for index in range(samples):
        try:
            state = int(device_client.query_app_state(bundle_id))
        except Exception as exc:
            obs.errors.append(f"query_app_state failed: {exc}")
            state = -1
        obs.app_states.append(state)
        if state == APP_STATE_FOREGROUND:
            obs.foreground_samples += 1
            reached_foreground = True
        elif reached_foreground and state == APP_STATE_NOT_RUNNING:
            obs.terminated_midwindow = True
        if interval > 0 and index < samples - 1:
            time.sleep(interval)


def _exercise(device_client, exercise_cfg: dict[str, Any]) -> tuple[list[dict[str, Any]], bool | None]:
    steps: list[dict[str, Any]] = []
    reached: bool | None = None
    if exercise_cfg.get("tap_text_field"):
        steps.append(_safe_step("tap_text_field", lambda: device_client.tap_text_field(exercise_cfg.get("text_field_selector"))))
    labels = [str(label) for label in (exercise_cfg.get("button_label_contains") or []) if str(label).strip()]
    if labels:
        step = _safe_step("tap_button", lambda: device_client.tap_first_button_matching(labels))
        steps.append(step)
        reached = bool(step["ok"])
    return steps, reached


def _safe_step(name: str, action) -> dict[str, Any]:
    try:
        result = action()
        return {"step": name, "ok": True, "result": result}
    except Exception as exc:
        return {"step": name, "ok": False, "error": str(exc)}


def exercise_and_observe(device_client, bundle_id: str, cfg: dict[str, Any], evidence_dir: Path, label: str) -> RunObservation:
    obs = RunObservation(label=label)
    evidence_dir = Path(evidence_dir)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    exercise_cfg = cfg.get("exercise") or {}

    recording = _start_recording(device_client)
    try:
        device_client.launch_app(bundle_id)
    except Exception as exc:
        obs.errors.append(f"launch failed: {exc}")

    _sample_states(device_client, bundle_id, exercise_cfg, obs)
    obs.drivable = obs.foreground_samples > 0

    try:
        source = device_client.page_source()
    except Exception as exc:
        obs.errors.append(f"page_source failed: {exc}")
        source = ""
    obs.tamper_markers_found = [marker for marker in tamper_markers(cfg, cfg.get("source_not_contains")) if marker in source]

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
    except Exception as exc:
        obs.errors.append(f"screenshot failed: {exc}")
    (evidence_dir / f"{label}_page_source.xml").write_text(source)

    obs.recording_path = _stop_recording(recording, evidence_dir / f"{label}_recording.mp4")
    obs.device_log_path = _capture_device_log(device_client, evidence_dir / f"{label}_device.log")
    return obs


def compare(baseline: RunObservation, repackaged: RunObservation, cfg: dict[str, Any]) -> EvidenceVerdict:
    """SURVIVED only when the repackaged build matches the clean baseline on every channel."""
    divergences: list[str] = []
    if baseline.drivable and not repackaged.drivable:
        divergences.append("Repackaged build did not stay in the foreground while the clean baseline did")
    if repackaged.terminated_midwindow and not baseline.terminated_midwindow:
        divergences.append("Repackaged build was terminated during the sampling window")
    new_markers = sorted(set(repackaged.tamper_markers_found) - set(baseline.tamper_markers_found))
    if new_markers:
        divergences.append(f"Tamper markers surfaced only after repackaging: {', '.join(new_markers)}")
    if baseline.sensitive_path_reached and repackaged.sensitive_path_reached is False:
        divergences.append("Sensitive path was reachable on the clean baseline but not after repackaging")
    return EvidenceVerdict("SURVIVED" if not divergences else "BLOCKED", divergences)


def confirm_gadget(cfg: dict[str, Any], bundle_id: str, evidence_dir: Path) -> dict[str, Any]:
    """Attach to the injected Frida Gadget and run the configured script; proof it loaded and ran."""
    frida_cfg = cfg.get("frida") or {}
    target = str(frida_cfg.get("attach_target") or bundle_id)
    timeout = float(frida_cfg.get("attach_timeout_seconds", 15))
    script_path = frida_cfg.get("script")
    messages: list[Any] = []
    try:
        import frida
    except Exception as exc:
        return {"ok": False, "errors": [f"frida is not installed: {exc}"], "target": target, "messages": messages}
    session = None
    try:
        device = frida.get_usb_device(timeout=timeout)
        session = device.attach(target)
        if script_path:
            script = session.create_script(Path(script_path).read_text())
            script.on("message", lambda message, data: messages.append(message))
            script.load()
            time.sleep(float(frida_cfg.get("script_wait_seconds", 2)))
        (Path(evidence_dir) / "frida_messages.json").write_text(json.dumps(messages, indent=2, default=str))
        return {"ok": True, "target": target, "script": str(script_path) if script_path else None, "messages": messages}
    except Exception as exc:
        return {"ok": False, "errors": [str(exc)], "target": target, "messages": messages}
    finally:
        if session is not None:
            try:
                session.detach()
            except Exception:
                pass


def _start_recording(device_client):
    driver = getattr(device_client, "driver", None)
    if driver is None or not hasattr(driver, "start_recording_screen"):
        return None
    try:
        driver.start_recording_screen()
        return driver
    except Exception:
        return None


def _stop_recording(driver, out_path: Path) -> str | None:
    if driver is None:
        return None
    try:
        encoded = driver.stop_recording_screen()
        out_path.write_bytes(base64.b64decode(encoded))
        return str(out_path)
    except Exception:
        return None


def _capture_device_log(device_client, out_path: Path) -> str | None:
    driver = getattr(device_client, "driver", None)
    if driver is None or not hasattr(driver, "get_log"):
        return None
    try:
        entries = driver.get_log("syslog")
    except Exception:
        return None
    try:
        out_path.write_text("\n".join(str(entry.get("message", entry)) for entry in entries))
    except Exception:
        return None
    return str(out_path)
