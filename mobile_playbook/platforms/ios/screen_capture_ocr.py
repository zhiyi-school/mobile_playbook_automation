from __future__ import annotations

import re
from pathlib import Path

_SHOT_MS = re.compile(r"broadcast-screenshot-(\d+)\.png$")
_OBSCURED_SAMPLE_SIDE = 64


def ocr_available() -> bool:
    try:
        import Vision  # noqa: F401
        return True
    except ImportError:
        return False


def recognise_text(image: Path) -> list[str]:
    import Quartz
    import Vision
    from Foundation import NSURL

    source = Quartz.CGImageSourceCreateWithURL(NSURL.fileURLWithPath_(str(image)), None)
    cg_image = Quartz.CGImageSourceCreateImageAtIndex(source, 0, None)
    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    request.setUsesLanguageCorrection_(False)   # canaries are not words
    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(cg_image, None)
    handler.performRequests_error_([request], None)
    return [obs.topCandidates_(1)[0].string() for obs in request.results() or []]


def frames_in_window(frames: list[Path], start_ms: int, end_ms: int) -> list[Path]:
    selected = []
    for frame in frames:
        match = _SHOT_MS.search(frame.name)
        if match and start_ms <= int(match.group(1)) <= end_ms:
            selected.append(frame)
    return sorted(selected)


def extract_frames(recording: Path, recording_started_ms: int, start_ms: int, end_ms: int,
                   every_seconds: float, dest: Path) -> list[Path]:
    """Video fallback when the recorder saved no screenshots inside the window."""
    import AVFoundation
    import CoreMedia
    import Quartz
    from Foundation import NSURL

    asset = AVFoundation.AVURLAsset.URLAssetWithURL_options_(NSURL.fileURLWithPath_(str(recording)), None)
    generator = AVFoundation.AVAssetImageGenerator.assetImageGeneratorWithAsset_(asset)
    generator.setAppliesPreferredTrackTransform_(True)
    generator.setRequestedTimeToleranceBefore_(CoreMedia.kCMTimeZero)
    generator.setRequestedTimeToleranceAfter_(CoreMedia.kCMTimeZero)
    dest.mkdir(parents=True, exist_ok=True)
    out, t = [], max(0.0, (start_ms - recording_started_ms) / 1000)
    end = (end_ms - recording_started_ms) / 1000
    while t <= end:
        cg_image = generator.copyCGImageAtTime_actualTime_error_(CoreMedia.CMTimeMakeWithSeconds(t, 600), None, None)[0]
        if cg_image is not None:
            path = dest / f"video-frame-{int(t * 1000):08d}.png"
            _write_png(Quartz, cg_image, path)
            out.append(path)
        t += every_seconds
    return out


def is_obscured(image: Path, threshold: float = 4.0) -> bool:
    """A near-uniform frame: the app blanked or covered itself while being captured."""
    import Quartz
    from Foundation import NSURL

    source = Quartz.CGImageSourceCreateWithURL(NSURL.fileURLWithPath_(str(image)), None)
    cg_image = Quartz.CGImageSourceCreateImageAtIndex(source, 0, None) if source is not None else None
    if cg_image is None:
        return False
    side = _OBSCURED_SAMPLE_SIDE
    context = Quartz.CGBitmapContextCreate(
        None, side, side, 8, side, Quartz.CGColorSpaceCreateDeviceGray(), Quartz.kCGImageAlphaNone
    )
    Quartz.CGContextDrawImage(context, Quartz.CGRectMake(0, 0, side, side), cg_image)
    sampled = Quartz.CGBitmapContextCreateImage(context)
    pixels = bytes(Quartz.CGDataProviderCopyData(Quartz.CGImageGetDataProvider(sampled)))[: side * side]
    mean = sum(pixels) / len(pixels)
    deviation = (sum((value - mean) ** 2 for value in pixels) / len(pixels)) ** 0.5
    return deviation < threshold


def scan_frames(frames: list[Path], canaries: dict[str, str], recogniser=recognise_text) -> dict:
    matches, obscured = [], []
    for frame in frames:
        if is_obscured(frame):
            obscured.append(str(frame))
            continue
        text = " ".join(recogniser(frame))
        normalised = text.replace(" ", "").lower()
        for kind, canary in canaries.items():
            if canary.lower() in normalised:
                matches.append({"frame": str(frame), "canary_kind": kind, "canary": canary, "text": text[:500]})
    return {"frames_scanned": len(frames), "obscured_frames": obscured, "matches": matches}


def _write_png(Quartz, cg_image, path: Path) -> None:
    from Foundation import NSURL

    destination = Quartz.CGImageDestinationCreateWithURL(NSURL.fileURLWithPath_(str(path)), "public.png", 1, None)
    Quartz.CGImageDestinationAddImage(destination, cg_image, None)
    if not Quartz.CGImageDestinationFinalize(destination):
        raise RuntimeError(f"Could not write {path}")
