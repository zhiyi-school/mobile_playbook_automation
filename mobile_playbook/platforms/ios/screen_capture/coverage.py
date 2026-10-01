"""
Validates decoded video coverage and recorder controls before accepting an obscured target capture.
"""

from __future__ import annotations

import math
import re
import time
from pathlib import Path

from mobile_playbook.platforms.ios.screen_capture import ocr

_SHOT_MS = re.compile(r"broadcast-screenshot-(\d+)\.png$")
_MAX_GAP_SECONDS = 1.0
_EDGE_MARGIN_MS = 3000
_MIN_WINDOW_MS = 5000


# Require full-resolution RGB uniformity so a small readable region cannot qualify as blank.
def _is_uniform(image) -> bool:
    import Quartz

    width, height = Quartz.CGImageGetWidth(image), Quartz.CGImageGetHeight(image)
    context = Quartz.CGBitmapContextCreate(None, width, height, 8, width * 4,
                                          Quartz.CGColorSpaceCreateDeviceRGB(), Quartz.kCGImageAlphaPremultipliedLast)
    if context is None:
        raise ocr.OCRProcessingError("Could not inspect full-resolution video pixels")
    Quartz.CGContextDrawImage(context, Quartz.CGRectMake(0, 0, width, height), image)
    sampled = Quartz.CGBitmapContextCreateImage(context)
    provider = Quartz.CGImageGetDataProvider(sampled) if sampled is not None else None
    data = Quartz.CGDataProviderCopyData(provider) if provider is not None else None
    pixels = bytes(data) if data is not None else b""
    if not pixels or len(pixels) != width * height * 4:
        raise ocr.OCRProcessingError("Full-resolution video pixels are incomplete")
    return all(max(channel) - min(channel) <= 4 for channel in (pixels[index::4] for index in range(3)))


# Read every video sample, inspect target pixels and OCR controls from this same video.
def _decode(recording: Path, target: tuple[float, float], windows: dict, marker: str, recogniser, report_dir: Path) -> dict:
    import AVFoundation
    import CoreMedia
    import Quartz
    import objc
    from Foundation import NSURL

    asset = AVFoundation.AVURLAsset.URLAssetWithURL_options_(NSURL.fileURLWithPath_(str(recording)), None)
    if asset is None:
        raise ocr.OCRProcessingError("Could not open the recording for coverage validation")
    tracks = asset.tracksWithMediaType_(AVFoundation.AVMediaTypeVideo)
    if len(tracks) != 1:
        raise ocr.OCRProcessingError("Coverage validation requires exactly one video track")
    segments = tracks[0].segments()
    if len(segments) != 1 or segments[0].isEmpty():
        raise ocr.OCRProcessingError("Video track contains missing or edited segments; its timeline cannot be verified")
    mapping = segments[0].timeMapping()
    target_start = CoreMedia.CMTimeGetSeconds(mapping.target.start)
    source_duration = CoreMedia.CMTimeGetSeconds(mapping.source.duration)
    target_duration = CoreMedia.CMTimeGetSeconds(mapping.target.duration)
    if (not all(math.isfinite(value) for value in (target_start, source_duration, target_duration)) or target_duration <= 0 or
            abs(target_start) > 0.001 or abs(source_duration - target_duration) > 0.001):
        raise ocr.OCRProcessingError("Video track has an offset or retimed edit; its timeline cannot be verified")
    reader, error = AVFoundation.AVAssetReader.assetReaderWithAsset_error_(asset, None)
    if reader is None:
        raise ocr.OCRProcessingError(f"Could not initialise video coverage decoding: {error}")
    output = AVFoundation.AVAssetReaderTrackOutput.assetReaderTrackOutputWithTrack_outputSettings_(
        tracks[0], {Quartz.kCVPixelBufferPixelFormatTypeKey: Quartz.kCVPixelFormatType_32BGRA})
    if output is None or not reader.canAddOutput_(output):
        raise ocr.OCRProcessingError("Could not initialise the decoded video output")
    reader.addOutput_(output)
    if not reader.startReading():
        raise ocr.OCRProcessingError(f"Could not start video coverage decoding: {reader.error()}")
    context = Quartz.CIContext.contextWithOptions_(None)
    if context is None:
        reader.cancelReading()
        raise ocr.OCRProcessingError("Could not initialise video pixel inspection")
    samples, controls = [], {}
    previous = None
    deadline = time.monotonic() + 120
    try:
        while True:
            if time.monotonic() >= deadline:
                raise ocr.OCRProcessingError("Video coverage decoding exceeded 120 seconds")
            with objc.autorelease_pool():
                sample = output.copyNextSampleBuffer()
                if sample is None:
                    break
                pts = CoreMedia.CMTimeGetSeconds(CoreMedia.CMSampleBufferGetPresentationTimeStamp(sample))
                if not math.isfinite(pts) or pts < 0 or (previous is not None and pts <= previous):
                    raise ocr.OCRProcessingError("Video presentation timestamps are invalid or non-increasing")
                previous = pts
                # Preserve an initial media gap; rebasing the first decoded sample would conceal it.
                seconds = pts
                pixel_buffer = CoreMedia.CMSampleBufferGetImageBuffer(sample)
                if pixel_buffer is None:
                    raise ocr.OCRProcessingError("A decoded video sample has no pixel buffer")
                ci_image = Quartz.CIImage.imageWithCVPixelBuffer_(pixel_buffer)
                if ci_image is None:
                    raise ocr.OCRProcessingError("Could not load decoded video pixels")
                ci_image = ci_image.imageByApplyingTransform_(tracks[0].preferredTransform())
                image = context.createCGImage_fromRect_(ci_image, ci_image.extent())
                if image is None:
                    raise ocr.OCRProcessingError("Could not inspect a decoded video sample")
                # Keep one preceding sample to verify coverage of the first target instant.
                obscured = _is_uniform(image) if target[0] - _MAX_GAP_SECONDS <= seconds <= target[1] + _MAX_GAP_SECONDS else None
                samples.append((seconds, obscured))
                for phase, (start, end) in windows.items():
                    if phase not in controls and start <= seconds <= end:
                        path = report_dir / "control_frames" / f"video-{phase}.png"
                        path.parent.mkdir(exist_ok=True)
                        ocr._write_png(Quartz, image, path)
                        scan = ocr.scan_frames([path], {"recorder": marker}, recogniser)
                        controls[phase] = {**scan, "status": "OCR_FAILED" if scan["failed_frames"] else "PASSED" if scan["matches"] else "CANARY_NOT_OBSERVED",
                                           "evidence_frames": [str(path)]}
        if reader.status() != AVFoundation.AVAssetReaderStatusCompleted:
            raise ocr.OCRProcessingError(f"Video coverage decoding did not complete: {reader.error()}")
    finally:
        reader.cancelReading()
    return {"samples": samples, "video_controls": controls}


# Require bracketing samples, bounded gaps and obscured pixels across the complete selected interval.
def check_samples(samples: list, start: float, end: float) -> dict:
    before = [index for index, (timestamp, _) in enumerate(samples) if timestamp <= start]
    after = [index for index, (timestamp, _) in enumerate(samples) if timestamp >= end]
    if not before or not after:
        return {"status": "INCOMPLETE", "reason": "Decoded samples do not bracket the target window"}
    selected = samples[before[-1]:after[0] + 1]
    gaps = [right[0] - left[0] for left, right in zip(selected, selected[1:])]
    maximum = max(gaps, default=math.inf)
    result = {"decoded_frame_count": len(selected), "max_gap_seconds": maximum,
              "first_sample_seconds": selected[0][0], "last_sample_seconds": selected[-1][0]}
    if maximum > _MAX_GAP_SECONDS + 0.001:
        return {**result, "status": "INCOMPLETE", "reason": "Decoded samples have a gap greater than one second"}
    if not all(blank is True for _, blank in selected):
        return {**result, "status": "CONTENT_PRESENT", "reason": "Decoded target samples were not all blank or uniform"}
    return {**result, "status": "PASSED"}


# Validate independent visibility, foreground observations, time alignment and the same recording's pixels.
def analyse(state: dict, pulled: dict, recording: Path | None, recording_valid: bool, recogniser, report_dir: Path) -> dict:
    blocked = {"status": "UNVERIFIED", "processing_failed": False}
    if state.get("status") != "PASSED" or (state.get("target_start_reference") or {}).get("status") != "PASSED":
        return {**blocked, "reason": "Target visibility or recorder controls did not pass"}
    if not recording or not recording_valid:
        return {**blocked, "reason": "A recording with a valid MP4 header is required"}
    window = state.get("target_window") or {}
    clocks = [window.get("clock_before") or {}, window.get("clock_after") or {}]
    if any(clock.get("status") != "MEASURED" for clock in clocks):
        return {**blocked, "reason": "Device clock alignment was unavailable"}
    overlap = max(clock["offset_min_ms"] for clock in clocks) <= min(clock["offset_max_ms"] for clock in clocks)
    low = min(clock["offset_min_ms"] for clock in clocks)
    high = max(clock["offset_max_ms"] for clock in clocks)
    if not overlap or not 0 <= high - low <= 1500 or any(clock["offset_max_ms"] < clock["offset_min_ms"] for clock in clocks):
        return {**blocked, "reason": "Device clock changed or clock measurement was too slow"}
    observations = window.get("foreground") or []
    times = [item["at_ms"] for item in observations]
    gaps = [right - left for left, right in zip(times, times[1:])]
    if len(times) < 2 or any(item.get("app_state") != 4 for item in observations) or any(not 0 < gap <= 2000 for gap in gaps):
        return {**blocked, "reason": "Target foreground continuity was not verified"}
    start_ms, end_ms = window["start_ms"] + _EDGE_MARGIN_MS, window["end_ms"] - _EDGE_MARGIN_MS
    if end_ms - start_ms < _MIN_WINDOW_MS or times[0] > window["start_ms"] + 1000 or times[-1] < window["end_ms"] - 1000:
        return {**blocked, "reason": "The verified target window is shorter than five seconds or lacks foreground observations"}
    shots = [int(match.group(1)) for frame in pulled["frames"] if (match := _SHOT_MS.search(frame.name))]
    started = pulled["manifest"].get("recordingStartedAt")
    if not shots or not isinstance(started, int) or isinstance(started, bool) or not 0 <= min(shots) - started * 1000 < 1000:
        return {**blocked, "reason": "The recording's first-sample time could not be bounded"}
    # The companion starts the writer on its first video sample. Its first saved screenshot
    # must be within that first wall-clock second, bounding any earlier failed PNG saves.
    anchor_max, anchor_min = min(shots), min(shots) - 1000
    target = ((start_ms + low - anchor_max) / 1000, (end_ms + high - anchor_min) / 1000)
    windows = {phase: ((state[phase]["start_ms"] + high - anchor_min) / 1000,
                       (state[phase]["end_ms"] + low - anchor_max) / 1000) for phase in ("before", "after")}
    metadata = {"start_ms": start_ms, "end_ms": end_ms, "duration_seconds": (end_ms - start_ms) / 1000,
                "offset_min_ms": low, "offset_max_ms": high, "video_window_seconds": list(target),
                "recording_origin_min_ms": anchor_min, "recording_origin_max_ms": anchor_max}
    unavailable = ocr.video_unavailable_reason()
    if unavailable:
        return {**blocked, **metadata, "status": "UNAVAILABLE", "reason": unavailable}
    try:
        decoded = _decode(recording, target, windows, state["recorder_text"], recogniser, report_dir)
    except ImportError as exc:
        return {**blocked, **metadata, "status": "UNAVAILABLE", "reason": f"Video coverage dependency unavailable: {exc}"}
    except (ocr.OCRProcessingError, OSError) as exc:
        return {**metadata, "status": "OCR_FAILED", "processing_failed": True, "reason": str(exc)}
    controls = decoded["video_controls"]
    result = {**metadata, **check_samples(decoded["samples"], *target), "video_controls": controls,
              "processing_failed": any(control.get("failed_frames") for control in controls.values())}
    if any((controls.get(phase) or {}).get("status") != "PASSED" for phase in ("before", "after")):
        result.update({"status": "OCR_FAILED" if result["processing_failed"] else "UNVERIFIED",
                       "reason": "Recorder controls were not verified in this same video"})
    return result
