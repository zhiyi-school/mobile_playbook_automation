from __future__ import annotations

from copy import deepcopy
from contextlib import nullcontext
from types import SimpleNamespace
import sys

import pytest

from mobile_playbook.platforms.ios.screen_capture import coverage, ocr


# Provide successful independent controls and foreground observations over a twelve-second hold.
@pytest.fixture
def verified_capture(tmp_path, monkeypatch):
    monkeypatch.setattr(ocr, "video_unavailable_reason", lambda: None)
    state = {"status": "PASSED", "recorder_text": "CTL123456ABCDEF", "target_start_reference": {"status": "PASSED"},
             "before": {"start_ms": 0, "end_ms": 6000}, "after": {"start_ms": 18000, "end_ms": 24000},
             "target_window": {"start_ms": 6000, "end_ms": 18000,
                               "clock_before": {"status": "MEASURED", "offset_min_ms": 0, "offset_max_ms": 1000},
                               "clock_after": {"status": "MEASURED", "offset_min_ms": 0, "offset_max_ms": 1000},
                               "foreground": [{"at_ms": stamp, "app_state": 4} for stamp in range(6000, 18001, 1000)]}}
    pulled = {"frames": [tmp_path / "broadcast-screenshot-0.png"], "manifest": {"recordingStartedAt": 0}}
    decoded = {"samples": [(second, 8 <= second <= 17) for second in range(25)],
               "video_controls": {phase: {"status": "PASSED"} for phase in ("before", "after")}}
    calls = []

    # Keep native decoding injected while retaining the real coverage and timestamp decision.
    def decode(recording, target, windows, marker, recogniser, report_dir):
        calls.append((target, windows))
        return deepcopy(decoded)

    monkeypatch.setattr(coverage, "_decode", decode)
    return state, pulled, decoded, calls


# Check clock uncertainty expansion and marker windows against concrete expected video times.
def test_verified_coverage_uses_conservative_video_bounds(tmp_path, verified_capture):
    state, pulled, decoded, calls = verified_capture
    result = coverage.analyse(state, pulled, tmp_path / "recording.mp4", True, None, tmp_path)
    assert result["status"] == "PASSED"
    assert result["duration_seconds"] == 6 and result["decoded_frame_count"] == 9
    assert calls == [((9.0, 17.0), {"before": (2.0, 6.0), "after": (20.0, 24.0)})]
    assert result["max_gap_seconds"] == 1


# Reject every prerequisite failure before attempting native decoding.
@pytest.mark.parametrize("fault", ["controls", "start_reference", "header", "no_video", "clock_unavailable", "clock_jump", "slow_clock",
                                    "background", "foreground_gap", "short_window", "missing_first_shot", "late_first_shot", "no_origin"])
def test_incomplete_prerequisites_cannot_reduce_risk(tmp_path, verified_capture, fault):
    state, pulled, decoded, calls = verified_capture
    recording, valid = tmp_path / "recording.mp4", True
    window = state["target_window"]
    if fault == "controls":
        state["status"] = "UNVERIFIED"
    elif fault == "start_reference":
        state["target_start_reference"]["status"] = "CANARY_NOT_OBSERVED"
    elif fault == "header":
        valid = False
    elif fault == "no_video":
        recording = None
    elif fault == "clock_unavailable":
        window["clock_after"]["status"] = "UNAVAILABLE"
    elif fault == "clock_jump":
        window["clock_after"].update(offset_min_ms=3000, offset_max_ms=4000)
    elif fault == "slow_clock":
        window["clock_after"]["offset_max_ms"] = 2000
    elif fault == "background":
        window["foreground"][3]["app_state"] = 3
    elif fault == "foreground_gap":
        del window["foreground"][3:6]
    elif fault == "short_window":
        window["end_ms"] = 12000
    elif fault == "missing_first_shot":
        pulled["frames"] = []
    elif fault == "late_first_shot":
        pulled["frames"] = [tmp_path / "broadcast-screenshot-2000.png"]
    else:
        pulled["manifest"]["recordingStartedAt"] = None
    result = coverage.analyse(state, pulled, recording, valid, None, tmp_path)
    assert result["status"] == "UNVERIFIED" and result["reason"]
    assert calls == [] and result["processing_failed"] is False


# Missing edges, timestamp gaps, visible pixels and failed same-video controls all block Reduced Risk.
@pytest.mark.parametrize("fault, status", [("truncated", "INCOMPLETE"), ("late_start", "INCOMPLETE"), ("gap", "INCOMPLETE"),
                                           ("content", "CONTENT_PRESENT"), ("video_control", "UNVERIFIED"), ("control_ocr", "OCR_FAILED")])
def test_decoded_video_failures_are_retained(tmp_path, verified_capture, fault, status):
    state, pulled, decoded, calls = verified_capture
    if fault == "truncated":
        decoded["samples"] = decoded["samples"][:17]
    elif fault == "late_start":
        decoded["samples"] = decoded["samples"][10:]
    elif fault == "gap":
        del decoded["samples"][12:15]
    elif fault == "content":
        decoded["samples"][12] = (12, False)
    elif fault == "video_control":
        decoded["video_controls"]["before"]["status"] = "CANARY_NOT_OBSERVED"
    else:
        decoded["video_controls"]["before"].update(status="OCR_FAILED", failed_frames=[{"error": "unreadable control"}])
    result = coverage.analyse(state, pulled, tmp_path / "recording.mp4", True, None, tmp_path)
    assert result["status"] == status and result["reason"]
    assert result["processing_failed"] is (fault == "control_ocr")


# Keep missing dependencies and expected decode failures diagnostic; propagate programming bugs.
@pytest.mark.parametrize("error, status", [(ImportError("CoreVideo missing"), "UNAVAILABLE"), (ocr.OCRProcessingError("truncated stream"), "OCR_FAILED"), (ValueError("unexpected bug"), None)])
def test_coverage_processing_errors_are_classified(tmp_path, monkeypatch, verified_capture, error, status):
    state, pulled, decoded, calls = verified_capture

    # Raise the requested failure at the native decoding boundary.
    def fail(*args):
        raise error

    monkeypatch.setattr(coverage, "_decode", fail)
    if status is None:
        with pytest.raises(ValueError, match="unexpected bug"):
            coverage.analyse(state, pulled, tmp_path / "recording.mp4", True, None, tmp_path)
    else:
        result = coverage.analyse(state, pulled, tmp_path / "recording.mp4", True, None, tmp_path)
        assert result["status"] == status
        assert result["processing_failed"] is (status == "OCR_FAILED")
        assert str(error) in result["reason"]


# Emulate native reader errors without weakening their classification or cleanup expectations.
@pytest.mark.parametrize("fault", ["duplicate_time", "negative_time", "nan_time", "missing_pixels", "reader_failed", "empty_edit", "retimed", "invalid_edit"])
def test_native_reader_rejects_invalid_samples_and_edit_maps(tmp_path, monkeypatch, fault):
    samples = [SimpleNamespace(pts=0.0, pixels=object()), SimpleNamespace(pts=1.0, pixels=object())]
    if fault == "duplicate_time":
        samples[1].pts = 0
    elif fault == "negative_time":
        samples[0].pts = -1
    elif fault == "nan_time":
        samples[1].pts = float("nan")
    elif fault == "missing_pixels":
        samples[1].pixels = None
    mapping = SimpleNamespace(source=SimpleNamespace(duration=3), target=SimpleNamespace(start=0, duration=3))
    if fault == "retimed":
        mapping.target.duration = 6
    elif fault == "invalid_edit":
        mapping.target.start = float("nan")
    segment = SimpleNamespace(isEmpty=lambda: fault == "empty_edit", timeMapping=lambda: mapping)
    track = SimpleNamespace(segments=lambda: [segment], preferredTransform=lambda: None)
    asset = SimpleNamespace(tracksWithMediaType_=lambda kind: [track])
    cancelled = []
    reader = SimpleNamespace(canAddOutput_=lambda output: True, addOutput_=lambda output: None, startReading=lambda: True,
                             status=lambda: 3 if fault == "reader_failed" else 2, error=lambda: "truncated video",
                             cancelReading=lambda: cancelled.append(True))
    output = SimpleNamespace(copyNextSampleBuffer=lambda: samples.pop(0) if samples else None)
    image = SimpleNamespace(imageByApplyingTransform_=lambda transform: image, extent=lambda: None)
    context = SimpleNamespace(createCGImage_fromRect_=lambda image, rect: object())
    monkeypatch.setitem(sys.modules, "AVFoundation", SimpleNamespace(AVMediaTypeVideo="video", AVAssetReaderStatusCompleted=2,
                        AVURLAsset=SimpleNamespace(URLAssetWithURL_options_=lambda url, options: asset),
                        AVAssetReader=SimpleNamespace(assetReaderWithAsset_error_=lambda asset, error: (reader, None)),
                        AVAssetReaderTrackOutput=SimpleNamespace(assetReaderTrackOutputWithTrack_outputSettings_=lambda track, settings: output)))
    monkeypatch.setitem(sys.modules, "CoreMedia", SimpleNamespace(CMTimeGetSeconds=lambda value: value, CMSampleBufferGetPresentationTimeStamp=lambda sample: sample.pts,
                        CMSampleBufferGetImageBuffer=lambda sample: sample.pixels))
    monkeypatch.setitem(sys.modules, "Quartz", SimpleNamespace(kCVPixelBufferPixelFormatTypeKey="format", kCVPixelFormatType_32BGRA="BGRA",
                        CIContext=SimpleNamespace(contextWithOptions_=lambda options: context), CIImage=SimpleNamespace(imageWithCVPixelBuffer_=lambda buffer: image)))
    monkeypatch.setitem(sys.modules, "Foundation", SimpleNamespace(NSURL=SimpleNamespace(fileURLWithPath_=lambda path: path)))
    monkeypatch.setitem(sys.modules, "objc", SimpleNamespace(autorelease_pool=nullcontext))
    monkeypatch.setattr(coverage, "_is_uniform", lambda image: True)
    with pytest.raises(ocr.OCRProcessingError):
        coverage._decode(tmp_path / "recording.mp4", (0, 3), {}, "marker", None, tmp_path)
    assert cancelled == ([] if fault in {"empty_edit", "retimed", "invalid_edit"} else [True])


# Use the outer clock bounds so a small measured offset change cannot narrow away uncertainty.
def test_overlapping_clock_measurements_keep_their_outer_bounds(tmp_path, verified_capture):
    state, pulled, decoded, calls = verified_capture
    state["target_window"]["clock_after"].update(offset_min_ms=500, offset_max_ms=1500)
    checked = coverage.analyse(state, pulled, tmp_path / "recording.mp4", True, None, tmp_path)
    assert checked["offset_min_ms"] == 0 and checked["offset_max_ms"] == 1500
    assert calls[0][0] == (9.0, 17.5)
