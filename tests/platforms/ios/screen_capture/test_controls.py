from __future__ import annotations

from types import SimpleNamespace

import pytest

from mobile_playbook.platforms.ios.screen_capture import controls, ocr


# Check that control preparation does not mutate credentials or reuse a previous marker.
def test_recorder_control_uses_a_unique_dummy_username_without_mutating_configuration(tmp_path):
    recorder = {"sign_in": {"username_accessibility_id": "recorder-username", "username": "tester", "password": "dummy"}}
    first, state = controls.prepare(recorder, {}, tmp_path)
    second, other = controls.prepare(recorder, {}, tmp_path)
    assert first["sign_in"]["username"] == state["recorder_text"]
    assert state["recorder_text"] != other["recorder_text"] == second["sign_in"]["username"]
    assert recorder["sign_in"]["username"] == "tester"
    assert first["sign_in"]["password"] == "dummy"


# Avoid recorder control attempts when disabled, OCR is off, or no suitable dummy sign-in exists.
@pytest.mark.parametrize("capture, expected", [({"controls": {"enabled": False}}, False), ({"ocr_provider": "none"}, False), ({}, True)])
def test_control_availability_is_explicit(tmp_path, capture, expected):
    recorder, state = controls.prepare({}, capture, tmp_path)
    assert state["enabled"] is expected
    if expected:
        controls.record_recorder(None, recorder, state, "before", tmp_path, 6)
        assert state["before"]["status"] == "NOT_CONFIGURED"


# Check that only freshly saved screenshots with foreground confirmation qualify as references.
@pytest.mark.parametrize("outcome", ["fresh", "stale", "missing", "empty", "background", "switch_app", "device_error"])
def test_reference_controls_reject_stale_missing_and_wrong_app_images(tmp_path, outcome):
    path = tmp_path / "reference.png"
    if outcome == "stale":
        path.write_bytes(b"old screenshot")

    # Model screenshot saving independently of the foreground-state response.
    def screenshot(target):
        if outcome == "device_error":
            raise RuntimeError("Appium could not capture")
        if outcome not in {"stale", "missing"}:
            target.write_bytes(b"" if outcome == "empty" else b"png")

    states = iter([4, 3])
    device = SimpleNamespace(query_app_state=lambda bundle: next(states) if outcome == "switch_app" else 3 if outcome == "background" else 4, screenshot=screenshot)
    reference = controls.record_reference(device, "target", path)
    expected = "CAPTURED" if outcome == "fresh" else "NOT_FOREGROUND" if outcome in {"background", "switch_app"} else "CAPTURE_FAILED"
    assert reference["capture_status"] == expected
    assert reference["path"] == str(path)


# Preserve unexpected recogniser bugs instead of representing them as a failed control.
def test_reference_analysis_does_not_hide_unexpected_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(ocr, "is_obscured", lambda image: False)

    # Raise a programming error from the injected OCR boundary.
    def recognise(image):
        raise ValueError("unexpected recogniser bug")

    with pytest.raises(ValueError, match="unexpected recogniser bug"):
        controls.analyse_reference({"capture_status": "CAPTURED", "path": str(tmp_path / "reference.png")}, "SCR123456", recognise)


# Provide independent reference images and two disjoint recorder control windows.
@pytest.fixture
def control_state(tmp_path):
    marker = "CTL135790"
    reference = tmp_path / "target.png"
    reference.write_text("SCR123456")
    state = {"recorder_text": marker, "target_reference": {"capture_status": "CAPTURED", "path": str(reference)}}
    for phase, start in (("before", 1000), ("after", 4000)):
        ref = tmp_path / f"{phase}.png"
        ref.write_text(marker)
        state[phase] = {"start_ms": start, "end_ms": start + 1000,
                        "reference": {"capture_status": "CAPTURED", "path": str(ref)}}
    return state


# Check both recorder video windows, partial decoding failures and strict phase boundaries.
@pytest.mark.parametrize("failed", [False, True])
def test_recorder_controls_use_video_fallback_and_keep_sample_errors(tmp_path, monkeypatch, control_state, failed):
    monkeypatch.setattr(ocr, "is_obscured", lambda image: False)
    monkeypatch.setattr(ocr, "video_unavailable_reason", lambda: None)
    calls = []
    state = control_state
    marker = state["recorder_text"]

    # Return a matching sample while optionally reporting another failed decode.
    def extract(recording, started, start, end, interval, dest):
        calls.append((start, end))
        dest.mkdir()
        frame = dest / "video-frame.png"
        frame.write_text(marker)
        errors = [{"stage": "video_decode", "error": "unreadable sample"}] if failed and start == 1001 else []
        return {"frames": [frame], "failures": errors}

    monkeypatch.setattr(ocr, "extract_frames", extract)
    controls.analyse(state, {"frames": [], "manifest": {"recordingStartedAt": 0}}, tmp_path / "recording.mp4",
                     "SCR123456", {}, lambda path: [path.read_text()], tmp_path)
    assert calls == [(1001, 1999), (4001, 4999)]
    assert state["target_reference"]["status"] == "PASSED"
    assert state["after"]["status"] == "PASSED"
    assert state["processing_failed"] is failed
    assert state["status"] == ("UNVERIFIED" if failed else "PASSED")
    assert state["before"]["broadcast"]["status"] == ("OCR_FAILED" if failed else "PASSED")
    assert len(list((tmp_path / "control_frames").glob("*.png"))) == 2


# Reject a shared boundary frame even when it contains the expected recorder marker.
def test_a_boundary_sample_cannot_verify_both_recorder_controls(tmp_path, monkeypatch, control_state):
    monkeypatch.setattr(ocr, "is_obscured", lambda image: False)
    control_state["after"].update({"start_ms": 2000, "end_ms": 3000})
    frame = tmp_path / "broadcast-screenshot-2000.png"
    frame.write_text(control_state["recorder_text"])

    controls.analyse(control_state, {"frames": [frame], "manifest": {}}, None, "SCR123456", {},
                     lambda path: [path.read_text()], tmp_path)

    assert control_state["status"] == "UNVERIFIED"
    assert all(control_state[phase]["broadcast"]["status"] == "NO_FRAMES" for phase in ("before", "after"))


# Reject non-terminating hold values before using the device or starting the foreground loop.
@pytest.mark.parametrize("duration", [float("inf"), float("nan"), 0])
def test_target_hold_rejects_invalid_durations(tmp_path, duration):
    with pytest.raises(ValueError, match="finite and greater than zero"):
        controls.record_target(None, "target", {}, tmp_path, duration)


# Retain round-trip uncertainty rather than treating whole-second device time as an exact timestamp.
def test_clock_alignment_includes_resolution_and_request_latency(monkeypatch):
    moments = iter([1.0, 1.2])
    monkeypatch.setattr(controls, "time", SimpleNamespace(time=lambda: next(moments)))
    measured = controls._clock(SimpleNamespace(device_time_ms=lambda: 1000))
    assert measured == {"status": "MEASURED", "offset_min_ms": -200, "offset_max_ms": 1000}
