"""
Arms the Burp extension's content capture for one test window and reads back the exchanges it recorded in full.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import tempfile
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# Names the extension shares; tools/burp/traffic_capture_extension.py defines the same values.
CONTENT_FILE_NAME = "content.jsonl"
MARKER_FILE_NAME = "content.armed"
MARKER_SCHEMA_VERSION = 1
CONTENT_SCHEMA_VERSION = 1
DEFAULT_MAX_BODY_BYTES = 65536
DEFAULT_TTL_SECONDS = 900.0


class ContentCaptureBusy(RuntimeError):
    """Another unexpired content capture window is armed in the same directory."""


@dataclass(frozen=True)
class ContentWindow:
    """An armed content capture window: where the extension writes and which listener ports it records."""

    directory: Path
    listener_ports: tuple[int, ...]

    # Return the file the extension appends this window's exchanges to.
    @property
    def content_path(self) -> Path:
        return self.directory / CONTENT_FILE_NAME

    # Return the marker whose presence arms the extension.
    @property
    def marker_path(self) -> Path:
        return self.directory / MARKER_FILE_NAME


@dataclass(frozen=True)
class InterceptedExchange:
    """One proxied request and response as the extension recorded it, with bodies decoded where their encoding allows."""

    listener: str
    client_ip: str
    scheme: str
    host: str
    port: int | None
    method: str
    url: str
    status_code: int | None
    request_headers: list[str]
    request_body: bytes
    request_body_length: int
    request_body_truncated: bool
    response_headers: list[str]
    response_body: bytes
    response_body_length: int
    response_body_truncated: bool
    response_body_encoding: str


@dataclass
class ContentRead:
    """The exchanges read from a content file and how many of its lines could not be used."""

    exchanges: list[InterceptedExchange] = field(default_factory=list)
    malformed_count: int = 0
    other_listener_count: int = 0
    trailing_partial_line: bool = False


# Arm content capture for the given listener ports: discard any earlier content file, then publish the marker the
# extension checks on every proxied response. Raise ContentCaptureBusy while another unexpired window is armed.
def arm_content_capture(
    directory: Path,
    listener_ports: list[int],
    *,
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
    ttl_seconds: float = DEFAULT_TTL_SECONDS,
) -> ContentWindow:
    ports = tuple(sorted({int(port) for port in listener_ports}))
    if not ports:
        raise ValueError("content capture needs at least one Burp listener port")
    window = ContentWindow(directory.expanduser().resolve(strict=False), ports)
    window.directory.mkdir(parents=True, exist_ok=True)
    marker = {
        "schema_version": MARKER_SCHEMA_VERSION,
        "listener_ports": list(ports),
        "max_body_bytes": int(max_body_bytes),
        # The expiry stops a run that dies without disarming from leaving content capture on indefinitely.
        "expires_at": time.time() + ttl_seconds,
    }
    with tempfile.NamedTemporaryFile("w", dir=window.directory, prefix=f".{MARKER_FILE_NAME}.", suffix=".tmp", delete=False) as handle:
        temporary = Path(handle.name)
        json.dump(marker, handle)
    try:
        for attempt in range(2):
            try:
                # Linking fails when the marker exists, so two runs cannot both claim the window.
                os.link(temporary, window.marker_path)
                break
            except FileExistsError:
                if attempt or _marker_live(window.marker_path):
                    raise ContentCaptureBusy(f"content capture is already armed at {window.marker_path}") from None
                logger.debug("burp content: replacing expired or unreadable marker %s", window.marker_path)
                window.marker_path.unlink(missing_ok=True)
    finally:
        temporary.unlink(missing_ok=True)
    # Removed only once this window owns the marker, so a busy window never deletes another run's content.
    window.content_path.unlink(missing_ok=True)
    logger.debug("burp content: armed %s for listener ports %s", window.marker_path, list(ports))
    return window


# Stop the extension recording content for the window by removing its marker.
def disarm_content_capture(window: ContentWindow) -> None:
    window.marker_path.unlink(missing_ok=True)
    logger.debug("burp content: disarmed %s", window.marker_path)


# Delete the window's content file, which holds raw request and response bodies.
def discard_content(window: ContentWindow) -> None:
    window.content_path.unlink(missing_ok=True)


# Read every complete record in the window's content file, keeping exchanges from its listener ports.
def read_content(window: ContentWindow) -> ContentRead:
    result = ContentRead()
    try:
        payload = window.content_path.read_bytes()
    except FileNotFoundError:
        logger.debug("burp content: no content file at %s", window.content_path)
        return result
    records = payload.split(b"\n")
    result.trailing_partial_line = bool(records.pop())
    for record in records:
        exchange = _parse_exchange(record)
        if exchange is None:
            result.malformed_count += 1
        elif _listener_port(exchange.listener) not in window.listener_ports:
            result.other_listener_count += 1
        else:
            result.exchanges.append(exchange)
    logger.debug(
        "burp content: read %s exchange(s), %s malformed, %s from other listeners, partial=%s",
        len(result.exchanges), result.malformed_count, result.other_listener_count, result.trailing_partial_line,
    )
    return result


# Return whether the marker is readable and unexpired.
def _marker_live(marker_path: Path) -> bool:
    try:
        marker = json.loads(marker_path.read_text())
        return float(marker["expires_at"]) > time.time()
    except (OSError, ValueError, KeyError, TypeError):
        return False


# Return the port of a listener interface such as "0.0.0.0:8081", or None when it has none.
def _listener_port(listener: str) -> int | None:
    try:
        return int(listener.rsplit(":", 1)[1])
    except (IndexError, ValueError):
        return None


# Parse one content record into an exchange, or return None when it is not a valid record.
def _parse_exchange(record: bytes) -> InterceptedExchange | None:
    try:
        entry = json.loads(record.rstrip(b"\r").decode("utf-8"))
        if not isinstance(entry, dict) or entry.get("schema_version") != CONTENT_SCHEMA_VERSION:
            return None
        request_body = base64.b64decode(entry.get("request_body_base64") or "", validate=True)
        raw_response_body = base64.b64decode(entry.get("response_body_base64") or "", validate=True)
        response_headers = [str(header) for header in entry.get("response_headers") or []]
        response_truncated = bool(entry.get("response_body_truncated"))
        response_body, encoding = _decode_body(raw_response_body, response_headers, response_truncated)
        port = entry.get("port")
        status = entry.get("status_code")
        return InterceptedExchange(
            listener=str(entry.get("listener") or ""),
            client_ip=str(entry.get("client_ip") or ""),
            scheme=str(entry.get("scheme") or "").lower(),
            host=str(entry.get("host") or "").lower().rstrip("."),
            port=int(port) if port is not None else None,
            method=str(entry.get("method") or ""),
            url=str(entry.get("url") or ""),
            status_code=int(status) if status is not None else None,
            request_headers=[str(header) for header in entry.get("request_headers") or []],
            request_body=request_body,
            request_body_length=int(entry.get("request_body_length") or 0),
            request_body_truncated=bool(entry.get("request_body_truncated")),
            response_headers=response_headers,
            response_body=response_body,
            response_body_length=int(entry.get("response_body_length") or 0),
            response_body_truncated=response_truncated,
            response_body_encoding=encoding,
        )
    except (UnicodeDecodeError, ValueError, TypeError, binascii.Error) as exc:
        logger.debug("burp content: malformed record (%s bytes): %s", len(record), exc)
        return None


# Undo a gzip or deflate Content-Encoding, returning the body and "identity", "gzip", "deflate" or, when it cannot be
# undone, "undecoded:<encoding>" with the body as recorded.
def _decode_body(body: bytes, headers: list[str], truncated: bool) -> tuple[bytes, str]:
    encoding = ""
    for header in headers[1:]:
        name, _, value = header.partition(":")
        if name.strip().lower() == "content-encoding":
            encoding = value.strip().lower()
    if not body or encoding in {"", "identity"}:
        return body, "identity"
    if encoding in {"gzip", "x-gzip", "deflate"}:
        # A cut body decompresses as far as it goes; zlib only raises when the stream itself is corrupt.
        attempts = [zlib.MAX_WBITS | 16] if encoding != "deflate" else [zlib.MAX_WBITS, -zlib.MAX_WBITS]
        for wbits in attempts:
            try:
                decompressor = zlib.decompressobj(wbits)
                decoded = decompressor.decompress(body)
                if not truncated:
                    decoded += decompressor.flush()
                return decoded, "deflate" if encoding == "deflate" else "gzip"
            except zlib.error as exc:
                logger.debug("burp content: %s body did not decode with wbits %s: %s", encoding, wbits, exc)
    return body, f"undecoded:{encoding}"
