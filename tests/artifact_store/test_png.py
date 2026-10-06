from __future__ import annotations

import struct
import zlib

import pytest

from mobile_playbook.artifact_store import png
from tests.icon_helpers import make_cgbi_png, make_png, make_png_rows


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


class TestDecoding:
    def test_rgba_is_returned_as_is(self):
        pixels, width, height = png.decode_rgba(make_png(2, 3, (10, 20, 30, 40)))

        assert (width, height) == (2, 3)
        assert pixels == bytes((10, 20, 30, 40)) * 6

    @pytest.mark.parametrize(
        ("color_type", "row", "extra", "expected"),
        [
            (0, b"\x80", {}, (128, 128, 128, 255)),
            (0, b"\x80", {"transparency": b"\x00\x80"}, (128, 128, 128, 0)),
            (2, b"\x01\x02\x03", {}, (1, 2, 3, 255)),
            (2, b"\x01\x02\x03", {"transparency": b"\x00\x01\x00\x02\x00\x03"}, (1, 2, 3, 0)),
            (3, b"\x01", {"palette": b"\x00\x00\x00\x2b\x57\x97"}, (0x2B, 0x57, 0x97, 255)),
            (3, b"\x01", {"palette": b"\x00\x00\x00\x2b\x57\x97", "transparency": b"\xff\x40"}, (0x2B, 0x57, 0x97, 0x40)),
            (4, b"\xff\x7f", {}, (255, 255, 255, 127)),
        ],
    )
    def test_every_eight_bit_colour_type_decodes_to_rgba(self, color_type, row, extra, expected):
        pixels, _, _ = png.decode_rgba(make_png_rows(color_type, [row], 1, **extra))

        assert tuple(pixels) == expected

    def test_filtered_rows_of_narrow_pixels_are_unfiltered_by_their_own_width(self):
        row = b"\x10\x20\x30\x40"
        pixels, width, _ = png.decode_rgba(make_png_rows(4, [row], 2, filter_type=1))

        assert width == 2
        assert pixels == bytes((0x10, 0x10, 0x10, 0x20, 0x30, 0x30, 0x30, 0x40))

    @pytest.mark.parametrize(
        "data",
        [
            b"not a png",
            make_cgbi_png(4, 4),
            make_png_rows(3, [b"\x05"], 1, palette=b"\x00\x00\x00"),
        ],
    )
    def test_formats_it_cannot_decode_are_rejected(self, data):
        with pytest.raises(png.UnsupportedImage):
            png.decode_rgba(data)

    def test_images_too_large_to_decode_quickly_are_rejected(self):
        with pytest.raises(png.UnsupportedImage):
            png.decode_rgba(make_png(png.MAX_DECODE_DIMENSION + 1, 1))


class TestAdaptiveIcons:
    def test_the_foreground_sits_over_the_background_and_is_cropped_to_the_visible_area(self):
        foreground = make_png_rows(6, [b"\x00\x00\x00\x00" * 108] * 54 + [b"\xff\xff\xff\xff" * 108] * 54, 108)
        background = make_png(54, 54, (0x2B, 0x57, 0x97, 255))

        composed = png.compose_adaptive_icon(foreground, background)
        pixels, width, height = png.decode_rgba(composed.data)

        assert (width, height) == (composed.width, composed.height) == (72, 72)
        assert tuple(pixels[:4]) == (0x2B, 0x57, 0x97, 255)
        assert tuple(pixels[-4:]) == (255, 255, 255, 255)

    def test_half_transparent_foreground_blends_with_the_background(self):
        composed = png.compose_adaptive_icon(make_png(3, 3, (255, 255, 255, 128)), make_png(3, 3, (0, 0, 0, 255)))

        assert tuple(png.decode_rgba(composed.data)[0][:4]) == (128, 128, 128, 255)

    def test_a_solid_colour_background_fills_behind_the_foreground(self):
        composed = png.compose_adaptive_icon(make_png(6, 6, (0, 0, 0, 0)), background_color=(1, 2, 3, 255))

        pixels, width, _ = png.decode_rgba(composed.data)
        assert width == 4
        assert pixels == bytes((1, 2, 3, 255)) * 16

    def test_a_missing_background_is_rejected(self):
        with pytest.raises(png.UnsupportedImage):
            png.compose_adaptive_icon(make_png(6, 6))

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("#ff2b5797", (0x2B, 0x57, 0x97, 255)),
            ("#2B5797", (0x2B, 0x57, 0x97, 255)),
            ("#8abc", (0xAA, 0xBB, 0xCC, 0x88)),
            ("#abc", (0xAA, 0xBB, 0xCC, 255)),
            ("@0x7f050001", None),
            ("#zzzzzz", None),
            ("", None),
        ],
    )
    def test_android_colour_values_are_parsed(self, value, expected):
        assert png.android_color(value) == expected
