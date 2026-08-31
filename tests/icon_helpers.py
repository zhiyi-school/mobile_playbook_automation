"""Builders for the archive and image shapes the icon extractor has to cope with."""

from __future__ import annotations

import plistlib
import struct
import zipfile
import zlib
from pathlib import Path
from typing import Any

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)


def _scanlines(width: int, height: int, pixel: tuple[int, int, int, int]) -> bytes:
    row = bytes(pixel) * width
    return b"".join(b"\x00" + row for _ in range(height))


def make_png(width: int = 16, height: int = 16, pixel: tuple[int, int, int, int] = (255, 0, 0, 255)) -> bytes:
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        PNG_SIGNATURE
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(_scanlines(width, height, pixel), 9))
        + _chunk(b"IEND", b"")
    )


ADAM7_PASSES = (
    (0, 0, 8, 8),
    (4, 0, 8, 8),
    (0, 4, 4, 8),
    (2, 0, 4, 4),
    (0, 2, 2, 4),
    (1, 0, 2, 2),
    (0, 1, 1, 2),
)


def make_cgbi_png(
    width: int = 16,
    height: int = 16,
    pixel: tuple[int, int, int, int] = (255, 0, 0, 255),
    pixel_at=None,
    interlace: int = 0,
) -> bytes:
    """A PNG in Apple's crushed form: CgBI chunk, BGRA order, raw deflate."""
    at = pixel_at or (lambda x, y: pixel)

    def bgra(x: int, y: int) -> bytes:
        red, green, blue, alpha = at(x, y)
        return bytes((blue, green, red, alpha))

    if interlace:
        raw = b""
        for x_start, y_start, x_step, y_step in ADAM7_PASSES:
            pass_width = -(-(width - x_start) // x_step)
            pass_height = -(-(height - y_start) // y_step)
            if pass_width <= 0 or pass_height <= 0:
                continue
            for row in range(pass_height):
                y = y_start + row * y_step
                raw += b"\x00" + b"".join(
                    bgra(x_start + column * x_step, y) for column in range(pass_width)
                )
    else:
        raw = b"".join(
            b"\x00" + b"".join(bgra(x, y) for x in range(width)) for y in range(height)
        )

    compressor = zlib.compressobj(9, zlib.DEFLATED, -zlib.MAX_WBITS)
    idat = compressor.compress(raw) + compressor.flush()
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, interlace)
    return (
        PNG_SIGNATURE
        + _chunk(b"CgBI", b"\x50\x00\x20\x06")
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", idat)
        + _chunk(b"IEND", b"")
    )


def make_ipa(
    path: Path,
    *,
    info_extra: dict[str, Any] | None = None,
    files: dict[str, bytes] | None = None,
    app_name: str = "Example.app",
    bundle_id: str = "com.example.placeholder",
) -> Path:
    info = {
        "CFBundleIdentifier": bundle_id,
        "CFBundleExecutable": "AppExec",
        "CFBundleDisplayName": "Example App",
        "CFBundleShortVersionString": "1.0.0",
        **(info_extra or {}),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"Payload/{app_name}/Info.plist", plistlib.dumps(info))
        zf.writestr(f"Payload/{app_name}/AppExec", b"binary")
        for name, data in (files or {}).items():
            zf.writestr(f"Payload/{app_name}/{name}", data)
    return path


def make_apk(path: Path, files: dict[str, bytes] | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("AndroidManifest.xml", b"\x03\x00\x08\x00binary-xml")
        for name, data in (files or {}).items():
            zf.writestr(name, data)
    return path


def primary_icon_info(*names: str) -> dict[str, Any]:
    return {"CFBundleIcons": {"CFBundlePrimaryIcon": {"CFBundleIconFiles": list(names)}}}
