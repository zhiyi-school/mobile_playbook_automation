from __future__ import annotations

import subprocess

import pytest

from mobile_playbook.platforms.ios.screen_capture import providers
from mobile_playbook.platforms.ios.screen_capture.providers import OCRProcessingError, TesseractOCR, get_ocr_provider


def test_provider_factory_preserves_configured_paths_languages_and_limits():
    provider = get_ocr_provider({"tesseract": {
        "executable": "/tools with spaces/tesseract", "language": "eng+deu",
        "page_segmentation_mode": 6, "timeout_seconds": 9, "tessdata_dir": "/language data",
    }})

    assert isinstance(provider, TesseractOCR)
    assert provider.metadata() == {"provider": "tesseract", "provider_settings": {
        "executable": "/tools with spaces/tesseract", "language": "eng+deu",
        "page_segmentation_mode": 6, "timeout_seconds": 9.0, "tessdata_dir": "/language data",
        "engine_mode": 1, "dictionary_correction": False,
    }}


@pytest.mark.parametrize("name", ["vision", "unknown", "none"])
def test_factory_rejects_a_provider_without_an_ocr_implementation(name):
    with pytest.raises(ValueError, match="Unsupported OCR provider"):
        get_ocr_provider({"ocr_provider": name})


@pytest.mark.parametrize("output, expected", [(" SCR123456 \n\nPWD123456\n\f", ["SCR123456", "PWD123456"]), ("\n\f", [])])
def test_tesseract_runs_without_a_shell_and_returns_text_lines(tmp_path, monkeypatch, output, expected):
    image = tmp_path / "capture with spaces;literal.png"
    image.write_bytes(b"png")
    seen = []

    def run(command, **kwargs):
        seen.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="Estimating resolution as 300")

    monkeypatch.setattr(providers.subprocess, "run", run)
    provider = TesseractOCR(executable="/tools with spaces/tesseract", language="eng+deu", tessdata_dir="/language data", timeout_seconds=7)

    assert provider.recognise_text(image) == expected
    command, options = seen[0]
    assert command == [
        "/tools with spaces/tesseract", str(image.resolve()), "stdout", "--tessdata-dir", "/language data",
        "-l", "eng+deu", "--psm", "11", "--oem", "1",
        "-c", "load_system_dawg=0", "-c", "load_freq_dawg=0",
    ]
    assert options == {"capture_output": True, "text": True, "encoding": "utf-8", "errors": "replace", "timeout": 7, "check": False}


@pytest.mark.parametrize("failure, message", [
    (FileNotFoundError("missing binary"), "Could not execute Tesseract"),
    (PermissionError("not executable"), "Could not execute Tesseract"),
    (subprocess.TimeoutExpired("tesseract", 30), "Tesseract timed out after 30 seconds"),
])
def test_expected_process_failures_are_ocr_errors(tmp_path, monkeypatch, failure, message):
    def run(command, **kwargs):
        raise failure

    monkeypatch.setattr(providers.subprocess, "run", run)

    with pytest.raises(OCRProcessingError, match=message):
        TesseractOCR().recognise_text(tmp_path / "frame.png")
    assert message in TesseractOCR().availability_error()


@pytest.mark.parametrize("detail", ["cannot decode image", ""])
def test_nonzero_exit_is_not_treated_as_no_text(tmp_path, monkeypatch, detail):
    monkeypatch.setattr(providers.subprocess, "run", lambda command, **kwargs: subprocess.CompletedProcess(command, 1, stdout="", stderr=detail))

    with pytest.raises(OCRProcessingError, match="exited with status 1") as error:
        TesseractOCR().recognise_text(tmp_path / "invalid.png")
    assert (detail or "no error detail supplied") in str(error.value)


@pytest.mark.parametrize("languages, language, expected", [
    (["eng", "osd"], "eng", None),
    (["eng", "deu"], "eng+deu", None),
    (["eng"], "eng+deu", "Tesseract language data unavailable: deu"),
    ([], "eng", "Tesseract language data unavailable: eng"),
])
def test_dependency_check_verifies_all_configured_languages(monkeypatch, languages, language, expected):
    seen = []

    def run(command, **kwargs):
        seen.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="List of available languages:\n" + "\n".join(languages), stderr="")

    monkeypatch.setattr(providers.subprocess, "run", run)

    assert TesseractOCR(language=language, tessdata_dir="/language data").availability_error() == expected
    assert seen == [["tesseract", "--tessdata-dir", "/language data", "--list-langs"]]


def test_unexpected_provider_errors_are_not_hidden(tmp_path, monkeypatch):
    def run(command, **kwargs):
        raise ValueError("unexpected adapter bug")

    monkeypatch.setattr(providers.subprocess, "run", run)
    with pytest.raises(ValueError, match="unexpected adapter bug"):
        TesseractOCR().recognise_text(tmp_path / "frame.png")


@pytest.mark.native_capture
def test_real_tesseract_rejects_a_corrupt_frame(tmp_path):
    provider = TesseractOCR()
    unavailable = provider.availability_error()
    if unavailable:
        pytest.skip(unavailable)
    image = tmp_path / "invalid frame.png"
    image.write_bytes(b"not a PNG")

    with pytest.raises(OCRProcessingError, match="exited with status"):
        provider.recognise_text(image)
