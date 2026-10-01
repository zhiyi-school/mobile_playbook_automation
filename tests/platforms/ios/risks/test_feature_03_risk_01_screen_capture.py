from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from mobile_playbook.platforms.ios.screen_capture import ocr as screen_capture_ocr
from mobile_playbook.platforms.ios.device_client import TextFieldUnavailableError
from mobile_playbook.platforms.ios.models import FINAL_STATUSES, InstallResult
from mobile_playbook.platforms.ios.results import normalize_ios_result
from mobile_playbook.platforms.ios.risks.feature_03_risk_01_screen_capture import Feature03Risk01
from mobile_playbook.reporting.report_writer import ReportWriter
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
        "capture": {"capture_window_seconds": 0.01, "type_secure_canary": True, "controls": {"enabled": False}},
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
        "matched_frame_count": 1,
        "obscured_frames": [],
        "failed_frames": [],
        "frames_source": "screenshots",
        "secure_field_exposed": False,
        "match_count": 1,
        "provider": "tesseract",
        "provider_settings": {
            "executable": "tesseract", "language": "eng", "page_segmentation_mode": 11,
            "timeout_seconds": 30.0, "tessdata_dir": None, "engine_mode": 1, "dictionary_correction": False,
        },
        "recogniser_injected": True,
    }
    assert (report_dir / "reference_screen.png").exists()
    assert len(list((report_dir / "frames").glob("*.png"))) == 1
    normalized = normalize_ios_result(result)
    assert normalized.severity == "high"
    assert result.launch_result["probe"]["typed"]["secure"]["status"] == "TYPED"
    assert normalized.summary == (
        "Canary text readable in 1 of 1 recorded frame(s); "
        "password canary typed but not observed; masking unverified"
    )
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
    assert result.launch_result["ocr"]["match_count"] == 2
    assert result.launch_result["ocr"]["matched_frame_count"] == 1
    scan = json.loads((report_dir / "ocr_matches.json").read_text())
    assert len(scan["matches"]) == 2 and scan["matched_frame_count"] == 1
    report = json.loads((report_dir / "report.json").read_text())
    assert report["launch_result"]["ocr"]["matched_frame_count"] == 1
    assert report["launch_result"]["probe"]["typed"]["secure"]["status"] == "TYPED"
    assert normalize_ios_result(result).summary == (
        "Canary text readable in 1 of 1 recorded frame(s); password canary readable in captured frames"
    )
    assert len(list((report_dir / "frames").glob("*.png"))) == 1


def test_blank_frames_are_inconclusive_and_keep_recording_evidence(screen_capture_config, tmp_path, monkeypatch):
    monkeypatch.setattr(screen_capture_ocr, "is_obscured", lambda image: True)

    result, report_dir = _run(screen_capture_config, tmp_path, RecorderDevice(), lambda image: pytest.fail("blank frames are not read"))

    assert (result.final_status, result.verdict) == ("CANARY_NOT_OBSERVED", "Inconclusive")
    assert result.errors == [
        "All captured frames were blank or uniform; "
        "app protection could not be distinguished from capture failure"
    ]
    normalized = normalize_ios_result(result)
    assert normalized.severity == "info"
    assert ("screen_recording", "recording.mp4") in {(item.kind, Path(item.path).name) for item in normalized.evidence}
    assert (report_dir / "recording.mp4").read_bytes() == MP4
    report = json.loads((report_dir / "report.json").read_text())
    assert (report["final_status"], report["verdict"]) == ("CANARY_NOT_OBSERVED", "Inconclusive")
    assert report["errors"] == result.errors
    scan = json.loads((report_dir / "ocr_matches.json").read_text())
    assert scan["obscured_frames"] and scan["matches"] == []


@pytest.mark.parametrize(
    ("text", "expected_status", "expected_verdict"),
    [([PLAIN], "RISK_EXISTS", "At Risk"), (["Username"], "CANARY_NOT_OBSERVED", "Inconclusive")],
)
def test_mixed_blank_and_readable_frames_depend_on_canary_matches(global_config, tmp_path, monkeypatch, text, expected_status, expected_verdict):
    blank = tmp_path / "broadcast-screenshot-1000.png"
    readable = tmp_path / "broadcast-screenshot-2000.png"
    for frame in (blank, readable):
        frame.write_bytes(b"png")
    monkeypatch.setattr(screen_capture_ocr, "is_obscured", lambda image: image == blank)

    def recognise(image):
        assert image == readable
        return text

    risk = Feature03Risk01(recognise)
    result = risk._base_result(RUN, global_config.apps[0])
    result.launch_result = {}
    result.final_status, result.verdict = risk._verdict(
        {"frames": [blank, readable], "manifest": {}}, None, 1000, 2000,
        {"plain": PLAIN}, {}, tmp_path, result,
    )

    assert (result.final_status, result.verdict) == (expected_status, expected_verdict)
    assert result.launch_result["ocr"]["obscured_frames"] == [str(blank)]
    assert result.launch_result["ocr"]["frames_scanned"] == 2


def test_readable_frames_without_the_canary_are_inconclusive(screen_capture_config, tmp_path, readable):
    result, report_dir = _run(screen_capture_config, tmp_path, RecorderDevice(), lambda image: ["Username", "•••"])

    assert (result.final_status, result.verdict) == ("CANARY_NOT_OBSERVED", "Inconclusive")
    assert (report_dir / "recording.mp4").exists()


def test_missing_ocr_still_attaches_the_recording(screen_capture_config, tmp_path, monkeypatch):
    monkeypatch.setattr(screen_capture_ocr, "ocr_unavailable_reason", lambda capture, *, provider: "Tesseract language data unavailable: eng")

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
        return {"frames": [frame], "failures": []}

    monkeypatch.setattr(screen_capture_ocr, "video_unavailable_reason", lambda: None)
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


@pytest.mark.parametrize("navigate, stop_fails", [(False, False), (True, False), (True, True)])
def test_an_unreachable_text_field_is_inconclusive_and_preserves_cleanup(screen_capture_config, tmp_path, monkeypatch, navigate, stop_fails):
    device = RecorderDevice()
    focus_attempts = []
    screen_capture_config.screen_capture["capture"]["auto_navigation"] = {
        "enabled": navigate, "max_steps": 2, "settle_seconds": 0,
    }
    if stop_fails:
        device.fail_labels = {"Stop Broadcast"}

    def no_field(selector=None):
        focus_attempts.append(selector)
        raise TextFieldUnavailableError("No visible enabled text field was found. Candidates: []")

    monkeypatch.setattr(device, "tap_text_field", no_field)
    result, report_dir = _run(screen_capture_config, tmp_path, device,
                              lambda image: pytest.fail("OCR must not run without a target canary"))

    assert "TEXT_FIELD_UNREACHABLE" in FINAL_STATUSES
    assert (result.final_status, result.verdict) == ("TEXT_FIELD_UNREACHABLE", "Inconclusive")
    assert len(focus_attempts) == (3 if navigate else 1)
    assert len(device.button_taps) == (2 if navigate else 0)
    assert device.typed_text == [] and "ocr" not in result.launch_result
    assert result.launch_result["probe"] == {"status": "TEXT_FIELD_UNREACHABLE", "error": result.errors[0]}
    assert device.label_taps == [["Start Broadcast"], ["Stop Broadcast"]]
    assert set(device.removed) == {RECORDER, "com.example.app"}
    assert result.cleanup_result.status == "CLEANED"
    assert len(result.errors) == (2 if stop_fails else 1)
    if stop_fails:
        assert result.errors[1].startswith("Could not stop the broadcast:")
    assert (report_dir / "target_screen.png").exists()
    assert (report_dir / "target_page_source.xml").exists()
    report = json.loads((report_dir / "report.json").read_text())
    assert (report["final_status"], report["verdict"]) == ("TEXT_FIELD_UNREACHABLE", "Inconclusive")
    assert report["errors"] == result.errors
    normalized = normalize_ios_result(result)
    assert normalized.severity == "info"
    assert normalized.summary == (
        "The configured probe could not reach an editable text field; screen capture protection was not assessed"
    )


def test_an_unexpected_focus_error_remains_failed(screen_capture_config, tmp_path, monkeypatch):
    device = RecorderDevice()

    def fail_focus(selector=None):
        raise RuntimeError("text field query lost its Appium session")

    monkeypatch.setattr(device, "tap_text_field", fail_focus)
    result, _ = _run(screen_capture_config, tmp_path, device)

    assert (result.final_status, result.verdict) == ("FAILED", "Inconclusive")
    assert result.errors == ["text field query lost its Appium session"]
    assert normalize_ios_result(result).severity == "medium"


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


@pytest.mark.parametrize("stage", ["recognition", "obscured_check"])
def test_ocr_failures_are_inconclusive_and_keep_evidence_and_cleanup(screen_capture_config, tmp_path, readable, monkeypatch, stage):
    device = RecorderDevice()

    def fail(image):
        raise screen_capture_ocr.OCRProcessingError("native processing refused the frame")

    if stage == "obscured_check":
        monkeypatch.setattr(screen_capture_ocr, "is_obscured", fail)

    result, report_dir = _run(screen_capture_config, tmp_path, device, fail)

    assert "OCR_FAILED" in FINAL_STATUSES
    assert (result.final_status, result.verdict) == ("OCR_FAILED", "Inconclusive")
    failures = result.launch_result["ocr"]["failed_frames"]
    assert len(failures) == 1
    assert failures[0]["error"] == "native processing refused the frame"
    assert result.errors == [
        f"OCR processing failed for {Path(failures[0]['frame']).name}: native processing refused the frame"
    ]
    scan = json.loads((report_dir / "ocr_matches.json").read_text())
    assert scan["failed_frames"] == failures and scan["matches"] == []
    report = json.loads((report_dir / "report.json").read_text())
    assert (report["final_status"], report["verdict"]) == ("OCR_FAILED", "Inconclusive")
    assert report["launch_result"]["ocr"]["failed_frames"] == failures
    assert report["errors"] == result.errors
    normalized = normalize_ios_result(result)
    assert normalized.severity == "info"
    assert normalized.summary == "OCR processing failed for 1 captured frame(s); review the available capture evidence"
    evidence = {(item.kind, Path(item.path).name) for item in normalized.evidence}
    assert ("screen_recording", "recording.mp4") in evidence
    assert ("report", "ocr_matches.json") in evidence
    assert (report_dir / "recording.mp4").read_bytes() == MP4
    assert device.label_taps == [["Start Broadcast"], ["Stop Broadcast"]]
    assert set(device.removed) == {RECORDER, "com.example.app"}
    assert result.cleanup_result.status == "CLEANED"


@pytest.mark.parametrize(
    ("text", "expected_status", "expected_verdict", "secure_typed"),
    [([PLAIN], "RISK_EXISTS", "At Risk", False), ([], "OCR_FAILED", "Inconclusive", False),
     ([PLAIN], "RISK_EXISTS", "At Risk", True)],
)
def test_partial_ocr_failure_preserves_canary_verdict_and_errors(global_config, tmp_path, monkeypatch, text, expected_status, expected_verdict, secure_typed):
    failed = tmp_path / "broadcast-screenshot-1000.png"
    readable = tmp_path / "broadcast-screenshot-2000.png"
    for frame in (failed, readable):
        frame.write_bytes(b"png")
    monkeypatch.setattr(screen_capture_ocr, "is_obscured", lambda image: False)

    def recognise(image):
        if image == failed:
            raise screen_capture_ocr.OCRProcessingError("Tesseract recognition failed")
        return text

    risk = Feature03Risk01(recognise)
    result = risk._base_result(RUN, global_config.apps[0])
    result.launch_result = {"probe": {"typed": {"secure": {"status": "TYPED"}}}} if secure_typed else {}
    canaries = {"plain": PLAIN, "secure": SECURE} if secure_typed else {"plain": PLAIN}
    result.final_status, result.verdict = risk._verdict(
        {"frames": [failed, readable], "manifest": {}}, None, 1000, 2000,
        canaries, {}, tmp_path, result,
    )

    assert (result.final_status, result.verdict) == (expected_status, expected_verdict)
    assert result.launch_result["ocr"]["failed_frames"] == [
        {"frame": str(failed), "error": "Tesseract recognition failed"}
    ]
    assert result.errors == [f"OCR processing failed for {failed.name}: Tesseract recognition failed"]
    scan = json.loads((tmp_path / "ocr_matches.json").read_text())
    assert [match["frame"] for match in scan["matches"]] == ([str(readable)] if text else [])

    if secure_typed:
        assert normalize_ios_result(result).summary == (
            "Canary text readable in 1 of 2 recorded frame(s); "
            "password canary typed but not observed; masking unverified; "
            "OCR processing failed for 1 other frame(s)"
        )


def test_successful_ocr_with_no_text_is_canary_not_observed(screen_capture_config, tmp_path, readable):
    result, _ = _run(screen_capture_config, tmp_path, RecorderDevice(), lambda image: [])

    assert (result.final_status, result.verdict) == ("CANARY_NOT_OBSERVED", "Inconclusive")
    assert result.launch_result["ocr"]["failed_frames"] == []



def test_a_disabled_password_probe_is_reported_as_not_tested(screen_capture_config, tmp_path, readable):
    screen_capture_config.screen_capture["capture"]["type_secure_canary"] = False
    device = RecorderDevice()

    result, report_dir = _run(screen_capture_config, tmp_path, device, lambda image: [PLAIN])

    assert [entry["text"] for entry in device.typed_text] == [PLAIN]
    assert (result.final_status, result.verdict) == ("RISK_EXISTS", "At Risk")
    report = json.loads((report_dir / "report.json").read_text())
    assert report["launch_result"]["probe"]["typed"]["secure"] == {"status": "DISABLED"}
    assert normalize_ios_result(result).summary.endswith("password probe not tested (disabled)")


@pytest.mark.parametrize("error", ["no secure field found", "secure field is not interactable"])
def test_a_password_focus_failure_is_reported_as_not_tested(screen_capture_config, tmp_path, readable, monkeypatch, error):
    device = RecorderDevice()
    original_tap = device.tap_text_field

    def tap(selector=None):
        if selector and "SecureTextField" in selector.get("ios_class_chain", ""):
            raise RuntimeError(error)
        return original_tap(selector)

    monkeypatch.setattr(device, "tap_text_field", tap)

    result, report_dir = _run(screen_capture_config, tmp_path, device, lambda image: [PLAIN])

    assert [entry["text"] for entry in device.typed_text] == [PLAIN]
    assert (result.final_status, result.verdict) == ("RISK_EXISTS", "At Risk")
    report = json.loads((report_dir / "report.json").read_text())
    assert report["launch_result"]["probe"]["typed"]["secure"] == {"status": "FOCUS_FAILED", "error": error}
    assert normalize_ios_result(result).summary.endswith(
        "password probe not tested (secure field could not be focused)"
    )


@pytest.mark.parametrize("outcome, status", [
    ("matches", "RISK_EXISTS"), ("no_text", "CANARY_NOT_OBSERVED"),
    ("exit_failure", "OCR_FAILED"), ("timeout", "OCR_FAILED"),
])
def test_the_capture_workflow_uses_the_configured_tesseract_provider(screen_capture_config, tmp_path, readable, monkeypatch, outcome, status):
    import subprocess
    from mobile_playbook.platforms.ios.screen_capture import providers

    screen_capture_config.screen_capture["capture"]["tesseract"] = {
        "executable": "/tools with spaces/tesseract", "language": "eng+deu",
        "page_segmentation_mode": 6, "timeout_seconds": 8, "tessdata_dir": "/language data",
    }
    monkeypatch.setattr(screen_capture_ocr, "ocr_unavailable_reason", lambda capture, *, provider: None)
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return subprocess.CompletedProcess(
            command, 1 if outcome == "exit_failure" else 0,
            stdout=f"{PLAIN}\n{SECURE}\n" if outcome == "matches" else "",
            stderr="cannot decode the frame" if outcome == "exit_failure" else "",
        )

    monkeypatch.setattr(providers.subprocess, "run", run)
    device = RecorderDevice()

    result, report_dir = _run(screen_capture_config, tmp_path, device)

    assert result.final_status == status
    assert result.verdict == ("At Risk" if outcome == "matches" else "Inconclusive")
    scan = json.loads((report_dir / "ocr_matches.json").read_text())
    assert scan["provider"] == "tesseract"
    assert scan["provider_settings"]["language"] == "eng+deu"
    assert scan["provider_settings"]["page_segmentation_mode"] == 6
    assert "recogniser_injected" not in scan
    assert calls[0][0][0] == "/tools with spaces/tesseract" and calls[0][1]["timeout"] == 8
    assert (report_dir / "recording.mp4").read_bytes() == MP4
    assert result.cleanup_result.status == "CLEANED"
    if outcome in {"exit_failure", "timeout"}:
        assert len(scan["failed_frames"]) == 1 and result.errors
    elif outcome == "matches":
        assert scan["matched_frame_count"] == 1 and len(scan["matches"]) == 2



def test_capture_defaults_stay_in_temporary_storage_without_reading_the_developer_env(screen_capture_config, tmp_path, readable, monkeypatch):
    from mobile_playbook.api import settings
    from mobile_playbook.common import storage_paths
    from mobile_playbook.platforms.ios.config import parse_config

    original_read = Path.read_text
    developer_env = storage_paths.REPOSITORY_ROOT / ".env"

    def read(path, *args, **kwargs):
        assert path != developer_env, "test attempted to read the developer environment file"
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.delenv("WORK_DIR", raising=False)
    original_app = screen_capture_config.apps[0]
    config = parse_config({
        "device": {"udid": "test-device", "team_id": "TEAM", "appium_server_url": "http://127.0.0.1:4723"},
        "runner": {"launch_wait_seconds": 0},
        "screen_capture": screen_capture_config.screen_capture,
        "apps": [{
            "id": original_app.id, "name": original_app.name, "bundle_id": original_app.bundle_id,
            "artifact": original_app.artifact,
            "risks": {"ios-feature-03-risk-01": {"enabled": True}},
        }],
    })
    assert settings.ENV_FILE == tmp_path / ".env"
    assert config.runner.work_dir == tmp_path / "artifacts" / "work" / "ios"
    writer = ReportWriter(storage_paths.reports_root(), RUN)
    device = RecorderDevice()

    result = Feature03Risk01(lambda image: [PLAIN]).run(config.apps[0], config, device, writer)

    assert (result.final_status, result.verdict) == ("RISK_EXISTS", "At Risk")
    assert result.artifact_result.ipa_path.is_relative_to(tmp_path)
    assert Path(result.launch_result["recording"]["path"]).is_relative_to(tmp_path)
    assert writer.run_dir.is_relative_to(tmp_path)
    assert list(config.runner.work_dir.rglob("*.ipa"))
    assert list(writer.run_dir.rglob("report.json"))
    assert result.cleanup_result.status == "CLEANED"


# Check that missing video bindings leave screenshot analysis usable.
@pytest.mark.parametrize("missing", ["AVFoundation", "CoreMedia"])
@pytest.mark.parametrize("screenshots", [False, True])
def test_missing_video_bindings_only_block_a_required_fallback(screen_capture_config, tmp_path, readable, monkeypatch, missing, screenshots):
    for name in ("AVFoundation", "CoreMedia", "Quartz", "Foundation"):
        monkeypatch.setitem(sys.modules, name, None if name == missing else SimpleNamespace())
    device = RecorderDevice(screenshots=screenshots)

    result, report_dir = _run(screen_capture_config, tmp_path, device, lambda image: [PLAIN])

    assert (result.final_status, result.verdict) == (("RISK_EXISTS", "At Risk") if screenshots else ("OCR_UNAVAILABLE", "Inconclusive"))
    assert (report_dir / "recording.mp4").read_bytes() == MP4
    assert result.cleanup_result.status == "CLEANED"
    if not screenshots:
        assert missing in result.launch_result["ocr"]["unavailable_reason"]
        assert result.launch_result["ocr"]["frames_source"] == "video"
        assert normalize_ios_result(result).severity == "info"


# Check video failures through the report-writing and device-cleanup boundaries.
@pytest.mark.parametrize("fault, text, status", [
    ("all_failed", [], "OCR_FAILED"), ("partial", [], "OCR_FAILED"),
    ("partial", [PLAIN], "RISK_EXISTS"), ("setup", [], "OCR_FAILED"),
    ("empty", [], "OCR_FAILED"), ("unexpected", [], "FAILED"),
])
def test_video_failures_keep_evidence_cleanup_and_positive_matches(screen_capture_config, tmp_path, readable, monkeypatch, fault, text, status):
    monkeypatch.setattr(screen_capture_ocr, "video_unavailable_reason", lambda: None)

    # Supply expected failures, partial successes and an unexpected decoder bug.
    def extract(recording, recording_started_ms, start_ms, end_ms, every_seconds, dest):
        if fault == "setup":
            raise screen_capture_ocr.OCRProcessingError("Could not create the video frame directory")
        if fault == "unexpected":
            raise RuntimeError("unexpected decoder bug")
        frames, failures = [], []
        if fault == "partial":
            dest.mkdir(parents=True)
            frame = dest / "video-frame-00002000.png"
            frame.write_bytes(b"png")
            frames.append(frame)
        if fault != "empty":
            failures.append({"recording": str(recording), "sample_ms": 1000, "stage": "video_decode", "error": "unreadable video sample"})
        return {"frames": frames, "failures": failures}

    monkeypatch.setattr(screen_capture_ocr, "extract_frames", extract)
    device = RecorderDevice(screenshots=False)

    result, report_dir = _run(screen_capture_config, tmp_path, device, lambda image: text)

    assert result.final_status == status
    assert result.verdict == ("At Risk" if status == "RISK_EXISTS" else "Inconclusive")
    normalized = normalize_ios_result(result)
    assert normalized.severity == {"RISK_EXISTS": "high", "OCR_FAILED": "info", "FAILED": "medium"}[status]
    assert (report_dir / "recording.mp4").read_bytes() == MP4
    assert ("screen_recording", "recording.mp4") in {(item.kind, Path(item.path).name) for item in normalized.evidence}
    assert device.label_taps == [["Start Broadcast"], ["Stop Broadcast"]]
    assert result.cleanup_result.status == "CLEANED"
    report = json.loads((report_dir / "report.json").read_text())
    assert report["final_status"] == status and report["errors"] == result.errors
    if fault == "unexpected":
        assert result.errors == ["unexpected decoder bug"]
        assert not (report_dir / "ocr_matches.json").exists()
    else:
        scan = json.loads((report_dir / "ocr_matches.json").read_text())
        assert scan["video_errors"] == result.launch_result["ocr"]["video_errors"]
        assert scan["video_errors"] and result.errors
        assert report["launch_result"]["ocr"]["video_errors"] == scan["video_errors"]
        assert len(scan["matches"]) == (1 if text else 0)
        if status == "OCR_FAILED":
            assert normalized.summary == "Video frame extraction failed or was incomplete; review the available capture evidence"
        else:
            assert normalized.summary.endswith("video processing was incomplete; review the processing errors")
            assert len(list((report_dir / "frames").glob("*.png"))) == 1


# Exercise a corrupt MP4 through native extraction, classification, evidence and cleanup.
@pytest.mark.native_capture
def test_a_real_corrupt_recording_is_ocr_failed_despite_an_mp4_like_header(screen_capture_config, tmp_path):
    unavailable = screen_capture_ocr.video_unavailable_reason()
    if unavailable:
        pytest.skip(unavailable)
    device = RecorderDevice(screenshots=False)

    result, report_dir = _run(screen_capture_config, tmp_path, device, lambda image: pytest.fail("corrupt video must not reach OCR"))

    assert (result.final_status, result.verdict) == ("OCR_FAILED", "Inconclusive")
    assert result.launch_result["recording"]["valid"] is True
    assert normalize_ios_result(result).severity == "info"
    scan = json.loads((report_dir / "ocr_matches.json").read_text())
    assert scan["frames_scanned"] == 0 and scan["matches"] == []
    assert scan["video_errors"] == result.launch_result["ocr"]["video_errors"]
    error, = scan["video_errors"]
    assert error["stage"] == "video_decode" and "Could not decode video sample" in error["error"]
    assert error["recording"] == str(report_dir / "recording.mp4")
    assert (report_dir / "recording.mp4").read_bytes() == MP4
    assert result.cleanup_result.status == "CLEANED"
    assert json.loads((report_dir / "report.json").read_text())["final_status"] == "OCR_FAILED"


# Model recorder and target capture independently while advancing a synthetic wall clock.
class ControlledRecorderDevice(RecorderDevice):
    # Store independent frame content and the current app for each control phase.
    def __init__(self, clock, scenario):
        super().__init__()
        self.clock, self.scenario, self.active = clock, scenario, None
        self.marker, self.control_shots = None, []

    # Track the app whose screen a subsequent screenshot should represent.
    def launch_app(self, bundle_id):
        self.active = bundle_id
        return super().launch_app(bundle_id)

    # Preserve recorder login state while returning it to the foreground.
    def activate_app(self, bundle_id):
        self.active = bundle_id
        return {"activated": bundle_id}

    # Capture the dummy username marker provided by the workflow.
    def set_text_by_accessibility_id(self, accessibility_id, text, clear_first=True):
        if accessibility_id == "recorder-username":
            self.marker = text
        return super().set_text_by_accessibility_id(accessibility_id, text, clear_first)

    # Timestamp target input using the same clock as the capture windows.
    def type_text(self, text, config=None):
        self.typed_at_ms.append(int(self.clock.time() * 1000))
        return MockDevice.type_text(self, text, config)

    # Report the target as backgrounded once the capture window's clock moves past the typed canary.
    def query_app_state(self, bundle_id):
        left = self.scenario == "wrong_app" and bundle_id == "com.example.app" and self.typed_at_ms
        return 3 if left and int(self.clock.time() * 1000) > self.typed_at_ms[-1] else 4

    # Supply a synthetic whole-second device clock without contacting Appium.
    def device_time_ms(self):
        return int(self.clock.time()) * 1000

    # Save expected Appium content independently of the corresponding ReplayKit frame.
    def screenshot(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        target = path.name in {"reference_screen.png", "control_target_start_reference.png"}
        if target and self.scenario == "positive_missing_reference":
            raise RuntimeError("Appium reference unavailable")
        content = PLAIN if target else self.marker
        if target and self.scenario == "visibility_missing":
            content = "Username"
        path.write_text(content)
        if not target:
            phase = "before" if "before" in path.name else "after"
            broadcast = "old recorder text" if phase == "after" and self.scenario == "after_mismatch" else content
            if phase == "before" and self.scenario in {"ocr_failed", "positive_with_failure"}:
                broadcast = "FAIL_OCR"
            if not (phase == "before" and self.scenario == "before_missing"):
                self.control_shots.append((int(self.clock.time() * 1000) - 1, broadcast))

    # Export recorder controls alongside independent target frames in one manifest.
    def pull_app_documents(self, bundle_id, subpath, dest, max_bytes):
        super().pull_app_documents(bundle_id, subpath, dest, max_bytes)
        target_text = PLAIN if self.scenario in {"positive", "positive_with_failure", "positive_missing_reference"} else "BLANK" if self.scenario == "blank" else "Username"
        shots = self.control_shots + [(self.typed_at_ms[0] + 1, target_text)]
        names = []
        for timestamp, content in shots:
            name = f"broadcast-screenshot-{timestamp}.png"
            (dest / name).write_text(content)
            names.append(name)
        manifest = json.loads((dest / "done.json").read_text())
        manifest["screenshots"] = names
        (dest / "done.json").write_text(json.dumps(manifest))
        return sorted(path for path in dest.iterdir() if path.is_file())


# Exercise successful controls, missing content, processing failures and confirmed risk through reports.
@pytest.mark.parametrize("scenario, status, controls_status", [
    ("positive", "RISK_EXISTS", "PASSED"), ("blank", "CANARY_NOT_OBSERVED", "PASSED"),
    ("no_text", "CANARY_NOT_OBSERVED", "PASSED"), ("visibility_missing", "CANARY_NOT_OBSERVED", "UNVERIFIED"),
    ("wrong_app", "CANARY_NOT_OBSERVED", "UNVERIFIED"), ("before_missing", "CANARY_NOT_OBSERVED", "UNVERIFIED"),
    ("after_mismatch", "CANARY_NOT_OBSERVED", "UNVERIFIED"), ("ocr_failed", "OCR_FAILED", "UNVERIFIED"),
    ("positive_with_failure", "RISK_EXISTS", "UNVERIFIED"),
    ("positive_missing_reference", "RISK_EXISTS", "UNVERIFIED"),
])
def test_capture_controls_explain_results_without_becoming_risk_matches(screen_capture_config, tmp_path, monkeypatch, scenario, status, controls_status):
    from mobile_playbook.platforms.ios.risks import feature_03_risk_01_screen_capture as risk_module
    from mobile_playbook.platforms.ios.screen_capture import controls

    clock = SimpleNamespace(now=time.time())
    clock.time = lambda: clock.now
    clock.sleep = lambda seconds: setattr(clock, "now", clock.now + seconds)
    monkeypatch.setattr(controls, "time", clock)
    monkeypatch.setattr(risk_module, "time", clock)
    monkeypatch.setattr(screen_capture_ocr, "is_obscured", lambda path: path.read_text() == "BLANK")
    capture = screen_capture_config.screen_capture["capture"]
    capture.update({"controls": {"enabled": True, "recorder_hold_seconds": 6}, "capture_window_seconds": 12, "video_frame_fallback": False})
    device = ControlledRecorderDevice(clock, scenario)

    # Read each channel independently and simulate a failure confined to one recorder frame.
    def recognise(path):
        text = path.read_text()
        if text == "FAIL_OCR":
            raise screen_capture_ocr.OCRProcessingError("synthetic control OCR failed")
        return [text]

    result, report_dir = _run(screen_capture_config, tmp_path, device, recognise)

    assert result.final_status == status
    assert result.verdict == ("At Risk" if status == "RISK_EXISTS" else "Inconclusive")
    control_state = result.launch_result["capture_controls"]
    assert control_state["status"] == controls_status
    assert control_state["target_reference"]["status"] == ("CANARY_NOT_OBSERVED" if scenario == "visibility_missing" else "NOT_FOREGROUND" if scenario == "wrong_app" else "CAPTURE_FAILED" if scenario == "positive_missing_reference" else "PASSED")
    assert control_state["before"]["end_ms"] <= control_state["after"]["start_ms"]
    assert screen_capture_config.screen_capture["recorder_app"]["sign_in"]["username"] == "tester"
    assert device.marker == control_state["recorder_text"] and device.marker != "tester"
    assert json.loads((report_dir / "capture_controls.json").read_text()) == control_state
    assert json.loads((report_dir / "report.json").read_text())["launch_result"]["capture_controls"] == control_state
    scan = json.loads((report_dir / "ocr_matches.json").read_text())
    assert scan["frames_scanned"] == 1 and scan["matched_frame_count"] == (1 if status == "RISK_EXISTS" else 0)
    assert all(match["canary_kind"] != "recorder" for match in scan["matches"])
    normalized = normalize_ios_result(result)
    evidence_names = {Path(item.path).name for item in normalized.evidence}
    assert {"capture_controls.json", "control_before_reference.png", "control_after_reference.png"} <= evidence_names
    assert ("reference_screen.png" in evidence_names) is (scenario != "positive_missing_reference")
    assert result.cleanup_result.status == "CLEANED"
    if status == "CANARY_NOT_OBSERVED":
        assert normalized.summary == "Canary not observed in target capture; " + control_state["diagnostic"]
    if scenario == "before_missing":
        assert control_state["before"]["broadcast"]["status"] == "NO_FRAMES"
    if scenario == "after_mismatch":
        assert control_state["after"]["broadcast"]["status"] == "CANARY_NOT_OBSERVED"
    if scenario in {"ocr_failed", "positive_with_failure"}:
        assert control_state["processing_failed"] is True
        assert "FAIL_OCR" not in normalized.summary


class CoveredRecorderDevice(ControlledRecorderDevice):
    # Add first-sample timing and independent faults to the controlled workflow fixture.
    def __init__(self, clock, fault):
        super().__init__(clock, "positive" if fault == "positive_bad_header" else "blank")
        self.fault, self.origin_ms = fault, int(clock.time() * 1000)
        self.clock_reads = 0

    # Model a device clock discontinuity after target capture.
    def device_time_ms(self):
        self.clock_reads += 1
        return int(self.clock.time()) * 1000 + (5000 if self.fault == "clock_jump" and self.clock_reads == 2 else 0)

    # Make a transient background state observable between otherwise valid references.
    def query_app_state(self, bundle_id):
        if self.fault == "background" and bundle_id == "com.example.app" and self.origin_ms + 12000 <= int(self.clock.time() * 1000) <= self.origin_ms + 13000:
            return 3
        return 4

    # Fail only the initial target reference, independently of subsequent capture output.
    def screenshot(self, path):
        if self.fault == "start_reference_missing" and path.name == "control_target_start_reference.png":
            raise RuntimeError("Initial reference unavailable")
        super().screenshot(path)

    # Anchor the exported recording to its first sample and optionally invalidate its bytes.
    def pull_app_documents(self, bundle_id, subpath, dest, max_bytes):
        super().pull_app_documents(bundle_id, subpath, dest, max_bytes)
        manifest_path = dest / "done.json"
        manifest = json.loads(manifest_path.read_text())
        first = f"broadcast-screenshot-{self.origin_ms}.png"
        if self.fault != "missing_origin":
            (dest / first).write_text(self.marker)
            manifest["screenshots"].append(first)
        manifest["recordingStartedAt"] = self.origin_ms // 1000
        if self.fault in {"bad_header", "positive_bad_header"}:
            (dest / manifest["recording"]).write_bytes(b"invalid recording" * 200)
        if self.fault == "no_video":
            (dest / manifest["recording"]).unlink()
            manifest["recording"] = None
        manifest_path.write_text(json.dumps(manifest))
        return sorted(path for path in dest.iterdir() if path.is_file())


# Verify verdicts, cleanup, saved diagnostics and normalized summaries through the complete workflow.
@pytest.mark.parametrize("fault, status", [("none", "SCREEN_CAPTURE_OBSCURED_VERIFIED"), ("gap", "CANARY_NOT_OBSERVED"),
                                           ("content", "CANARY_NOT_OBSERVED"), ("bad_header", "CANARY_NOT_OBSERVED"),
                                           ("no_video", "CANARY_NOT_OBSERVED"), ("background", "CANARY_NOT_OBSERVED"),
                                           ("clock_jump", "CANARY_NOT_OBSERVED"), ("missing_origin", "CANARY_NOT_OBSERVED"),
                                           ("start_reference_missing", "CANARY_NOT_OBSERVED"), ("decode_error", "OCR_FAILED"),
                                           ("same_video_control", "CANARY_NOT_OBSERVED"), ("positive_bad_header", "RISK_EXISTS")])
def test_verified_capture_coverage_controls_the_reduced_risk_verdict(screen_capture_config, tmp_path, monkeypatch, fault, status):
    from mobile_playbook.platforms.ios.risks import feature_03_risk_01_screen_capture as risk_module
    from mobile_playbook.platforms.ios.screen_capture import controls, coverage

    clock = SimpleNamespace(now=200000.0)
    clock.time = lambda: clock.now
    clock.sleep = lambda seconds: setattr(clock, "now", clock.now + seconds)
    monkeypatch.setattr(controls, "time", clock)
    monkeypatch.setattr(risk_module, "time", clock)
    monkeypatch.setattr(screen_capture_ocr, "is_obscured", lambda path: path.read_text() == "BLANK")
    monkeypatch.setattr(screen_capture_ocr, "video_unavailable_reason", lambda: None)
    calls = []

    # Supply decoded frames at one-second intervals while varying native coverage failures.
    def decode(recording, target, windows, marker, recogniser, report_dir):
        calls.append(target)
        if fault == "decode_error":
            raise screen_capture_ocr.OCRProcessingError("The video stream is truncated")
        samples = [(second, False if fault == "content" and second == 12 else True) for second in range(25) if not (fault == "gap" and 12 <= second <= 14)]
        video_controls = {}
        for phase in ("before", "after"):
            path = report_dir / "control_frames" / f"video-{phase}.png"
            path.parent.mkdir(exist_ok=True)
            path.write_text(marker)
            video_controls[phase] = {"status": "CANARY_NOT_OBSERVED" if fault == "same_video_control" and phase == "after" else "PASSED", "evidence_frames": [str(path)]}
        return {"samples": samples, "video_controls": video_controls}

    monkeypatch.setattr(coverage, "_decode", decode)
    screen_capture_config.screen_capture["capture"].update(controls={"enabled": True}, capture_window_seconds=12, video_frame_fallback=False)
    result, report_dir = _run(screen_capture_config, tmp_path, CoveredRecorderDevice(clock, fault), lambda path: [path.read_text()])

    assert result.final_status == status and status in FINAL_STATUSES
    assert result.verdict == ("Reduced Risk" if status == "SCREEN_CAPTURE_OBSCURED_VERIFIED" else "At Risk" if status == "RISK_EXISTS" else "Inconclusive")
    control_state = result.launch_result["capture_controls"]
    assert json.loads((report_dir / "capture_controls.json").read_text()) == control_state
    assert json.loads((report_dir / "report.json").read_text())["launch_result"]["capture_controls"] == control_state
    assert result.cleanup_result.status == "CLEANED"
    normalized = normalize_ios_result(result)
    names = {Path(item.path).name for item in normalized.evidence}
    assert ("control_target_start_reference.png" in names) is (fault != "start_reference_missing")
    if status == "SCREEN_CAPTURE_OBSCURED_VERIFIED":
        assert control_state["coverage"]["status"] == "PASSED" and control_state["coverage"]["duration_seconds"] == 6
        assert normalized.severity == "low"
        assert "verified 6-second window" in normalized.summary
        assert "video-before.png" in names and "video-after.png" in names
        assert all("unverified" not in error.lower() for error in result.errors)
    elif fault == "decode_error":
        assert control_state["coverage"]["processing_failed"] is True
        assert normalized.summary == "Capture coverage processing failed; review the available capture evidence"
        assert normalized.severity == "info"
    elif fault == "positive_bad_header":
        assert result.launch_result["recording"]["valid"] is False
        assert calls == [] and "coverage" not in control_state
    else:
        assert control_state["coverage"]["status"] != "PASSED"
        assert normalized.severity == "info"


# Stop before typing any canary when the target app is never in the foreground after launch.
def test_a_target_outside_the_foreground_stops_before_any_input(screen_capture_config, tmp_path):
    class BackgroundTargetDevice(RecorderDevice):
        # Report the target as backgrounded, as when an update prompt hands off to the App Store.
        def query_app_state(self, bundle_id):
            return 3 if bundle_id == "com.example.app" else 4

    device = BackgroundTargetDevice()

    result, report_dir = _run(screen_capture_config, tmp_path, device, lambda image: [PLAIN])

    assert (result.final_status, result.verdict) == ("TARGET_NOT_FOREGROUND", "Inconclusive")
    assert device.typed_text == []
    assert device.permission_alert_calls[0]["foreground_bundle_id"] == "com.example.app"
    assert result.launch_result["probe"]["status"] == "TARGET_NOT_FOREGROUND"
    assert ["Stop Broadcast"] in device.label_taps
    assert result.cleanup_result.status == "CLEANED"
    normalized = normalize_ios_result(result)
    assert normalized.severity == "info"
    assert normalized.summary.startswith("The target app left the foreground")
    assert json.loads((report_dir / "report.json").read_text())["final_status"] == "TARGET_NOT_FOREGROUND"


# Stop navigating as soon as a navigation tap sends the target app to the background.
def test_navigation_stops_when_a_tap_leaves_the_target_app(screen_capture_config, tmp_path):
    screen_capture_config.screen_capture["capture"]["auto_navigation"] = {"enabled": True, "max_steps": 4, "settle_seconds": 0}

    class LeavingDevice(RecorderDevice):
        # Fail the first focus, then report the target backgrounded after one navigation tap.
        def __init__(self):
            super().__init__()
            self.navigation_taps = 0

        def tap_text_field(self, selector=None):
            raise RuntimeError("No visible enabled text field was found. Candidates: []")

        def tap_first_button_matching(self, label_contains=None, exclude_label_contains=None, allow_any=False):
            self.navigation_taps += 1
            return {"label": "Update Now", "matched_by": "label_contains"}

        def query_app_state(self, bundle_id):
            return 3 if bundle_id == "com.example.app" and self.navigation_taps else 4

    device = LeavingDevice()

    result, _ = _run(screen_capture_config, tmp_path, device, lambda image: [PLAIN])

    assert result.final_status == "TARGET_NOT_FOREGROUND"
    assert device.navigation_taps == 1
    assert device.typed_text == []
