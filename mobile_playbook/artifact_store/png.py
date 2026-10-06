"""
PNG validation, conversion of Apple CgBI icons into standard PNGs, and composition of Android adaptive icon layers.
"""

from __future__ import annotations

import logging
import struct
import zlib
from dataclasses import dataclass

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
logger = logging.getLogger(__name__)

MAX_SOURCE_BYTES = 4 * 1024 * 1024
MAX_DIMENSION = 2048
MAX_CGBI_DIMENSION = 512
MAX_DECODE_DIMENSION = 1024
# An adaptive icon's layers are 108dp square, and launchers show at most the central 72dp.
ADAPTIVE_VISIBLE_FRACTION = (72, 108)

_RGBA8 = (8, 6)
# Bytes per pixel of each 8-bit colour type: greyscale, RGB, palette, greyscale with alpha, RGBA.
_BYTES_PER_PIXEL = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}

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
    """Raised when image data is not a PNG this module accepts."""
    pass


@dataclass(frozen=True)
class NormalizedPng:
    data: bytes
    width: int
    height: int


# Reports whether data starts with the PNG signature.
def looks_like_png(data: bytes) -> bool:
    return data[:8] == PNG_SIGNATURE


# Yields each PNG chunk's type and body up to IEND, raising on truncated data.
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


# Unpacks width, height, bit depth, color type and interlace from an IHDR body.
def _header(body: bytes) -> tuple[int, int, int, int, int]:
    if len(body) < 13:
        raise UnsupportedImage("Truncated PNG header")
    width, height, depth, color_type, _compression, _filter, interlace = struct.unpack(">IIBBBBB", body[:13])
    return width, height, depth, color_type, interlace


# Validates a PNG and converts Apple CgBI PNGs to standard ones. See docs/api.md#application-icons.
def normalize(data: bytes) -> NormalizedPng:
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
    logger.debug(
        "artifact store: PNG %dx%d depth=%d color=%d interlace=%d cgbi=%s (%d bytes).",
        width,
        height,
        depth,
        color_type,
        interlace,
        b"CgBI" in kinds,
        len(data),
    )
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
    logger.debug("artifact store: converted CgBI PNG %dx%d to standard RGBA.", width, height)
    return NormalizedPng(_build_png(pixels, width, height), width, height)


# Rebuilds CgBI pixel data as unfiltered, filter-type-0 scanlines, de-interlacing Adam7 passes.
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


# Unfilters one Adam7 pass (or the whole image) and converts it to straight RGBA.
def _unfilter_pass(raw: bytes, position: int, width: int, height: int) -> tuple[list[bytearray], int]:
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


# Converts a row of premultiplied BGRA pixels to straight RGBA in place.
def _to_straight_rgba(line: bytearray, stride: int) -> None:
    for index in range(0, stride, 4):
        blue, green, red, alpha = line[index], line[index + 1], line[index + 2], line[index + 3]
        if alpha:
            red = min(255, red * 255 // alpha)
            green = min(255, green * 255 // alpha)
            blue = min(255, blue * 255 // alpha)
        line[index : index + 4] = bytes((red, green, blue, alpha))


# Reverses a PNG scanline filter in place using the previous row, for pixels of `bpp` bytes.
def _unfilter(line: bytearray, previous: bytearray, filter_type: int, stride: int, bpp: int = 4) -> None:
    if filter_type == 0:
        return
    if filter_type not in (1, 2, 3, 4):
        raise UnsupportedImage("Unsupported PNG filter type")
    for index in range(stride):
        left = line[index - bpp] if index >= bpp else 0
        up = previous[index]
        up_left = previous[index - bpp] if index >= bpp else 0
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


# Encodes a PNG chunk with its length and CRC.
def _chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)


# Builds an 8-bit RGBA, non-interlaced PNG from filtered scanlines.
def _build_png(pixels: bytes, width: int, height: int) -> bytes:
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        PNG_SIGNATURE
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(pixels, 9))
        + _chunk(b"IEND", b"")
    )


# Decodes an 8-bit, non-interlaced PNG into straight RGBA bytes, raising UnsupportedImage for any other format.
def decode_rgba(data: bytes) -> tuple[bytes, int, int]:
    if len(data) > MAX_SOURCE_BYTES or not looks_like_png(data):
        raise UnsupportedImage("Not a PNG image within the icon size limit")
    chunks = list(_iter_chunks(data))
    header = next((body for kind, body in chunks if kind == b"IHDR"), None)
    if header is None or any(kind == b"CgBI" for kind, _ in chunks):
        raise UnsupportedImage("Not a standard PNG")
    width, height, depth, color_type, interlace = _header(header)
    bpp = _BYTES_PER_PIXEL.get(color_type)
    if depth != 8 or bpp is None or interlace != 0:
        raise UnsupportedImage("Only 8-bit, non-interlaced PNGs are decoded")
    if not 0 < width <= MAX_DECODE_DIMENSION or not 0 < height <= MAX_DECODE_DIMENSION:
        raise UnsupportedImage("Image is outside the dimensions this decoder handles")
    palette = next((body for kind, body in chunks if kind == b"PLTE"), b"")
    transparency = next((body for kind, body in chunks if kind == b"tRNS"), b"")
    try:
        raw = zlib.decompress(b"".join(body for kind, body in chunks if kind == b"IDAT"))
    except zlib.error as exc:
        raise UnsupportedImage("PNG image data could not be decompressed") from exc
    stride = width * bpp
    if len(raw) < (stride + 1) * height:
        raise UnsupportedImage("PNG image data is shorter than its header claims")

    pixels = bytearray(width * height * 4)
    previous = bytearray(stride)
    for y in range(height):
        start = y * (stride + 1)
        line = bytearray(raw[start + 1 : start + 1 + stride])
        _unfilter(line, previous, raw[start], stride, bpp)
        previous = line
        for x in range(width):
            pixels[(y * width + x) * 4 : (y * width + x) * 4 + 4] = _rgba(
                line[x * bpp : x * bpp + bpp], color_type, palette, transparency
            )
    return bytes(pixels), width, height


# Converts one decoded pixel of a colour type to an RGBA tuple, applying any tRNS transparency.
def _rgba(pixel: bytearray, color_type: int, palette: bytes, transparency: bytes) -> bytes:
    if color_type == 6:
        return bytes(pixel)
    if color_type == 4:
        return bytes((pixel[0], pixel[0], pixel[0], pixel[1]))
    if color_type == 3:
        index = pixel[0]
        if index * 3 + 3 > len(palette):
            raise UnsupportedImage("PNG palette index out of range")
        alpha = transparency[index] if index < len(transparency) else 255
        return bytes((*palette[index * 3 : index * 3 + 3], alpha))
    if color_type == 2:
        transparent = len(transparency) == 6 and bytes(pixel) == transparency[1::2]
        return bytes((*pixel, 0 if transparent else 255))
    transparent = len(transparency) == 2 and pixel[0] == transparency[1]
    return bytes((pixel[0], pixel[0], pixel[0], 0 if transparent else 255))


# Scales RGBA pixels to a new size by nearest neighbour.
def _scale(pixels: bytes, width: int, height: int, new_width: int, new_height: int) -> bytes:
    if (width, height) == (new_width, new_height):
        return pixels
    scaled = bytearray(new_width * new_height * 4)
    for y in range(new_height):
        source_row = (y * height // new_height) * width
        for x in range(new_width):
            source = (source_row + x * width // new_width) * 4
            target = (y * new_width + x) * 4
            scaled[target : target + 4] = pixels[source : source + 4]
    return bytes(scaled)


# Builds an Android adaptive icon from its layers: the foreground over a PNG or solid-colour background,
# cropped to the central area a launcher shows.
def compose_adaptive_icon(
    foreground: bytes, background: bytes | None = None, background_color: tuple[int, int, int, int] | None = None
) -> NormalizedPng:
    front, width, height = decode_rgba(foreground)
    if background is not None:
        back_pixels, back_width, back_height = decode_rgba(background)
        back = _scale(back_pixels, back_width, back_height, width, height)
    elif background_color is not None:
        back = bytes(background_color) * (width * height)
    else:
        raise UnsupportedImage("An adaptive icon needs a background layer")

    composed = bytearray(width * height * 4)
    for offset in range(0, len(composed), 4):
        alpha = front[offset + 3]
        back_alpha = back[offset + 3]
        out_alpha = alpha + back_alpha * (255 - alpha) // 255
        for channel in range(3):
            if out_alpha:
                value = (front[offset + channel] * alpha + back[offset + channel] * back_alpha * (255 - alpha) // 255)
                composed[offset + channel] = min(255, value // out_alpha)
        composed[offset + 3] = out_alpha

    visible, full = ADAPTIVE_VISIBLE_FRACTION
    crop_width, crop_height = width * visible // full, height * visible // full
    left, top = (width - crop_width) // 2, (height - crop_height) // 2
    rows = [
        b"\x00" + bytes(composed[((top + y) * width + left) * 4 : ((top + y) * width + left + crop_width) * 4])
        for y in range(crop_height)
    ]
    return NormalizedPng(_build_png(b"".join(rows), crop_width, crop_height), crop_width, crop_height)


# Parses an Android colour value (#RGB, #ARGB, #RRGGBB or #AARRGGBB) into RGBA, or returns None.
def android_color(value: str) -> tuple[int, int, int, int] | None:
    digits = (value or "").strip().lstrip("#")
    if len(digits) in (3, 4):
        digits = "".join(character * 2 for character in digits)
    if len(digits) == 6:
        digits = "ff" + digits
    if len(digits) != 8:
        return None
    try:
        alpha, red, green, blue = (int(digits[index : index + 2], 16) for index in range(0, 8, 2))
    except ValueError:
        return None
    return red, green, blue, alpha
