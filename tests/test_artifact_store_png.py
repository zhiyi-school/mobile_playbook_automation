from __future__ import annotations

import struct
import zlib

import pytest

from mobile_playbook.artifact_store import png
from tests.icon_helpers import make_cgbi_png, make_png


def _pixels(data: bytes) -> bytes:
    position = 8
    idat = b""
    while position < len(data):
        (length,) = struct.unpack(">I", data[position : position + 4])
        kind = data[position + 4 : position + 8]
        if kind == b"IDAT":
            idat += data[position + 8 : position + 8 + length]
        position += 12 + length
    return zlib.decompress(idat)


def test_standard_png_passes_through_unchanged():
    original = make_png(24, 24)
    normalized = png.normalize(original)
    assert normalized.data == original
    assert (normalized.width, normalized.height) == (24, 24)


def test_cgbi_png_is_rebuilt_into_a_decodable_png():
    normalized = png.normalize(make_cgbi_png(4, 4, (10, 200, 30, 255)))

    assert png.looks_like_png(normalized.data)
    assert b"CgBI" not in normalized.data
    assert (normalized.width, normalized.height) == (4, 4)
    assert _pixels(normalized.data) == b"".join(b"\x00" + bytes((10, 200, 30, 255)) * 4 for _ in range(4))


def test_cgbi_png_undoes_premultiplied_alpha():
    half = 128
    crushed = make_cgbi_png(1, 1, (128, 64, 32, half))
    normalized = png.normalize(crushed)
    red, green, blue, alpha = _pixels(normalized.data)[1:5]
    assert alpha == half
    assert (red, green, blue) == (255, 127, 63)


def test_interlaced_cgbi_png_is_deinterlaced_correctly():
    def pattern(x, y):
        return (x * 8 % 256, y * 8 % 256, (x + y) * 4 % 256, 255)

    interlaced = png.normalize(make_cgbi_png(17, 13, pixel_at=pattern, interlace=1))
    progressive = png.normalize(make_cgbi_png(17, 13, pixel_at=pattern, interlace=0))

    assert (interlaced.width, interlaced.height) == (17, 13)
    assert _pixels(interlaced.data) == _pixels(progressive.data)
    assert b"CgBI" not in interlaced.data


def test_deinterlaced_output_declares_no_interlace():
    normalized = png.normalize(make_cgbi_png(9, 9, interlace=1))

    header = normalized.data[normalized.data.index(b"IHDR") + 4 :]
    assert header[12] == 0


def test_truncated_interlaced_cgbi_is_rejected():
    data = make_cgbi_png(16, 16, interlace=1)
    idat = data.index(b"IDAT")
    with pytest.raises(png.UnsupportedImage):
        png.normalize(data[:idat] + data[idat : idat + 12] + data[-12:])


def test_non_png_data_is_rejected():
    with pytest.raises(png.UnsupportedImage):
        png.normalize(b"GIF89a not a png")


def test_truncated_png_is_rejected():
    with pytest.raises(png.UnsupportedImage):
        png.normalize(make_png(8, 8)[:30])


def test_oversized_source_is_rejected():
    with pytest.raises(png.UnsupportedImage, match="size limit"):
        png.normalize(b"\x89PNG\r\n\x1a\n" + b"\x00" * (png.MAX_SOURCE_BYTES + 1))


def test_oversized_dimensions_are_rejected():
    header = struct.pack(">IIBBBBB", png.MAX_DIMENSION + 1, 8, 8, 6, 0, 0, 0)
    data = (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + header
        + struct.pack(">I", zlib.crc32(b"IHDR" + header) & 0xFFFFFFFF)
        + struct.pack(">I", 0)
        + b"IEND"
        + struct.pack(">I", zlib.crc32(b"IEND") & 0xFFFFFFFF)
    )
    with pytest.raises(png.UnsupportedImage, match="maximum icon dimensions"):
        png.normalize(data)


def test_cgbi_beyond_the_decoder_limit_is_rejected():
    oversized = png.MAX_CGBI_DIMENSION + 1
    with pytest.raises(png.UnsupportedImage, match="this decoder handles"):
        png.normalize(make_cgbi_png(oversized, 1))
