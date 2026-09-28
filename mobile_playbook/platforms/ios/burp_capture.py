from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CaptureCursor:
    path: Path
    existed: bool
    device: int | None
    inode: int | None
    offset: int
    initial_size: int
    initial_mtime_ns: int | None
    pending: bytes = b""


@dataclass
class CaptureObservation:
    matched_entries: list[dict]
    new_line_count: int = 0
    valid_entry_count: int = 0
    malformed_entry_count: int = 0
    https_entry_count: int = 0
    non_https_entry_count: int = 0
    unmatched_entry_count: int = 0
    created_during_window: bool = False
    source_changed: bool = False
    source_unavailable: bool = False
    trailing_partial_line: bool = False


def _identity(stat_result) -> tuple[int | None, int | None]:
    device = getattr(stat_result, "st_dev", None)
    inode = getattr(stat_result, "st_ino", None)
    return (device or None, inode or None)


def snapshot_capture(path: Path) -> CaptureCursor:
    resolved = path.expanduser().resolve(strict=False)
    try:
        stat_result = resolved.stat()
    except FileNotFoundError as exc:
        logger.debug("ios burp capture: %s does not exist yet at snapshot: %s", resolved, exc, exc_info=True)
        return CaptureCursor(resolved, False, None, None, 0, 0, None)
    except OSError as exc:
        logger.debug("ios burp capture: cannot stat %s at snapshot: %s", resolved, exc, exc_info=True)
        return CaptureCursor(resolved, True, None, None, 0, 0, None)
    device, inode = _identity(stat_result)
    logger.debug(
        "ios burp capture: snapshot %s size=%s device=%s inode=%s", resolved, stat_result.st_size, device, inode
    )
    return CaptureCursor(
        resolved,
        True,
        device,
        inode,
        stat_result.st_size,
        stat_result.st_size,
        getattr(stat_result, "st_mtime_ns", None),
    )


def _normalize_host(value: object) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    parsed = urlsplit(raw if "://" in raw else f"//{raw}")
    return str(parsed.hostname or "").rstrip(".")


def _matches_expected_host(host: str, expected_hosts: list[str]) -> bool:
    if not expected_hosts:
        return True
    return bool(host) and any(
        expected and (host == expected or host.endswith(f".{expected}")) for expected in expected_hosts
    )


def _changed(cursor: CaptureCursor, device: int | None, inode: int | None) -> bool:
    if cursor.device is not None and device is not None and cursor.device != device:
        return True
    return cursor.inode is not None and inode is not None and cursor.inode != inode


def poll_capture(
    cursor: CaptureCursor,
    expected_hosts: list[str],
) -> tuple[CaptureCursor, CaptureObservation]:
    observation = CaptureObservation(matched_entries=[], trailing_partial_line=bool(cursor.pending))
    normalized_expected = [_normalize_host(value) for value in expected_hosts]

    if cursor.existed and cursor.device is None and cursor.inode is None and cursor.initial_mtime_ns is None:
        logger.debug("ios burp capture: %s was unreadable at snapshot; source unavailable", cursor.path)
        observation.source_unavailable = True
        return cursor, observation

    try:
        path_stat = cursor.path.stat()
    except FileNotFoundError as exc:
        logger.debug("ios burp capture: %s missing on poll (existed at snapshot=%s): %s", cursor.path, cursor.existed, exc, exc_info=True)
        observation.source_unavailable = cursor.existed
        return cursor, observation
    except OSError as exc:
        logger.debug("ios burp capture: cannot stat %s on poll: %s", cursor.path, exc, exc_info=True)
        observation.source_unavailable = True
        return cursor, observation

    device, inode = _identity(path_stat)
    if cursor.existed and _changed(cursor, device, inode):
        logger.debug(
            "ios burp capture: %s replaced (device %s->%s, inode %s->%s)", cursor.path, cursor.device, device, cursor.inode, inode
        )
        observation.source_changed = True
        return cursor, observation
    if path_stat.st_size < cursor.offset:
        logger.debug("ios burp capture: %s truncated (size %s < offset %s)", cursor.path, path_stat.st_size, cursor.offset)
        observation.source_changed = True
        return cursor, observation

    try:
        with cursor.path.open("rb") as handle:
            open_stat = os.fstat(handle.fileno())
            open_device, open_inode = _identity(open_stat)
            if _changed(cursor, open_device, open_inode):
                logger.debug("ios burp capture: opened %s differs from snapshot identity", cursor.path)
                observation.source_changed = True
                return cursor, observation
            if device is not None and open_device is not None and device != open_device:
                logger.debug("ios burp capture: %s device changed between stat and open", cursor.path)
                observation.source_changed = True
                return cursor, observation
            if inode is not None and open_inode is not None and inode != open_inode:
                logger.debug("ios burp capture: %s inode changed between stat and open", cursor.path)
                observation.source_changed = True
                return cursor, observation
            if open_stat.st_size < cursor.offset:
                logger.debug("ios burp capture: opened %s truncated (size %s < offset %s)", cursor.path, open_stat.st_size, cursor.offset)
                observation.source_changed = True
                return cursor, observation
            handle.seek(cursor.offset)
            appended = handle.read()
            offset = handle.tell()
    except OSError as exc:
        logger.debug("ios burp capture: reading %s failed: %s", cursor.path, exc, exc_info=True)
        observation.source_unavailable = True
        return cursor, observation

    payload = cursor.pending + appended
    records = payload.split(b"\n")
    pending = records.pop()
    observation.new_line_count = len(records)
    observation.created_during_window = not cursor.existed
    observation.trailing_partial_line = bool(pending)

    for record in records:
        try:
            decoded = record.rstrip(b"\r").decode("utf-8")
            entry = json.loads(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            logger.debug("ios burp capture: malformed record (%s bytes): %s", len(record), exc)
            observation.malformed_entry_count += 1
            continue
        if not isinstance(entry, dict):
            observation.malformed_entry_count += 1
            continue
        if entry.get("schema_version") != 1:
            observation.malformed_entry_count += 1
            continue
        scheme = str(entry.get("scheme") or "").strip().lower()
        if not scheme:
            observation.malformed_entry_count += 1
            continue
        observation.valid_entry_count += 1
        if scheme == "https":
            observation.https_entry_count += 1
        else:
            observation.non_https_entry_count += 1
        host = _normalize_host(entry.get("host"))
        if scheme == "https" and _matches_expected_host(host, normalized_expected):
            observation.matched_entries.append(entry)
        else:
            observation.unmatched_entry_count += 1

    logger.debug(
        "ios burp capture: poll %s read %s bytes offset %s->%s; lines=%s valid=%s malformed=%s https=%s non_https=%s "
        "matched=%s unmatched=%s partial=%s created_during_window=%s expected_hosts=%s",
        cursor.path,
        len(appended),
        cursor.offset,
        offset,
        observation.new_line_count,
        observation.valid_entry_count,
        observation.malformed_entry_count,
        observation.https_entry_count,
        observation.non_https_entry_count,
        len(observation.matched_entries),
        observation.unmatched_entry_count,
        observation.trailing_partial_line,
        observation.created_during_window,
        normalized_expected,
    )
    next_cursor = CaptureCursor(
        cursor.path,
        True,
        open_device,
        open_inode,
        offset,
        cursor.initial_size,
        cursor.initial_mtime_ns,
        pending,
    )
    return next_cursor, observation
