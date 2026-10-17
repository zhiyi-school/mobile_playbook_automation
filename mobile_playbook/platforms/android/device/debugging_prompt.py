"""
Approves Android's adb debugging prompt on a Corellium instance by recognising the prompt in a screenshot and tapping
its Allow button through the shared Corellium touch input.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import os
import re
import struct
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from mobile_playbook.platforms.android.device.adb import AdbClient
from mobile_playbook.platforms.android.device.adb_connection import READY, UNAUTHORIZED, AdbConnectionError, boot_completed
from mobile_playbook.platforms.corellium.client import CorelliumClient, CorelliumError

logger = logging.getLogger(__name__)

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
DIAGNOSTICS_FOLDER = "debug-authorization"


class UnrecognizedPrompt(Exception):
    """A screenshot does not show the supported adb debugging prompt."""


class RecognitionError(RuntimeError):
    """The text recogniser could not read a screenshot."""


class DebuggingAuthorizationError(AdbConnectionError):
    """The adb debugging prompt could not be approved; diagnostics name the saved screenshot and reason."""

    # Keep the diagnostics path alongside the adb state the connection error reports.
    def __init__(self, message: str, *, serial: str, state: str, diagnostics: Path | None):
        super().__init__(message, serial=serial, state=state)
        self.diagnostics = diagnostics


@dataclass(frozen=True)
class Word:
    """One recognised word, its pixel box in the screenshot and the text line it belongs to."""

    text: str
    left: int
    top: int
    width: int
    height: int
    line: tuple[int, ...]

    # Return the word lowercased without punctuation, for comparing against prompt text.
    @property
    def key(self) -> str:
        return re.sub(r"[^a-z0-9]", "", self.text.lower())


@dataclass(frozen=True)
class PromptMatch:
    """Where to tap Allow, in the pixel coordinates of the screenshot it was found in."""

    x: int
    y: int
    width: int
    height: int
    orientation: str


@dataclass(frozen=True)
class DebuggingPrompt:
    """The text that identifies Android's adb debugging prompt and labels its buttons."""

    title: tuple[str, ...] = ("allow", "usb", "debugging")
    message: tuple[str, ...] = ("rsa", "key", "fingerprint")
    allow_labels: frozenset[str] = frozenset({"allow"})
    button_labels: frozenset[str] = frozenset({"allow", "cancel"})


class WordRecognizer(Protocol):
    # Return the words recognised in a PNG image file, raising RecognitionError when recognition cannot run.
    def words(self, image: Path) -> list[Word]: ...


@dataclass(frozen=True)
class TesseractWords:
    """Recognises words and their boxes with the Tesseract command line."""

    executable: str = "tesseract"
    language: str = "eng"
    timeout_seconds: float = 30.0

    # Run Tesseract's TSV output on the image and parse its words.
    def words(self, image: Path) -> list[Word]:
        command = [self.executable, str(image), "stdout", "-l", self.language, "--psm", "11", "tsv"]
        try:
            process = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                     timeout=self.timeout_seconds, check=False)
        except subprocess.TimeoutExpired as exc:
            raise RecognitionError(f"Tesseract timed out after {self.timeout_seconds:g}s") from exc
        except OSError as exc:
            raise RecognitionError(f"Could not run Tesseract ({self.executable}): {exc}") from exc
        if process.returncode:
            raise RecognitionError(f"Tesseract exited with status {process.returncode}: {(process.stderr or '').strip()[:500]}")
        return parse_tsv(process.stdout)


# Parse Tesseract TSV output into its recognised words, skipping layout rows and empty text.
def parse_tsv(text: str) -> list[Word]:
    rows = text.splitlines()
    if not rows:
        return []
    columns = {name: index for index, name in enumerate(rows[0].split("\t"))}
    words = []
    for row in rows[1:]:
        cells = row.split("\t")
        if len(cells) != len(columns) or cells[columns["level"]] != "5" or not cells[columns["text"]].strip():
            continue
        try:
            if float(cells[columns["conf"]]) < 0:
                continue
            box = [int(cells[columns[name]]) for name in ("left", "top", "width", "height")]
            line = tuple(int(cells[columns[name]]) for name in ("page_num", "block_num", "par_num", "line_num"))
        except ValueError:
            continue
        words.append(Word(cells[columns["text"]].strip(), *box, line=line))
    return words


# Return a PNG's pixel width and height from its header.
def png_size(data: bytes) -> tuple[int, int]:
    if len(data) < 24 or data[:8] != PNG_SIGNATURE or data[12:16] != b"IHDR":
        raise UnrecognizedPrompt("the screenshot is not a PNG image")
    width, height = struct.unpack(">II", data[16:24])
    if not width or not height:
        raise UnrecognizedPrompt("the screenshot has no pixels")
    return width, height


# Name the orientation of a screenshot from its dimensions.
def orientation(width: int, height: int) -> str:
    return "landscape" if width > height else "portrait"


# Find the Allow button of the debugging prompt in the recognised words, or explain why the screen is not that prompt.
def locate_allow(words: list[Word], width: int, height: int, prompt: DebuggingPrompt | None = None) -> PromptMatch:
    prompt = prompt or DebuggingPrompt()
    lines: dict[tuple[int, ...], list[Word]] = {}
    for word in sorted(words, key=lambda item: (item.top, item.left)):
        if word.key:
            lines.setdefault(word.line, []).append(word)
    keys = {line: [word.key for word in line_words] for line, line_words in lines.items()}
    titles = [line for line, line_keys in keys.items() if tuple(line_keys[: len(prompt.title)]) == prompt.title]
    if not titles:
        raise UnrecognizedPrompt("the screen does not show the adb debugging prompt title")
    if len(titles) > 1:
        raise UnrecognizedPrompt("the adb debugging prompt title appears more than once")
    if not any(_contains(line_keys, prompt.message) for line_keys in keys.values()):
        raise UnrecognizedPrompt("the prompt does not show the host's RSA key fingerprint")
    title_bottom = max(word.top + word.height for word in lines[titles[0]])
    buttons = [
        word
        for line, line_words in lines.items()
        if line != titles[0] and set(keys[line]) <= prompt.button_labels
        for word in line_words
        if word.key in prompt.allow_labels and word.top >= title_bottom
    ]
    if not buttons:
        raise UnrecognizedPrompt("the prompt shows no Allow button")
    if len(buttons) > 1:
        raise UnrecognizedPrompt("the prompt shows more than one Allow button")
    button = buttons[0]
    x, y = button.left + button.width // 2, button.top + button.height // 2
    if not (0 <= x < width and 0 <= y < height):
        raise UnrecognizedPrompt("the Allow button lies outside the screenshot")
    return PromptMatch(x, y, width, height, orientation(width, height))


# Report whether a sequence of words appears contiguously in a line.
def _contains(line_keys: list[str], phrase: tuple[str, ...]) -> bool:
    return any(tuple(line_keys[index : index + len(phrase)]) == phrase for index in range(len(line_keys) - len(phrase) + 1))


# Return the path of this host's adb public key, where adb keeps it.
def adb_key_path() -> Path:
    home = os.environ.get("ANDROID_USER_HOME")
    return Path(home) / "adbkey.pub" if home else Path.home() / ".android" / "adbkey.pub"


# Return the fingerprint Android shows for this host's adb key, or None when the host has no readable key.
def host_adb_key_fingerprint(path: Path | None = None) -> str | None:
    try:
        blob = base64.b64decode((path or adb_key_path()).read_text().split()[0], validate=True)
    except (OSError, IndexError, ValueError, binascii.Error):
        return None
    digest = hashlib.md5(blob, usedforsecurity=False).hexdigest().upper()
    return ":".join(digest[index : index + 2] for index in range(0, len(digest), 2))


class DebuggingAuthorizer:
    """Approves the adb debugging prompt on one exact Corellium instance, then waits for adb to be ready."""

    # Bind the authorizer to the instance and adb serial it approves, and to where failure diagnostics go.
    def __init__(
        self,
        client: CorelliumClient,
        instance_id: str,
        serial: str,
        *,
        diagnostics_dir: Path,
        recognizer: WordRecognizer | None = None,
        prompt: DebuggingPrompt | None = None,
        prompt_timeout: float = 30.0,
        approval_timeout: float = 120.0,
        poll: float = 2.0,
    ):
        self.client = client
        self.instance_id = instance_id
        self.serial = serial
        self.diagnostics_dir = diagnostics_dir
        self.recognizer = recognizer or TesseractWords()
        self.prompt = prompt or DebuggingPrompt()
        self.prompt_timeout = prompt_timeout
        self.approval_timeout = approval_timeout
        self.poll = poll

    # Tap Allow on the recognised prompt and wait until adb lists the device as ready and Android has booted.
    def __call__(self, host: AdbClient, phone: AdbClient) -> None:
        logger.warning(
            "adb on %s is unauthorized; approving the debugging prompt from a screenshot. The baseline should already "
            "authorize this host's adb key (fingerprint %s).",
            self.serial,
            host_adb_key_fingerprint() or "unavailable",
        )
        match, image, words = self._find_prompt()
        logger.info("Tapping Allow at (%s, %s) on the %sx%s %s screenshot of Corellium instance %s.",
                    match.x, match.y, match.width, match.height, match.orientation, self.instance_id)
        try:
            self.client.touch(self.instance_id, match.x, match.y)
        except CorelliumError as exc:
            raise self._fail(f"tapping Allow failed: {exc}", image, words, match) from exc
        deadline = time.monotonic() + self.approval_timeout
        while not (host.device_states().get(self.serial) == READY and boot_completed(phone)):
            if time.monotonic() >= deadline:
                raise self._fail(f"adb did not become ready within {self.approval_timeout:g}s of tapping Allow",
                                 self._screenshot_or_none(), None, match)
            time.sleep(min(self.poll, max(0.0, deadline - time.monotonic())))

    # Poll screenshots until the prompt is recognised, failing with diagnostics when it never appears.
    def _find_prompt(self) -> tuple[PromptMatch, bytes, list[Word]]:
        deadline = time.monotonic() + self.prompt_timeout
        while True:
            try:
                image = self.client.screenshot(self.instance_id, format="png")
            except CorelliumError as exc:
                raise self._fail(f"taking a screenshot failed: {exc}", None, None, None) from exc
            words: list[Word] = []
            try:
                width, height = png_size(image)
                words = self._recognize(image)
                return locate_allow(words, width, height, self.prompt), image, words
            except UnrecognizedPrompt as exc:
                reason = str(exc)
            except RecognitionError as exc:
                raise self._fail(f"text recognition failed: {exc}", image, None, None) from exc
            if time.monotonic() >= deadline:
                raise self._fail(f"an unfamiliar screen is showing ({reason})", image, words, None)
            time.sleep(min(self.poll, max(0.0, deadline - time.monotonic())))

    # Recognise the words of a screenshot through a temporary PNG file.
    def _recognize(self, image: bytes) -> list[Word]:
        with tempfile.TemporaryDirectory(prefix="adb-prompt-") as directory:
            path = Path(directory) / "screenshot.png"
            path.write_bytes(image)
            return self.recognizer.words(path)

    # Return a fresh screenshot for diagnostics, or None when Corellium cannot provide one.
    def _screenshot_or_none(self) -> bytes | None:
        try:
            return self.client.screenshot(self.instance_id, format="png")
        except CorelliumError as exc:
            logger.debug("android adb prompt: diagnostic screenshot of %s failed: %s", self.instance_id, exc)
            return None

    # Save the screenshot and a JSON description of the failure, and return the error that reports them.
    def _fail(self, reason: str, image: bytes | None, words: list[Word] | None, match: PromptMatch | None) -> DebuggingAuthorizationError:
        stem = f"{re.sub(r'[^A-Za-z0-9._-]', '_', self.instance_id)}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}"
        directory = self.diagnostics_dir / DIAGNOSTICS_FOLDER
        details = {
            "instance_id": self.instance_id,
            "serial": self.serial,
            "reason": reason,
            "screenshot": None,
            "host_key_fingerprint": host_adb_key_fingerprint(),
            "tap": {"x": match.x, "y": match.y} if match else None,
            "recognized_lines": _line_texts(words or []),
        }
        saved: Path | None = directory / f"{stem}.json"
        try:
            directory.mkdir(parents=True, exist_ok=True)
            if image is not None:
                (directory / f"{stem}.png").write_bytes(image)
                details["screenshot"] = f"{stem}.png"
                try:
                    width, height = png_size(image)
                    details.update(width=width, height=height, orientation=orientation(width, height))
                except UnrecognizedPrompt:
                    pass
            saved.write_text(json.dumps(details, indent=2) + "\n", encoding="utf-8")
        except OSError as exc:
            logger.warning("Could not save debugging-prompt diagnostics to %s: %s", directory, exc)
            saved = None
        location = f" Diagnostics: {saved}." if saved else ""
        return DebuggingAuthorizationError(
            f"Could not approve adb debugging on Corellium instance {self.instance_id}: {reason}.{location}",
            serial=self.serial,
            state=UNAUTHORIZED,
            diagnostics=saved,
        )


# Join recognised words back into their lines, top to bottom.
def _line_texts(words: list[Word]) -> list[str]:
    lines: dict[tuple[int, ...], list[Word]] = {}
    for word in sorted(words, key=lambda item: (item.top, item.left)):
        lines.setdefault(word.line, []).append(word)
    return [" ".join(word.text for word in line) for line in lines.values()]
