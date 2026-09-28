from __future__ import annotations

import sys
from pathlib import Path

import pytest

from mobile_playbook.platforms.ios import screen_capture_ocr as ocr


def _frame(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.write_bytes(b"png")
    return path


def test_frames_in_window_keeps_screenshots_timestamped_inside_the_window(tmp_path):
    frames = [
        _frame(tmp_path, "broadcast-screenshot-999.png"),
        _frame(tmp_path, "broadcast-screenshot-2000.png"),
        _frame(tmp_path, "broadcast-screenshot-1000.png"),
        _frame(tmp_path, "broadcast-screenshot-3000.png"),
        _frame(tmp_path, "broadcast-screenshot-3001.png"),
        _frame(tmp_path, "screenshot-2000.png"),
    ]

    selected = ocr.frames_in_window(frames, 1000, 3000)

    assert [frame.name for frame in selected] == [
        "broadcast-screenshot-1000.png",
        "broadcast-screenshot-2000.png",
        "broadcast-screenshot-3000.png",
    ]


def test_scan_frames_finds_plain_and_secure_canaries(tmp_path, monkeypatch):
    monkeypatch.setattr(ocr, "is_obscured", lambda image: False)
    first, second = _frame(tmp_path, "a.png"), _frame(tmp_path, "b.png")
    text = {first: ["Username", "SCR 123 456"], second: ["Password", "pwd123456"]}

    scan = ocr.scan_frames([first, second], {"plain": "SCR123456", "secure": "PWD123456"}, lambda image: text[image])

    assert scan["frames_scanned"] == 2
    assert scan["obscured_frames"] == []
    assert [(match["frame"], match["canary_kind"]) for match in scan["matches"]] == [
        (str(first), "plain"),
        (str(second), "secure"),
    ]


def test_scan_frames_reports_no_match_when_the_canary_is_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(ocr, "is_obscured", lambda image: False)

    scan = ocr.scan_frames([_frame(tmp_path, "a.png")], {"plain": "SCR123456"}, lambda image: ["Password", "••••••"])

    assert scan["matches"] == []


def test_scan_frames_skips_obscured_frames_without_reading_them(tmp_path, monkeypatch):
    blank, readable = _frame(tmp_path, "blank.png"), _frame(tmp_path, "readable.png")
    monkeypatch.setattr(ocr, "is_obscured", lambda image: image == blank)
    read: list[Path] = []

    def recogniser(image):
        read.append(image)
        return ["SCR123456"]

    scan = ocr.scan_frames([blank, readable], {"plain": "SCR123456"}, recogniser)

    assert scan["obscured_frames"] == [str(blank)]
    assert read == [readable]
    assert len(scan["matches"]) == 1


def test_ocr_is_unavailable_when_vision_cannot_be_imported(monkeypatch):
    monkeypatch.setitem(sys.modules, "Vision", None)

    assert ocr.ocr_available() is False


def test_is_obscured_tells_a_blank_frame_from_a_busy_one(tmp_path):
    Quartz = pytest.importorskip("Quartz")
    pytest.importorskip("Foundation")

    def write(path: Path, pattern) -> Path:
        side = 64
        pixels = bytes(pattern(x, y) for y in range(side) for x in range(side))
        provider = Quartz.CGDataProviderCreateWithData(None, pixels, len(pixels), None)
        image = Quartz.CGImageCreate(
            side, side, 8, 8, side, Quartz.CGColorSpaceCreateDeviceGray(), Quartz.kCGImageAlphaNone,
            provider, None, False, Quartz.kCGRenderingIntentDefault,
        )
        ocr._write_png(Quartz, image, path)
        return path

    blank = write(tmp_path / "blank.png", lambda x, y: 255)
    busy = write(tmp_path / "busy.png", lambda x, y: 0 if (x // 4 + y // 4) % 2 else 255)

    assert ocr.is_obscured(blank) is True
    assert ocr.is_obscured(busy) is False


def _gray_frames(Quartz, patterns, side=64):
    images = []
    for pattern in patterns:
        pixels = bytes(pattern(x, y) for y in range(side) for x in range(side))
        provider = Quartz.CGDataProviderCreateWithData(None, pixels, len(pixels), None)
        images.append(Quartz.CGImageCreate(
            side, side, 8, 8, side, Quartz.CGColorSpaceCreateDeviceGray(), Quartz.kCGImageAlphaNone,
            provider, None, False, Quartz.kCGRenderingIntentDefault,
        ))
    return images


def _write_video(path: Path, images, side=64) -> Path:
    import time

    AVFoundation = pytest.importorskip("AVFoundation")
    CoreMedia = pytest.importorskip("CoreMedia")
    Quartz = pytest.importorskip("Quartz")
    from Foundation import NSURL

    writer, _ = AVFoundation.AVAssetWriter.assetWriterWithURL_fileType_error_(
        NSURL.fileURLWithPath_(str(path)), AVFoundation.AVFileTypeMPEG4, None
    )
    writer_input = AVFoundation.AVAssetWriterInput.assetWriterInputWithMediaType_outputSettings_(
        AVFoundation.AVMediaTypeVideo,
        {AVFoundation.AVVideoCodecKey: AVFoundation.AVVideoCodecTypeH264, AVFoundation.AVVideoWidthKey: side, AVFoundation.AVVideoHeightKey: side},
    )
    adaptor = AVFoundation.AVAssetWriterInputPixelBufferAdaptor.assetWriterInputPixelBufferAdaptorWithAssetWriterInput_sourcePixelBufferAttributes_(
        writer_input, {Quartz.kCVPixelBufferPixelFormatTypeKey: Quartz.kCVPixelFormatType_32BGRA}
    )
    writer.addInput_(writer_input)
    writer.startWriting()
    writer.startSessionAtSourceTime_(CoreMedia.CMTimeMake(0, 1))
    renderer = Quartz.CIContext.contextWithOptions_(None)
    for second, image in enumerate(images):
        _, buffer = Quartz.CVPixelBufferCreate(None, side, side, Quartz.kCVPixelFormatType_32BGRA, None, None)
        renderer.render_toCVPixelBuffer_(Quartz.CIImage.imageWithCGImage_(image), buffer)
        while not writer_input.isReadyForMoreMediaData():
            time.sleep(0.01)
        assert adaptor.appendPixelBuffer_withPresentationTime_(buffer, CoreMedia.CMTimeMake(second, 1))
    writer_input.markAsFinished()
    finished: list[bool] = []
    writer.finishWritingWithCompletionHandler_(lambda: finished.append(True))
    deadline = time.monotonic() + 10
    while not finished and time.monotonic() < deadline:
        time.sleep(0.02)
    assert finished and path.stat().st_size > 0
    return path


def test_extract_frames_samples_each_requested_second_of_a_real_video(tmp_path):
    Quartz = pytest.importorskip("Quartz")
    blank, busy = (lambda x, y: 0), (lambda x, y: 0 if (x // 4 + y // 4) % 2 else 255)
    video = _write_video(tmp_path / "broadcast-10.mp4", _gray_frames(Quartz, [blank, busy, blank, busy]))

    frames = ocr.extract_frames(video, 10_000, 11_000, 13_000, 1.0, tmp_path / "video_frames")

    assert [frame.name for frame in frames] == ["video-frame-00001000.png", "video-frame-00002000.png", "video-frame-00003000.png"]
    assert [ocr.is_obscured(frame) for frame in frames] == [False, True, False]


def test_recognise_text_reads_a_canary_rendered_on_screen(tmp_path):
    AppKit = pytest.importorskip("AppKit")
    pytest.importorskip("Vision")
    width, height = 600, 300
    rep = AppKit.NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, width, height, 8, 4, True, False, AppKit.NSDeviceRGBColorSpace, 0, 0
    )
    AppKit.NSGraphicsContext.saveGraphicsState()
    AppKit.NSGraphicsContext.setCurrentContext_(AppKit.NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep))
    AppKit.NSColor.whiteColor().set()
    AppKit.NSRectFill(((0, 0), (width, height)))
    attributes = {AppKit.NSFontAttributeName: AppKit.NSFont.systemFontOfSize_(48), AppKit.NSForegroundColorAttributeName: AppKit.NSColor.blackColor()}
    AppKit.NSString.stringWithString_("SCR134908").drawAtPoint_withAttributes_((60, 120), attributes)
    AppKit.NSGraphicsContext.restoreGraphicsState()
    frame = tmp_path / "broadcast-screenshot-1000.png"
    frame.write_bytes(bytes(rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, {})))

    scan = ocr.scan_frames([frame], {"plain": "SCR134908", "secure": "PWD134908"})

    assert [match["canary_kind"] for match in scan["matches"]] == ["plain"]
