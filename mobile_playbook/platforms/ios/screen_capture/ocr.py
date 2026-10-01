"""
Scans captured frames for canaries using an OCR provider, with macOS image and video processing.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from mobile_playbook.platforms.ios.screen_capture.providers import OCRProcessingError, OCRProvider, get_ocr_provider

logger = logging.getLogger(__name__)

_SHOT_MS = re.compile(r"broadcast-screenshot-(\d+)\.png$")
_OBSCURED_SAMPLE_SIDE = 64


# Explain missing image-processing or OCR dependencies for the configured capture.
def ocr_unavailable_reason(capture: dict | None = None, *, provider: OCRProvider | None = None) -> str | None:
    try:
        import Quartz  # noqa: F401
        import Foundation  # noqa: F401
    except ImportError as exc:
        return f"macOS image-processing dependencies unavailable: {exc}"
    return (provider if provider is not None else get_ocr_provider(capture)).availability_error()


# Explain missing native dependencies only when video frame extraction is needed.
def video_unavailable_reason() -> str | None:
    try:
        import AVFoundation  # noqa: F401
        import CoreMedia  # noqa: F401
        import Quartz  # noqa: F401
        import Foundation  # noqa: F401
    except ImportError as exc:
        return f"macOS video-processing dependencies unavailable: {exc}"
    return None


# Decode a frame, raising a processing error when the image cannot be loaded.
def _load_image(image: Path):
    import Quartz
    from Foundation import NSURL

    source = Quartz.CGImageSourceCreateWithURL(NSURL.fileURLWithPath_(str(image)), None)
    if source is None:
        raise OCRProcessingError("Could not open the frame image")
    cg_image = Quartz.CGImageSourceCreateImageAtIndex(source, 0, None)
    if cg_image is None:
        raise OCRProcessingError("Could not decode the frame image")
    return cg_image


# Return the screenshot frames whose timestamped names fall inside the window, sorted.
def frames_in_window(frames: list[Path], start_ms: int, end_ms: int) -> list[Path]:
    selected = []
    for frame in frames:
        match = _SHOT_MS.search(frame.name)
        if match and start_ms <= int(match.group(1)) <= end_ms:
            selected.append(frame)
    logger.debug("ios ocr: %s of %s frames inside window %s-%s ms", len(selected), len(frames), start_ms, end_ms)
    return sorted(selected)


# Extract usable PNG frames and retain decoding or writing errors for each requested video sample.
def extract_frames(recording: Path, recording_started_ms: int, start_ms: int, end_ms: int,
                   every_seconds: float, dest: Path) -> dict:
    import AVFoundation
    import CoreMedia
    import Quartz
    from Foundation import NSURL

    asset = AVFoundation.AVURLAsset.URLAssetWithURL_options_(NSURL.fileURLWithPath_(str(recording)), None)
    if asset is None:
        raise OCRProcessingError(f"Could not open the recording: {recording}")
    generator = AVFoundation.AVAssetImageGenerator.assetImageGeneratorWithAsset_(asset)
    if generator is None:
        raise OCRProcessingError(f"Could not initialise video decoding: {recording}")
    generator.setAppliesPreferredTrackTransform_(True)
    generator.setRequestedTimeToleranceBefore_(CoreMedia.kCMTimeZero)
    generator.setRequestedTimeToleranceAfter_(CoreMedia.kCMTimeZero)
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise OCRProcessingError(f"Could not create the video frame directory: {exc}") from exc
    out, failed = [], []
    t = max(0.0, (start_ms - recording_started_ms) / 1000)
    end = (end_ms - recording_started_ms) / 1000
    logger.debug("ios ocr: extracting frames from %s at %.2fs..%.2fs every %ss into %s", recording, t, end, every_seconds, dest)
    while t <= end:
        stage = "video_decode"
        try:
            # With actualTime=None, PyObjC returns the image and NSError only.
            cg_image, error = generator.copyCGImageAtTime_actualTime_error_(CoreMedia.CMTimeMakeWithSeconds(t, 600), None, None)
            if cg_image is None:
                raise OCRProcessingError(f"Could not decode video sample at {t:g}s: {error if error is not None else 'no image returned'}")
            stage = "png_write"
            path = dest / f"video-frame-{int(t * 1000):08d}.png"
            _write_png(Quartz, cg_image, path)
            out.append(path)
        except (OCRProcessingError, OSError) as exc:
            failed.append({"recording": str(recording), "sample_ms": int(t * 1000), "stage": stage, "error": str(exc)})
            logger.debug("ios ocr: video sample at %.2fs failed during %s: %s", t, stage, exc)
        t += every_seconds
    logger.debug("ios ocr: extracted %s video frames from %s; %s samples failed", len(out), recording, len(failed))
    return {"frames": out, "failures": failed}


# Return whether a decoded frame is near-uniform; raise if its pixels cannot be sampled.
def is_obscured(image: Path, threshold: float = 4.0) -> bool:
    import Quartz

    cg_image = _load_image(image)
    side = _OBSCURED_SAMPLE_SIDE
    context = Quartz.CGBitmapContextCreate(
        None, side, side, 8, side, Quartz.CGColorSpaceCreateDeviceGray(), Quartz.kCGImageAlphaNone
    )
    if context is None:
        raise OCRProcessingError("Could not create the frame sampling context")
    Quartz.CGContextDrawImage(context, Quartz.CGRectMake(0, 0, side, side), cg_image)
    sampled = Quartz.CGBitmapContextCreateImage(context)
    if sampled is None:
        raise OCRProcessingError("Could not sample the frame image")
    provider = Quartz.CGImageGetDataProvider(sampled)
    data = Quartz.CGDataProviderCopyData(provider) if provider is not None else None
    if data is None:
        raise OCRProcessingError("Could not read the sampled frame pixels")
    pixels = bytes(data)[: side * side]
    if len(pixels) != side * side:
        raise OCRProcessingError("The sampled frame pixel data is incomplete")
    mean = sum(pixels) / len(pixels)
    deviation = (sum((value - mean) ** 2 for value in pixels) / len(pixels)) ** 0.5
    logger.debug("ios ocr: %s mean=%.2f deviation=%.2f threshold=%s -> obscured=%s", image, mean, deviation, threshold, deviation < threshold)
    return deviation < threshold


# Scan each frame for canaries, retaining expected processing failures and continuing with other frames.
def scan_frames(frames: list[Path], canaries: dict[str, str], recogniser) -> dict:
    matches, obscured, failed = [], [], []
    logger.debug("ios ocr: scanning %s frames for canary kinds %s", len(frames), sorted(canaries))
    for frame in frames:
        try:
            if is_obscured(frame):
                logger.debug("ios ocr: %s obscured; not scanned", frame)
                obscured.append(str(frame))
                continue
            text = " ".join(recogniser(frame))
        except OCRProcessingError as exc:
            logger.debug("ios ocr: frame processing failed for %s: %s", frame, exc)
            failed.append({"frame": str(frame), "error": str(exc)})
            continue
        normalised = text.replace(" ", "").lower()
        logger.debug("ios ocr: %s text length %s", frame, len(text))
        for kind, canary in canaries.items():
            if canary.lower() in normalised:
                logger.debug("ios ocr: %s contains %s canary", frame, kind)
                matches.append({"frame": str(frame), "canary_kind": kind, "canary": canary, "text": text[:500]})
    logger.debug("ios ocr: scanned %s frames: %s obscured, %s failed, %s canary matches", len(frames), len(obscured), len(failed), len(matches))
    return {
        "frames_scanned": len(frames),
        "matched_frame_count": len({match["frame"] for match in matches}),
        "obscured_frames": obscured,
        "failed_frames": failed,
        "matches": matches,
    }


# Write a CGImage to PNG, raising a processing error when Quartz cannot create or finalize it.
def _write_png(Quartz, cg_image, path: Path) -> None:
    from Foundation import NSURL

    destination = Quartz.CGImageDestinationCreateWithURL(NSURL.fileURLWithPath_(str(path)), "public.png", 1, None)
    if destination is None:
        raise OCRProcessingError(f"Could not create the PNG destination: {path}")
    Quartz.CGImageDestinationAddImage(destination, cg_image, None)
    if not Quartz.CGImageDestinationFinalize(destination):
        logger.debug("ios ocr: CGImageDestinationFinalize failed for %s", path)
        raise OCRProcessingError(f"Could not finalize the PNG: {path}")
