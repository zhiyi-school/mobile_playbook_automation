from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from mobile_playbook.platforms.ios import screen_capture_ocr
from mobile_playbook.platforms.ios.models import InstallResult
from mobile_playbook.platforms.ios.results import normalize_ios_result
from mobile_playbook.platforms.ios.risks.feature_03_risk_01 import Feature03Risk01
from mobile_playbook.report import ReportWriter
from tests.conftest import MockDevice

RECORDER = "com.example.recorder"
RUN = "20260924-134908"
PLAIN, SECURE = "SCR134908", "PWD134908"
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 2048


class RecorderDevice(MockDevice):
    def __init__(self, *, screenshots: bool = True, recording: bool = True, write_manifest: bool = True):
        super().__init__()
        self.screenshots = screenshots
        self.recording = recording
        self.write_manifest = write_manifest
        self.label_taps: list[list[str]] = []
        self.fail_labels: set[str] = set()
        self.pull_error: str | None = None
        self.stuck: set[str] = set()
        self.typed_at_ms: list[int] = []
        self.type_error: Exception | None = None

    def install_app(self, ipa_path: Path, timeout_ms: int) -> InstallResult:
        result = super().install_app(ipa_path, timeout_ms)
        self.installed.discard("com.example.app.test")
        self.installed.add(RECORDER if Path(ipa_path).name == "Recorder.ipa" else "com.example.app")
        return result

    def remove_app_verified(self, bundle_id: str, timeout_seconds: float = 20.0) -> dict:
        if bundle_id in self.stuck:
            return {"requested": True, "verified": False}
        self.remove_app(bundle_id)
        return {"requested": True, "verified": True}

    def tap_label(self, labels: list[str], timeout_seconds: float) -> dict:
        self.label_taps.append(list(labels))
        if self.fail_labels.intersection(labels):
            raise RuntimeError(f"no element labelled {labels}")
        return {"labels": labels, "tapped": True}

    def save_diagnostics(self, directory: Path, prefix: str) -> dict:
        return {"prefix": prefix}

    def type_text(self, text: str, config: dict | None = None) -> dict:
        if self.type_error:
            raise self.type_error
        self.typed_at_ms.append(int(time.time() * 1000))
        return super().type_text(text, config)

    def pull_app_documents(self, bundle_id: str, subpath: str, dest: Path, max_bytes: int) -> list[Path]:
        if self.pull_error:
            raise RuntimeError(self.pull_error)
        dest.mkdir(parents=True, exist_ok=True)
        started = int(time.time()) - 60
        shots = []
        if self.screenshots:
            shots = [f"broadcast-screenshot-{self.typed_at_ms[0]}.png", f"broadcast-screenshot-{started * 1000}.png"]
            for name in shots:
                (dest / name).write_bytes(b"png")
        recording = f"broadcast-{started}.mp4" if self.recording else None
        if recording:
            (dest / recording).write_bytes(MP4)
        if self.write_manifest:
            (dest / "done.json").write_text(json.dumps({
                "recording": recording,
                "recordingBytes": len(MP4) if recording else 0,
                "recordingStartedAt": started if recording else None,
                "screenshots": shots,
                "exportedAt": int(time.time()),
            }))
        return sorted(path for path in dest.iterdir() if path.is_file())


@pytest.fixture
def screen_capture_config(global_config, tmp_path):
    global_config.screen_capture = {
        "recorder_app": {
            "bundle_id": RECORDER,
            "ipa": str(tmp_path / "Recorder.ipa"),
            "install": True,
            "uninstall_after_test": True,
            "require_clean_state": True,
            "sign_in": {
                "username_accessibility_id": "recorder-username",
                "password_accessibility_id": "recorder-password",
                "username": "tester",
                "password": "tester",
            },
            "start_button_accessibility_id": "start-phone-recording",
            "export_button_accessibility_id": "export-evidence",
            "start_broadcast_labels": ["Start Broadcast"],
            "stop_broadcast_labels": ["Stop Broadcast"],
            "countdown_seconds": 0,
            "export_timeout_seconds": 0,
        },
        "capture": {"capture_window_seconds": 0.01, "type_secure_canary": True},
    }
    global_config.apps[0].risks = {"ios-feature-03-risk-01": {"enabled": True}}
    return global_config


@pytest.fixture
def readable(monkeypatch):
    monkeypatch.setattr(screen_capture_ocr, "is_obscured", lambda image: False)


def _run(config, tmp_path, device, recogniser=None):
    writer = ReportWriter(tmp_path / "reports", RUN)
    result = Feature03Risk01(recogniser).run(config.apps[0], config, device, writer)
    report_dir = writer.test_report_dir(config.apps[0].id, "ios-feature-03-risk-01", "screen_capture")
    return result, report_dir


def test_a_readable_canary_is_reported_as_risk_exists(screen_capture_config, tmp_path, readable):
    device = RecorderDevice()

    result, report_dir = _run(screen_capture_config, tmp_path, device, lambda image: ["Username", PLAIN])

    assert (result.final_status, result.verdict) == ("RISK_EXISTS", "At Risk")
    assert [entry["text"] for entry in device.typed_text] == [PLAIN, SECURE]
    assert device.text_entries[:2] == [
        {"accessibility_id": "recorder-username", "text": "tester", "clear_first": True},
        {"accessibility_id": "recorder-password", "text": "tester", "clear_first": True},
    ]
    assert (report_dir / "recording.mp4").read_bytes() == MP4
    assert result.launch_result["recording"]["valid"] is True
    assert result.launch_result["ocr"] == {
        "frames_scanned": 1,
        "obscured_frames": [],
        "frames_source": "screenshots",
        "secure_field_exposed": False,
        "match_count": 1,
    }
    assert (report_dir / "reference_screen.png").exists()
    assert len(list((report_dir / "frames").glob("*.png"))) == 1
    normalized = normalize_ios_result(result)
    assert normalized.severity == "high"
    assert normalized.summary == "Canary text readable in 1 of 1 recorded frame(s); password field masked"
    kinds = {(item.kind, Path(item.path).name) for item in normalized.evidence}
    assert ("screen_recording", "recording.mp4") in kinds
    assert ("report", "ocr_matches.json") in kinds
    assert any(kind == "screenshot" and "frames" in path for kind, path in ((i.kind, i.path) for i in normalized.evidence))
    assert json.loads((report_dir / "report.json").read_text())["final_status"] == "RISK_EXISTS"


def test_a_readable_password_canary_marks_the_secure_field_exposed(screen_capture_config, tmp_path, readable):
    result, report_dir = _run(screen_capture_config, tmp_path, RecorderDevice(), lambda image: [PLAIN, SECURE])

    assert result.final_status == "RISK_EXISTS"
    assert result.launch_result["ocr"]["secure_field_exposed"] is True
    assert json.loads((report_dir / "ocr_matches.json").read_text())["secure_field_exposed"] is True
    assert normalize_ios_result(result).summary.endswith("password field NOT masked")


def test_blank_frames_are_reported_as_obscured(screen_capture_config, tmp_path, monkeypatch):
    monkeypatch.setattr(screen_capture_ocr, "is_obscured", lambda image: True)

    result, _ = _run(screen_capture_config, tmp_path, RecorderDevice(), lambda image: pytest.fail("blank frames are not read"))

    assert (result.final_status, result.verdict) == ("SCREEN_CAPTURE_OBSCURED", "Reduced Risk")
    assert normalize_ios_result(result).severity == "low"


def test_readable_frames_without_the_canary_are_inconclusive(screen_capture_config, tmp_path, readable):
    result, report_dir = _run(screen_capture_config, tmp_path, RecorderDevice(), lambda image: ["Username", "•••"])

    assert (result.final_status, result.verdict) == ("CANARY_NOT_OBSERVED", "Inconclusive")
    assert (report_dir / "recording.mp4").exists()


def test_missing_ocr_still_attaches_the_recording(screen_capture_config, tmp_path, monkeypatch):
    monkeypatch.setattr(screen_capture_ocr, "ocr_available", lambda: False)

    result, report_dir = _run(screen_capture_config, tmp_path, RecorderDevice())

    assert (result.final_status, result.verdict) == ("OCR_UNAVAILABLE", "Inconclusive")
    assert (report_dir / "recording.mp4").exists()
    normalized = normalize_ios_result(result)
    assert normalized.summary == "OCR_UNAVAILABLE"
    assert ("screen_recording", "recording.mp4") in {(item.kind, Path(item.path).name) for item in normalized.evidence}


def test_video_frames_are_used_when_no_screenshot_falls_in_the_window(screen_capture_config, tmp_path, readable, monkeypatch):
    calls: list[tuple] = []

    def extract_frames(recording, recording_started_ms, start_ms, end_ms, every_seconds, dest):
        calls.append((recording.name, recording_started_ms, every_seconds, dest.name))
        dest.mkdir(parents=True, exist_ok=True)
        frame = dest / "video-frame-00001000.png"
        frame.write_bytes(b"png")
        return [frame]

    monkeypatch.setattr(screen_capture_ocr, "extract_frames", extract_frames)

    result, _ = _run(screen_capture_config, tmp_path, RecorderDevice(screenshots=False), lambda image: [PLAIN])

    assert result.final_status == "RISK_EXISTS"
    assert result.launch_result["ocr"]["frames_source"] == "video"
    assert calls and calls[0][0] == "recording.mp4" and calls[0][2:] == (1.0, "video_frames")


def test_a_broadcast_sheet_that_never_appears_is_broadcast_not_started(screen_capture_config, tmp_path):
    device = RecorderDevice()
    device.fail_labels = {"Start Broadcast"}

    result, _ = _run(screen_capture_config, tmp_path, device)

    assert (result.final_status, result.verdict) == ("BROADCAST_NOT_STARTED", "Inconclusive")
    assert result.launch_result["broadcast_error"]["stage"] == "start_broadcast"
    assert device.typed_text == []
    assert ["Stop Broadcast"] not in device.label_taps


def test_an_export_without_done_json_is_a_retrieval_failure(screen_capture_config, tmp_path):
    result, _ = _run(screen_capture_config, tmp_path, RecorderDevice(write_manifest=False))

    assert (result.final_status, result.verdict) == ("RECORDING_RETRIEVAL_FAILED", "Inconclusive")
    assert "never wrote done.json" in result.errors[0]


def test_a_pull_above_the_size_limit_is_a_retrieval_failure(screen_capture_config, tmp_path):
    device = RecorderDevice()
    device.pull_error = "Pulled evidence is 300 bytes, above max_pull_bytes=100"

    result, _ = _run(screen_capture_config, tmp_path, device)

    assert result.final_status == "RECORDING_RETRIEVAL_FAILED"
    assert "max_pull_bytes" in result.errors[0]


def test_a_failure_during_capture_stops_the_broadcast_and_removes_both_apps(screen_capture_config, tmp_path):
    device = RecorderDevice()
    device.type_error = RuntimeError("keyboard went away")

    result, _ = _run(screen_capture_config, tmp_path, device)

    assert result.final_status == "FAILED"
    assert device.label_taps == [["Start Broadcast"], ["Stop Broadcast"]]
    assert set(device.removed) == {RECORDER, "com.example.app"}
    assert result.cleanup_result.status == "CLEANED"
    assert result.cleanup_result.metadata["installed_recorder_by_risk"] is True


def test_a_leftover_recorder_that_cannot_be_removed_is_a_dirty_starting_state(screen_capture_config, tmp_path):
    device = RecorderDevice()
    device.installed.add(RECORDER)
    device.stuck.add(RECORDER)

    result, report_dir = _run(screen_capture_config, tmp_path, device)

    assert (result.final_status, result.verdict) == ("DIRTY_STARTING_STATE", "Inconclusive")
    assert result.launch_result["starting_state"]["recorder_preinstalled"] is True
    assert device.label_taps == []
    assert (report_dir / "report.json").exists()
