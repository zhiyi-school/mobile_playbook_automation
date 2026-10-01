"""
Defines the OCR provider contract and the Tesseract adapter used for screen-capture analysis.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class OCRProcessingError(RuntimeError):
    """A frame could not be decoded or processed by the OCR pipeline."""


class OCRProvider(Protocol):
    name: str

    # Explain a missing runtime dependency, or return None when recognition can run.
    def availability_error(self) -> str | None: ...

    # Recognise a frame's text lines, raising OCRProcessingError on processing failure.
    def recognise_text(self, image: Path) -> list[str]: ...

    # Describe the engine and recognition settings saved with the capture evidence.
    def metadata(self) -> dict: ...


@dataclass
class TesseractOCR:
    executable: str = "tesseract"
    language: str = "eng"
    page_segmentation_mode: int = 11
    timeout_seconds: float = 30.0
    tessdata_dir: str | None = None
    name: str = "tesseract"

    # Execute Tesseract without a shell and turn expected process failures into OCR errors.
    def _run(self, arguments: list[str]) -> subprocess.CompletedProcess:
        try:
            process = subprocess.run(
                [self.executable, *arguments], capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=self.timeout_seconds, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise OCRProcessingError(f"Tesseract timed out after {self.timeout_seconds:g} seconds") from exc
        except OSError as exc:
            raise OCRProcessingError(f"Could not execute Tesseract ({self.executable}): {exc}") from exc
        if process.returncode:
            detail = (process.stderr or process.stdout or "no error detail supplied").strip()[:1000]
            raise OCRProcessingError(f"Tesseract exited with status {process.returncode}: {detail}")
        return process

    # Build language-data arguments shared by dependency checks and recognition.
    def _data_arguments(self) -> list[str]:
        return ["--tessdata-dir", self.tessdata_dir] if self.tessdata_dir else []

    # Verify that Tesseract can enumerate every configured recognition language.
    def availability_error(self) -> str | None:
        try:
            process = self._run([*self._data_arguments(), "--list-langs"])
        except OCRProcessingError as exc:
            return str(exc)
        available = set((process.stdout + "\n" + process.stderr).splitlines())
        missing = sorted(set(self.language.split("+")) - available)
        if missing:
            return f"Tesseract language data unavailable: {', '.join(missing)}"
        return None

    # Recognise scattered screen text with dictionary correction disabled for random canaries.
    def recognise_text(self, image: Path) -> list[str]:
        process = self._run([
            str(image.resolve()), "stdout", *self._data_arguments(), "-l", self.language,
            "--psm", str(self.page_segmentation_mode), "--oem", "1",
            "-c", "load_system_dawg=0", "-c", "load_freq_dawg=0",
        ])
        return [line.strip() for line in process.stdout.splitlines() if line.strip()]

    # Record reproducible recognition settings without capturing recognised text.
    def metadata(self) -> dict:
        return {
            "provider": self.name,
            "provider_settings": {
                "executable": self.executable, "language": self.language,
                "page_segmentation_mode": self.page_segmentation_mode,
                "timeout_seconds": self.timeout_seconds, "tessdata_dir": self.tessdata_dir,
                "engine_mode": 1, "dictionary_correction": False,
            },
        }


# Construct the configured OCR provider independently of capture and verdict logic.
def get_ocr_provider(capture: dict | None = None) -> OCRProvider:
    capture = capture or {}
    name = capture.get("ocr_provider", "tesseract")
    if name != "tesseract":
        raise ValueError(f"Unsupported OCR provider: {name}")
    settings = capture.get("tesseract") or {}
    return TesseractOCR(
        executable=str(settings.get("executable", "tesseract")),
        language=str(settings.get("language", "eng")),
        page_segmentation_mode=int(settings.get("page_segmentation_mode", 11)),
        timeout_seconds=float(settings.get("timeout_seconds", 30)),
        tessdata_dir=str(settings["tessdata_dir"]) if settings.get("tessdata_dir") else None,
    )
