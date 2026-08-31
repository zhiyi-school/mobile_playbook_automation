from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

MAX_SOURCE_BYTES = 4 * 1024 * 1024
MAX_DIMENSION = 2048
MAX_CGBI_DIMENSION = 512

_RGBA8 = (8, 6)

#: Adam7 (x_start, y_start, x_step, y_step) per interlace pass.
ADAM7_PASSES = (
    (0, 0, 8, 8),
    (4, 0, 8, 8),
    (0, 4, 4, 8),
    (2, 0, 4, 4),
    (0, 2, 2, 4),
    (1, 0, 2, 2),
    (0, 1, 1, 2),
)


class UnsupportedImage(ValueError):
    pass


@dataclass(frozen=True)
class NormalizedPng:
    data: bytes
    width: int
    height: int


def looks_like_png(data: bytes) -> bool:
    return data[:8] == PNG_SIGNATURE


def _iter_chunks(data: bytes):
    position = 8
    while position + 8 <= len(data):
        (length,) = struct.unpack(">I", data[position : position + 4])
        kind = data[position + 4 : position + 8]
        body = data[position + 8 : position + 8 + length]
        if len(body) != length:
            raise UnsupportedImage("Truncated PNG chunk")
        yield kind, body
        position += 12 + length
        if kind == b"IEND":
            return
    raise UnsupportedImage("PNG data ended before IEND")


def _header(body: bytes) -> tuple[int, int, int, int, int]:
    if len(body) < 13:
        raise UnsupportedImage("Truncated PNG header")
    width, height, depth, color_type, _compression, _filter, interlace = struct.unpack(">IIBBBBB", body[:13])
    return width, height, depth, color_type, interlace


def normalize(data: bytes) -> NormalizedPng:
    """Validate a PNG and return one any browser can decode. See docs/api.md#application-icons."""
    if len(data) > MAX_SOURCE_BYTES:
        raise UnsupportedImage("Image is larger than the icon size limit")
    if not looks_like_png(data):
        raise UnsupportedImage("Not a PNG image")

    chunks = list(_iter_chunks(data))
    kinds = {kind for kind, _ in chunks}
    if b"IHDR" not in kinds:
        raise UnsupportedImage("PNG has no header chunk")

    header = next(body for kind, body in chunks if kind == b"IHDR")
    width, height, depth, color_type, interlace = _header(header)
    if width <= 0 or height <= 0:
        raise UnsupportedImage("PNG has no pixels")
    if width > MAX_DIMENSION or height > MAX_DIMENSION:
        raise UnsupportedImage("Image exceeds the maximum icon dimensions")

    if b"CgBI" not in kinds:
        return NormalizedPng(data, width, height)

    if (depth, color_type) != _RGBA8 or interlace not in (0, 1):
        raise UnsupportedImage("Unsupported CgBI pixel format")
    if width > MAX_CGBI_DIMENSION or height > MAX_CGBI_DIMENSION:
        raise UnsupportedImage("CgBI image exceeds the size this decoder handles")

    idat = b"".join(body for kind, body in chunks if kind == b"IDAT")
    if not idat:
        raise UnsupportedImage("CgBI PNG has no image data")
    try:
        raw = zlib.decompress(idat, -zlib.MAX_WBITS)
    except zlib.error as exc:
        raise UnsupportedImage("CgBI image data could not be decompressed") from exc

    pixels = _undo_cgbi(raw, width, height, interlace)
    return NormalizedPng(_build_png(pixels, width, height), width, height)


def _undo_cgbi(raw: bytes, width: int, height: int, interlace: int) -> bytes:
    if interlace == 0:
        rows, _ = _unfilter_pass(raw, 0, width, height)
        return b"".join(b"\x00" + row for row in rows)

    stride = width * 4
    image = bytearray(stride * height)
    position = 0
    for x_start, y_start, x_step, y_step in ADAM7_PASSES:
        pass_width = -(-(width - x_start) // x_step)
        pass_height = -(-(height - y_start) // y_step)
        if pass_width <= 0 or pass_height <= 0:
            continue
        rows, position = _unfilter_pass(raw, position, pass_width, pass_height)
        for row_index, row in enumerate(rows):
            y = y_start + row_index * y_step
            for column in range(pass_width):
                offset = (y * width + x_start + column * x_step) * 4
                image[offset : offset + 4] = row[column * 4 : column * 4 + 4]
    return b"".join(b"\x00" + bytes(image[y * stride : (y + 1) * stride]) for y in range(height))


def _unfilter_pass(raw: bytes, position: int, width: int, height: int) -> tuple[list[bytearray], int]:
    """One Adam7 pass (or the whole image), unfiltered and converted to straight RGBA."""
    stride = width * 4
    if len(raw) - position < (stride + 1) * height:
        raise UnsupportedImage("CgBI image data is shorter than its header claims")

    rows: list[bytearray] = []
    previous = bytearray(stride)
    for _ in range(height):
        filter_type = raw[position]
        position += 1
        line = bytearray(raw[position : position + stride])
        position += stride
        _unfilter(line, previous, filter_type, stride)
        previous = bytearray(line)
        _to_straight_rgba(line, stride)
        rows.append(line)
    return rows, position


def _to_straight_rgba(line: bytearray, stride: int) -> None:
    for index in range(0, stride, 4):
        blue, green, red, alpha = line[index], line[index + 1], line[index + 2], line[index + 3]
        if alpha:
            red = min(255, red * 255 // alpha)
            green = min(255, green * 255 // alpha)
            blue = min(255, blue * 255 // alpha)
        line[index : index + 4] = bytes((red, green, blue, alpha))


def _unfilter(line: bytearray, previous: bytearray, filter_type: int, stride: int) -> None:
    if filter_type == 0:
        return
    if filter_type not in (1, 2, 3, 4):
        raise UnsupportedImage("Unsupported PNG filter type")
    for index in range(stride):
        left = line[index - 4] if index >= 4 else 0
        up = previous[index]
        up_left = previous[index - 4] if index >= 4 else 0
        if filter_type == 1:
            line[index] = (line[index] + left) & 0xFF
        elif filter_type == 2:
            line[index] = (line[index] + up) & 0xFF
        elif filter_type == 3:
            line[index] = (line[index] + ((left + up) >> 1)) & 0xFF
        else:
            estimate = left + up - up_left
            distances = (abs(estimate - left), abs(estimate - up), abs(estimate - up_left))
            nearest = left if distances[0] <= distances[1] and distances[0] <= distances[2] else (
                up if distances[1] <= distances[2] else up_left
            )
            line[index] = (line[index] + nearest) & 0xFF


def _chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)


def _build_png(pixels: bytes, width: int, height: int) -> bytes:
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        PNG_SIGNATURE
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(pixels, 9))
        + _chunk(b"IEND", b"")
    )
