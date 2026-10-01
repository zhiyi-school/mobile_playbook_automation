from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from mobile_playbook.platforms.ios.screen_capture import ocr
from tests.platforms.ios.screen_capture.helpers import wait_for_video_input


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


@pytest.mark.native_capture
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


# Write synthetic frames to a bounded native video, including optional initial gaps and rectangular images.
def _write_video(path: Path, images, side=64, first_second=0, height=None) -> Path:
    import time

    AVFoundation = pytest.importorskip("AVFoundation")
    CoreMedia = pytest.importorskip("CoreMedia")
    Quartz = pytest.importorskip("Quartz")
    from Foundation import NSURL

    height = side if height is None else height
    writer, _ = AVFoundation.AVAssetWriter.assetWriterWithURL_fileType_error_(
        NSURL.fileURLWithPath_(str(path)), AVFoundation.AVFileTypeMPEG4, None
    )
    writer_input = AVFoundation.AVAssetWriterInput.assetWriterInputWithMediaType_outputSettings_(
        AVFoundation.AVMediaTypeVideo,
        {AVFoundation.AVVideoCodecKey: AVFoundation.AVVideoCodecTypeH264, AVFoundation.AVVideoWidthKey: side, AVFoundation.AVVideoHeightKey: height},
    )
    adaptor = AVFoundation.AVAssetWriterInputPixelBufferAdaptor.assetWriterInputPixelBufferAdaptorWithAssetWriterInput_sourcePixelBufferAttributes_(
        writer_input, {Quartz.kCVPixelBufferPixelFormatTypeKey: Quartz.kCVPixelFormatType_32BGRA}
    )
    writer.addInput_(writer_input)
    writer.startWriting()
    writer.startSessionAtSourceTime_(CoreMedia.CMTimeMake(0, 1))
    renderer = Quartz.CIContext.contextWithOptions_(None)
    for second, image in enumerate(images, first_second):
        _, buffer = Quartz.CVPixelBufferCreate(None, side, height, Quartz.kCVPixelFormatType_32BGRA, None, None)
        renderer.render_toCVPixelBuffer_(Quartz.CIImage.imageWithCGImage_(image), buffer)
        wait_for_video_input(writer, writer_input, second)
        assert adaptor.appendPixelBuffer_withPresentationTime_(buffer, CoreMedia.CMTimeMake(second, 1))
    writer_input.markAsFinished()
    finished: list[bool] = []
    writer.finishWritingWithCompletionHandler_(lambda: finished.append(True))
    deadline = time.monotonic() + 10
    while not finished and time.monotonic() < deadline:
        time.sleep(0.02)
    assert finished and path.stat().st_size > 0
    return path


@pytest.mark.native_capture
def test_extract_frames_samples_each_requested_second_of_a_real_video(tmp_path):
    Quartz = pytest.importorskip("Quartz")
    blank, busy = (lambda x, y: 0), (lambda x, y: 0 if (x // 4 + y // 4) % 2 else 255)
    video = _write_video(tmp_path / "broadcast-10.mp4", _gray_frames(Quartz, [blank, busy, blank, busy]))

    extracted = ocr.extract_frames(video, 10_000, 11_000, 13_000, 1.0, tmp_path / "video_frames")
    frames = extracted["frames"]

    assert extracted["failures"] == []
    assert [frame.name for frame in frames] == ["video-frame-00001000.png", "video-frame-00002000.png", "video-frame-00003000.png"]
    assert [ocr.is_obscured(frame) for frame in frames] == [False, True, False]


@pytest.mark.parametrize("font_size, dark, compressed", [(48, False, False), (24, False, False), (16, True, False), (24, True, True)])
@pytest.mark.native_capture
def test_recognise_text_reads_canaries_rendered_on_screen(tmp_path, font_size, dark, compressed):
    AppKit = pytest.importorskip("AppKit")
    from mobile_playbook.platforms.ios.screen_capture.providers import TesseractOCR
    provider = TesseractOCR()
    unavailable = ocr.ocr_unavailable_reason(provider=provider)
    if unavailable:
        pytest.skip(unavailable)
    width, height = 600, 300
    rep = AppKit.NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, width, height, 8, 4, True, False, AppKit.NSDeviceRGBColorSpace, 0, 0
    )
    AppKit.NSGraphicsContext.saveGraphicsState()
    AppKit.NSGraphicsContext.setCurrentContext_(AppKit.NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep))
    (AppKit.NSColor.blackColor() if dark else AppKit.NSColor.whiteColor()).set()
    AppKit.NSRectFill(((0, 0), (width, height)))
    attributes = {AppKit.NSFontAttributeName: AppKit.NSFont.systemFontOfSize_(font_size), AppKit.NSForegroundColorAttributeName: AppKit.NSColor.whiteColor() if dark else AppKit.NSColor.blackColor()}
    AppKit.NSString.stringWithString_("SCR134908").drawAtPoint_withAttributes_((60, 120), attributes)
    AppKit.NSString.stringWithString_("PWD134908").drawAtPoint_withAttributes_((60, 60), attributes)
    AppKit.NSString.stringWithString_("CTL134908ABCDEF").drawAtPoint_withAttributes_((60, 180), attributes)
    AppKit.NSGraphicsContext.restoreGraphicsState()
    frame = tmp_path / ("compressed frame.jpg" if compressed else "broadcast-screenshot-1000.png")
    image_type = AppKit.NSBitmapImageFileTypeJPEG if compressed else AppKit.NSBitmapImageFileTypePNG
    properties = {AppKit.NSImageCompressionFactor: 0.5} if compressed else {}
    frame.write_bytes(bytes(rep.representationUsingType_properties_(image_type, properties)))

    scan = ocr.scan_frames([frame], {"plain": "SCR134908", "secure": "PWD134908"}, provider.recognise_text)

    assert [match["canary_kind"] for match in scan["matches"]] == ["plain", "secure"]
    assert scan["matched_frame_count"] == 1 and scan["failed_frames"] == []
    from mobile_playbook.platforms.ios.screen_capture.controls import analyse_reference
    reference = analyse_reference({"capture_status": "CAPTURED", "path": str(frame)}, "CTL134908ABCDEF", provider.recognise_text)
    assert reference["status"] == "PASSED"
    if (font_size, dark, compressed) == (24, False, False):
        from mobile_playbook.platforms.ios.screen_capture import coverage
        image = ocr._load_image(frame)
        Quartz = pytest.importorskip("Quartz")
        blank = _gray_frames(Quartz, [lambda x, y: 0], side=600)[0]
        video = _write_video(tmp_path / "controls.mp4", [image] * 4 + [blank] * 7 + [image] * 4, side=600, height=300)
        decoded = coverage._decode(video, (5, 9), {"before": (1, 2), "after": (12, 13)}, "CTL134908ABCDEF", provider.recognise_text, tmp_path)
        assert coverage.check_samples(decoded["samples"], 5, 9)["status"] == "PASSED"
        assert all(decoded["video_controls"][phase]["status"] == "PASSED" for phase in ("before", "after"))



@pytest.fixture
def image_decoder(monkeypatch):
    state = SimpleNamespace(source=object(), image=object())
    quartz = SimpleNamespace(
        CGImageSourceCreateWithURL=lambda url, options: state.source,
        CGImageSourceCreateImageAtIndex=lambda source, index, options: state.image,
    )
    foundation = SimpleNamespace(NSURL=SimpleNamespace(fileURLWithPath_=lambda path: path))
    monkeypatch.setitem(sys.modules, "Quartz", quartz)
    monkeypatch.setitem(sys.modules, "Foundation", foundation)
    state.quartz = quartz
    return state


@pytest.mark.parametrize("missing", ["source", "image"])
def test_image_decoder_rejects_an_unreadable_frame(tmp_path, image_decoder, missing):
    setattr(image_decoder, missing, None)

    with pytest.raises(ocr.OCRProcessingError, match="frame image"):
        ocr._load_image(_frame(tmp_path, "invalid.png"))


def test_scan_frames_records_an_image_decode_failure(tmp_path, image_decoder):
    image_decoder.image = None
    frame = _frame(tmp_path, "invalid.png")

    scan = ocr.scan_frames([frame], {"plain": "SCR123456"}, lambda image: pytest.fail("unreadable frames must not reach OCR"))

    assert scan["failed_frames"] == [{"frame": str(frame), "error": "Could not decode the frame image"}]
    assert scan["obscured_frames"] == []
    assert scan["matches"] == []


def test_is_obscured_reports_a_missing_sampling_context(tmp_path, image_decoder):
    quartz = image_decoder.quartz
    quartz.CGColorSpaceCreateDeviceGray = lambda: object()
    quartz.kCGImageAlphaNone = 0
    quartz.CGBitmapContextCreate = lambda *args: None

    with pytest.raises(ocr.OCRProcessingError, match="sampling context"):
        ocr.is_obscured(_frame(tmp_path, "frame.png"))


@pytest.mark.parametrize("text", [[], ["SCR123456"]])
def test_scan_frames_continues_after_a_recognition_failure(tmp_path, monkeypatch, text):
    monkeypatch.setattr(ocr, "is_obscured", lambda image: False)
    failed, readable = _frame(tmp_path, "failed.png"), _frame(tmp_path, "readable.png")
    visited = []

    def recognise(image):
        visited.append(image)
        if image == failed:
            raise ocr.OCRProcessingError("Tesseract recognition failed")
        return text

    scan = ocr.scan_frames([failed, readable], {"plain": "SCR123456"}, recognise)

    assert visited == [failed, readable]
    assert scan["frames_scanned"] == 2
    assert scan["failed_frames"] == [{"frame": str(failed), "error": "Tesseract recognition failed"}]
    assert [match["frame"] for match in scan["matches"]] == ([str(readable)] if text else [])


def test_scan_frames_does_not_hide_unexpected_recogniser_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(ocr, "is_obscured", lambda image: False)

    def broken_recogniser(image):
        raise ValueError("unexpected recogniser bug")

    with pytest.raises(ValueError, match="unexpected recogniser bug"):
        ocr.scan_frames([_frame(tmp_path, "frame.png")], {"plain": "SCR123456"}, broken_recogniser)


@pytest.mark.parametrize("missing", ["Quartz", "Foundation"])
def test_ocr_is_unavailable_when_an_image_dependency_is_missing(image_decoder, monkeypatch, missing):
    monkeypatch.setitem(sys.modules, missing, None)

    assert "macOS image-processing dependencies unavailable" in ocr.ocr_unavailable_reason()



def test_matching_frame_count_counts_each_frame_once_when_both_canaries_match(tmp_path, monkeypatch):
    monkeypatch.setattr(ocr, "is_obscured", lambda image: False)
    first, second, third = (_frame(tmp_path, name) for name in ("a.png", "b.png", "c.png"))
    text = {first: ["SCR123456", "PWD123456"], second: ["SCR123456"], third: ["Username"]}

    scan = ocr.scan_frames(
        [first, second, third], {"plain": "SCR123456", "secure": "PWD123456"}, lambda image: text[image]
    )

    assert scan["frames_scanned"] == 3
    assert scan["matched_frame_count"] == 2
    assert len(scan["matches"]) == 3



def test_a_stalled_synthetic_video_writer_is_cancelled_at_the_deadline(monkeypatch):
    from tests.platforms.ios.screen_capture import helpers

    cancelled = []
    clock = iter([0, 10])
    monkeypatch.setattr(helpers.time, "monotonic", lambda: next(clock))
    writer = SimpleNamespace(cancelWriting=lambda: cancelled.append(True))
    writer_input = SimpleNamespace(isReadyForMoreMediaData=lambda: False)

    with pytest.raises(pytest.fail.Exception, match="frame 2 within 10 seconds"):
        wait_for_video_input(writer, writer_input, 2)
    assert cancelled == [True]


# Provide a native-shaped decoder with one failed and one successful sample.
@pytest.fixture
def video_decoder(monkeypatch):
    state = SimpleNamespace(samples=[(None, "unreadable video sample"), (object(), None)], asset=object(), written=[])
    # Return the same image/error pair as the native PyObjC method.
    def sample(time, actual, error):
        return state.samples.pop(0)

    generator = SimpleNamespace(
        setAppliesPreferredTrackTransform_=lambda value: None,
        setRequestedTimeToleranceBefore_=lambda value: None,
        setRequestedTimeToleranceAfter_=lambda value: None,
        copyCGImageAtTime_actualTime_error_=sample,
    )
    state.generator = generator
    monkeypatch.setitem(sys.modules, "AVFoundation", SimpleNamespace(
        AVURLAsset=SimpleNamespace(URLAssetWithURL_options_=lambda url, options: state.asset),
        AVAssetImageGenerator=SimpleNamespace(assetImageGeneratorWithAsset_=lambda asset: state.generator),
    ))
    monkeypatch.setitem(sys.modules, "CoreMedia", SimpleNamespace(kCMTimeZero=0, CMTimeMakeWithSeconds=lambda seconds, scale: seconds))
    monkeypatch.setitem(sys.modules, "Quartz", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "Foundation", SimpleNamespace(NSURL=SimpleNamespace(fileURLWithPath_=lambda path: path)))

    # Save successful samples as temporary files for downstream assertions.
    def write(quartz, image, path):
        state.written.append(path)
        path.write_bytes(b"png")

    monkeypatch.setattr(ocr, "_write_png", write)
    return state


# Check that sample decoding and write errors preserve later usable frames.
@pytest.mark.parametrize("failure", ["decode", "no_image", "png_write"])
def test_video_extraction_retains_good_samples_after_expected_failures(tmp_path, monkeypatch, video_decoder, failure):
    if failure == "no_image":
        video_decoder.samples[0] = (None, None)
    elif failure == "png_write":
        video_decoder.samples[0] = (object(), None)
        write = ocr._write_png

        # Fail the first write while allowing the later sample to be retained.
        def fail_first(quartz, image, path):
            if path.name == "video-frame-00000000.png":
                raise OSError("PNG destination not writable")
            write(quartz, image, path)

        monkeypatch.setattr(ocr, "_write_png", fail_first)
    recording = tmp_path / "recording.mp4"

    extracted = ocr.extract_frames(recording, 0, 0, 1000, 1, tmp_path / "frames")

    assert [frame.name for frame in extracted["frames"]] == ["video-frame-00001000.png"]
    assert extracted["frames"][0].read_bytes() == b"png"
    error, = extracted["failures"]
    assert error["recording"] == str(recording) and error["sample_ms"] == 0
    assert error["stage"] == ("png_write" if failure == "png_write" else "video_decode")
    assert {"decode": "unreadable video sample", "no_image": "no image returned", "png_write": "not writable"}[failure] in error["error"]


# Check that unavailable decoder objects produce typed processing failures.
@pytest.mark.parametrize("missing", ["asset", "generator"])
def test_video_decoder_setup_failures_are_processing_errors(tmp_path, video_decoder, missing):
    setattr(video_decoder, missing, None)
    with pytest.raises(ocr.OCRProcessingError, match="Could not"):
        ocr.extract_frames(tmp_path / "recording.mp4", 0, 0, 1000, 1, tmp_path / "frames")


# Check that a blocked output directory is reported as an expected processing failure.
def test_video_destination_creation_failure_is_a_processing_error(tmp_path, video_decoder):
    destination = _frame(tmp_path, "not-a-directory")
    with pytest.raises(ocr.OCRProcessingError, match="Could not create the video frame directory"):
        ocr.extract_frames(tmp_path / "recording.mp4", 0, 0, 1000, 1, destination)


# Check Quartz failure signals at both PNG-writing boundaries.
@pytest.mark.parametrize("missing_destination", [True, False])
def test_png_creation_and_finalization_failures_are_processing_errors(tmp_path, image_decoder, missing_destination):
    quartz = SimpleNamespace(
        CGImageDestinationCreateWithURL=lambda *args: None if missing_destination else object(),
        CGImageDestinationAddImage=lambda *args: None,
        CGImageDestinationFinalize=lambda destination: False,
    )
    with pytest.raises(ocr.OCRProcessingError, match="PNG"):
        ocr._write_png(quartz, object(), tmp_path / "frame.png")


# Decode a real recording to verify coverage uses actual frame times and rejects visible regions.
@pytest.mark.native_capture
@pytest.mark.parametrize("visible", [False, True])
def test_native_video_coverage_reads_every_target_frame(tmp_path, visible):
    from mobile_playbook.platforms.ios.screen_capture import coverage
    Quartz = pytest.importorskip("Quartz")
    blank, busy = (lambda x, y: 0), (lambda x, y: 0 if (x // 4 + y // 4) % 2 else 255)
    images = _gray_frames(Quartz, [busy] + [busy if visible and index == 3 else blank for index in range(7)] + [busy])
    video = _write_video(tmp_path / "broadcast-0.mp4", images)

    decoded = coverage._decode(video, (2, 6), {"before": (0, 0.5), "after": (8, 8.5)}, "CTL134908ABCDEF", lambda path: ["CTL134908ABCDEF"], tmp_path)
    checked = coverage.check_samples(decoded["samples"], 2, 6)

    assert [sample[0] for sample in decoded["samples"]] == list(range(9))
    assert checked["status"] == ("CONTENT_PRESENT" if visible else "PASSED")
    assert all(decoded["video_controls"][phase]["status"] == "PASSED" for phase in ("before", "after"))
    assert {path.name for path in (tmp_path / "control_frames").iterdir()} == {"video-before.png", "video-after.png"}


# A tiny region can be lost by thumbnail sampling but must block the full-resolution blank verdict.
@pytest.mark.native_capture
def test_strict_blank_check_detects_a_small_visible_region():
    from mobile_playbook.platforms.ios.screen_capture import coverage
    Quartz = pytest.importorskip("Quartz")
    images = _gray_frames(Quartz, [lambda x, y: 255 if x == 1 and y == 1 else 0], side=256)
    assert coverage._is_uniform(images[0]) is False


# Reject a real corrupt stream even when its bytes begin with an MP4 file-type box.
@pytest.mark.native_capture
def test_native_coverage_rejects_a_corrupt_mp4(tmp_path):
    from mobile_playbook.platforms.ios.screen_capture import coverage
    pytest.importorskip("AVFoundation")
    video = tmp_path / "recording.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 2048)
    with pytest.raises(ocr.OCRProcessingError):
        coverage._decode(video, (2, 6), {}, "CTL134908ABCDEF", None, tmp_path)


# Keep the original media timestamps when the writer omitted the first samples.
@pytest.mark.native_capture
def test_native_coverage_does_not_hide_an_initial_video_gap(tmp_path):
    from mobile_playbook.platforms.ios.screen_capture import coverage
    Quartz = pytest.importorskip("Quartz")
    video = _write_video(tmp_path / "late.mp4", _gray_frames(Quartz, [lambda x, y: 0] * 6), first_second=3)
    with pytest.raises(ocr.OCRProcessingError, match="missing or edited segments"):
        coverage._decode(video, (2, 6), {}, "CTL134908ABCDEF", None, tmp_path)
