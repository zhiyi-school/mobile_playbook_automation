"""
iOS preflight checks: device connection and Appium errors, plus per-risk readiness warnings.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mobile_playbook.logging_setup import safe_url
from mobile_playbook.orchestration.appium_process import tcp_reachable as _tcp_reachable
from mobile_playbook.platforms.ios.burp_health import (
    HEALTH_SCHEMA_VERSION,
    canonical_capture_path,
    device_udid_hash,
    health_record_path,
    normalize_proxy_url,
    read_health_record,
)
from mobile_playbook.platforms.ios.config import effective_risk_config
from mobile_playbook.platforms.ios.models import AppConfig
from mobile_playbook.platforms.ios.mutations.repackage import resolve_insert_dylib
from mobile_playbook.platforms.ios.screen_capture_ocr import ocr_available
from mobile_playbook.storage import ios_capture_path, resolve_under_repository

logger = logging.getLogger(__name__)

DEVICES_SECTION = "Devices"
TRAFFIC_INTERCEPTION_RISK_ID = "ios-feature-02-risk-01"
REPACKAGING_RISK_ID = "ios-feature-01-risk-02"
SCREEN_CAPTURE_RISK_ID = "ios-feature-03-risk-01"
DEFAULT_HEALTH_MAX_AGE_SECONDS = 300


@dataclass(frozen=True)
class IosPreflightWarning:
    code: str
    message: str
    risk_id: str
    app_ids: tuple[str, ...]


@dataclass(frozen=True)
class IosPreflightResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[IosPreflightWarning] = field(default_factory=list)


# Check required device fields, device connection and Appium reachability, collecting errors.
def check_ios_preflight(config) -> IosPreflightResult:
    errors: list[str] = []
    logger.debug(
        "ios preflight: checking device udid=%s team_id=%s appium=%s",
        getattr(config.device, "udid", ""),
        getattr(config.device, "team_id", ""),
        getattr(config.device, "appium_server_url", ""),
    )
    if not getattr(config.device, "udid", ""):
        errors.append("device.udid is required")
    if not getattr(config.device, "team_id", ""):
        errors.append("device.team_id is required")
    if not getattr(config.device, "appium_server_url", ""):
        errors.append("device.appium_server_url is required")
    logger.debug("ios preflight: required device fields check found %s error(s)", len(errors))
    if not errors:
        _check_device_connected(config.device.udid, errors)
    else:
        logger.debug("ios preflight: skipping device connection check because required fields are missing")
    if not errors:
        _check_appium_reachable(config.device.appium_server_url, errors)
    else:
        logger.debug("ios preflight: skipping Appium reachability check because of earlier errors")
    logger.debug("ios preflight: device preflight ok=%s errors=%s", not errors, errors)
    return IosPreflightResult(ok=not errors, errors=errors)


# Warn about Burp proxy, capture file and health record problems for planned interception tests.
def check_traffic_interception_preflight(
    config,
    planned_tests: list[tuple[AppConfig, str]],
) -> list[IosPreflightWarning]:
    grouped: dict[tuple[Any, ...], dict[str, Any]] = {}
    for app, risk_id in planned_tests:
        if risk_id != TRAFFIC_INTERCEPTION_RISK_ID:
            continue
        app_risk_config = (getattr(app, "risks", {}) or {}).get(risk_id) or {}
        if not app_risk_config.get("enabled", False):
            logger.debug("ios preflight: traffic interception not enabled for %s, skipping", getattr(app, "id", None))
            continue
        effective = effective_risk_config(config, risk_id, app_risk_config)
        burp = effective.get("burp") or {}
        configured_path = burp.get("capture_path")
        capture_path = resolve_under_repository(configured_path) if configured_path else ios_capture_path()
        proxy_url = str(burp.get("proxy_url") or "")
        expected_hosts = tuple(str(host) for host in (effective.get("expected_hosts") or []))
        max_age = float(burp.get("health_max_age_seconds", DEFAULT_HEALTH_MAX_AGE_SECONDS))
        key = (
            normalize_proxy_url(proxy_url),
            canonical_capture_path(capture_path),
            max_age,
            expected_hosts,
        )
        group = grouped.setdefault(
            key,
            {
                "proxy_url": proxy_url,
                "capture_path": capture_path,
                "expected_hosts": expected_hosts,
                "max_age": max_age,
                "app_ids": set(),
            },
        )
        group["app_ids"].add(str(app.id))
        logger.debug(
            "ios preflight: traffic interception for %s uses proxy=%s capture=%s max_age=%ss expected_hosts=%s",
            app.id,
            proxy_url,
            capture_path,
            max_age,
            expected_hosts,
        )

    logger.debug("ios preflight: traffic interception checks grouped into %s configuration(s)", len(grouped))
    warnings: list[IosPreflightWarning] = []
    for group in grouped.values():
        warnings.extend(_traffic_interception_warnings(config, group))
    logger.debug("ios preflight: traffic interception warnings %s", [warning.code for warning in warnings])
    return warnings


# Warn when insert_dylib or codesign is unavailable for planned repackaging tests.
def check_repackaging_preflight(
    config,
    planned_tests: list[tuple[AppConfig, str]],
) -> list[IosPreflightWarning]:
    app_ids: list[str] = []
    for app, risk_id in planned_tests:
        if risk_id != REPACKAGING_RISK_ID:
            continue
        if not ((getattr(app, "risks", {}) or {}).get(risk_id) or {}).get("enabled", False):
            logger.debug("ios preflight: repackaging not enabled for %s, skipping", getattr(app, "id", None))
            continue
        app_ids.append(str(app.id))
    if not app_ids:
        logger.debug("ios preflight: no apps plan repackaging, no repackaging checks")
        return []
    ids = tuple(sorted(set(app_ids)))
    effective = effective_risk_config(config, REPACKAGING_RISK_ID, {})
    insert_dylib = resolve_insert_dylib(effective.get("insert_dylib_path"))
    warnings: list[IosPreflightWarning] = []
    insert_dylib_found = (
        Path(insert_dylib).exists() if os.sep in insert_dylib else shutil.which(insert_dylib) is not None
    )
    logger.debug(
        "ios preflight: repackaging for %s insert_dylib=%s (configured %s) found=%s",
        ids,
        insert_dylib,
        effective.get("insert_dylib_path"),
        insert_dylib_found,
    )
    if not insert_dylib_found:
        warnings.append(
            IosPreflightWarning(
                "INSERT_DYLIB_UNAVAILABLE",
                f"insert_dylib could not be found ('{insert_dylib}'); vendor it under tools/insert_dylib/ "
                "or set repackaging.insert_dylib_path. Repackaging reports DYLIB_INJECTION_FAILED without it.",
                REPACKAGING_RISK_ID,
                ids,
            )
        )
    codesign_path = shutil.which("codesign")
    logger.debug("ios preflight: codesign on PATH at %s", codesign_path)
    if codesign_path is None:
        warnings.append(
            IosPreflightWarning(
                "CODESIGN_UNAVAILABLE",
                "'codesign' is not on PATH; repackaging cannot re-sign the build and reports RESIGN_FAILED.",
                REPACKAGING_RISK_ID,
                ids,
            )
        )
    logger.debug("ios preflight: repackaging warnings %s", [warning.code for warning in warnings])
    return warnings


# Warn when the recorder IPA is missing or Vision OCR is unavailable for planned capture tests.
def check_screen_capture_preflight(
    config,
    planned_tests: list[tuple[AppConfig, str]],
) -> list[IosPreflightWarning]:
    missing_ipas: dict[str, set[str]] = {}
    ocr_app_ids: set[str] = set()
    for app, risk_id in planned_tests:
        if risk_id != SCREEN_CAPTURE_RISK_ID:
            continue
        app_risk_config = (getattr(app, "risks", {}) or {}).get(risk_id) or {}
        if not app_risk_config.get("enabled", False):
            logger.debug("ios preflight: screen capture not enabled for %s, skipping", getattr(app, "id", None))
            continue
        effective = effective_risk_config(config, risk_id, app_risk_config)
        recorder_ipa = (effective.get("recorder_app") or {}).get("ipa")
        if recorder_ipa and not Path(str(recorder_ipa)).expanduser().exists():
            logger.debug("ios preflight: recorder IPA %s missing for %s", recorder_ipa, app.id)
            missing_ipas.setdefault(str(recorder_ipa), set()).add(str(app.id))
        else:
            logger.debug("ios preflight: recorder IPA for %s is %s", app.id, recorder_ipa or "not configured")
        if (effective.get("capture") or {}).get("ocr_provider", "vision") != "none":
            ocr_app_ids.add(str(app.id))

    warnings = [
        IosPreflightWarning(
            "RECORDER_IPA_MISSING",
            f"The screen recorder IPA does not exist ('{ipa}'); build ReplayConsentRecorder.ipa and set "
            "screen_capture.recorder_app.ipa. The screen capture risk cannot install the recorder without it.",
            SCREEN_CAPTURE_RISK_ID,
            tuple(sorted(app_ids)),
        )
        for ipa, app_ids in sorted(missing_ipas.items())
    ]
    logger.debug("ios preflight: screen capture apps needing OCR %s", sorted(ocr_app_ids))
    if ocr_app_ids and not ocr_available():
        logger.debug("ios preflight: Vision OCR bindings unavailable")
        warnings.append(
            IosPreflightWarning(
                "OCR_UNAVAILABLE",
                "The Vision OCR bindings (pyobjc-framework-Vision) are not installed; the screen recording is still "
                "collected, but the result is OCR_UNAVAILABLE and needs manual review.",
                SCREEN_CAPTURE_RISK_ID,
                tuple(sorted(ocr_app_ids)),
            )
        )
    logger.debug("ios preflight: screen capture warnings %s", [warning.code for warning in warnings])
    return warnings


# Return the proxy, capture file and health record warnings for one interception configuration.
def _traffic_interception_warnings(config, group: dict[str, Any]) -> list[IosPreflightWarning]:
    proxy_url = group["proxy_url"]
    capture_path: Path = group["capture_path"]
    max_age: float = group["max_age"]
    app_ids = tuple(sorted(group["app_ids"]))
    warnings: list[IosPreflightWarning] = []

    # Record a warning for this configuration's apps.
    def warn(code: str, message: str) -> None:
        logger.debug("ios preflight: traffic interception warning %s for %s", code, app_ids)
        warnings.append(IosPreflightWarning(code, message, TRAFFIC_INTERCEPTION_RISK_ID, app_ids))

    proxy_reachable = _tcp_reachable(proxy_url, timeout=2)
    logger.debug("ios preflight: Burp proxy %s reachable=%s", safe_url(proxy_url), proxy_reachable)
    if not proxy_reachable:
        warn("BURP_PROXY_UNREACHABLE", "The configured Burp proxy is unreachable; start it and verify its listener.")

    capture_is_file = False
    if capture_path.is_dir():
        warn("BURP_CAPTURE_PATH_IS_DIRECTORY", "The configured Burp capture location is a directory, not a JSONL file.")
    elif capture_path.exists():
        capture_is_file = True
        if not _capture_readable(capture_path):
            warn("BURP_CAPTURE_UNREADABLE", "The Burp capture file cannot be read by the automation process.")
    elif not capture_path.parent.exists():
        warn("BURP_CAPTURE_PARENT_MISSING", "The parent directory for the Burp capture file does not exist.")
    elif not _path_writable(capture_path.parent):
        warn("BURP_CAPTURE_PARENT_UNWRITABLE", "The parent directory for the Burp capture file is not writable.")

    if not group["expected_hosts"]:
        warn(
            "BURP_EXPECTED_HOSTS_EMPTY",
            "No expected hosts are configured, so unrelated device traffic can be attributed to the app.",
        )

    record_path = health_record_path(capture_path)
    record = read_health_record(capture_path)
    logger.debug(
        "ios preflight: capture %s is_file=%s; health record %s present=%s",
        capture_path,
        capture_is_file,
        record_path,
        record is not None,
    )
    if record is not None:
        logger.debug(
            "ios preflight: health record schema=%s proxy=%s capture=%s valid_entries=%s verified_at=%s",
            record.get("schema_version"),
            record.get("proxy_url"),
            record.get("capture_path"),
            record.get("valid_entry_count"),
            record.get("verified_at"),
        )
    if record is None:
        code = "BURP_HEALTH_MISSING" if not record_path.exists() else "BURP_HEALTH_MISMATCH"
        message = (
            "No Burp canary health record exists; run the interception check before the scan."
            if code == "BURP_HEALTH_MISSING"
            else "The Burp canary health record is invalid; rerun the interception check."
        )
        warn(code, message)
    elif not _health_matches(record, config, capture_path, proxy_url):
        warn(
            "BURP_HEALTH_MISMATCH",
            "The Burp canary health record does not match the configured proxy, capture file, or device; rerun the interception check.",
        )
    elif _record_age_seconds(record) > max_age:
        warn("BURP_HEALTH_STALE", "The Burp canary health record is stale; rerun the interception check.")

    if capture_is_file and _path_age_seconds(capture_path) > max_age:
        warn("BURP_CAPTURE_STALE", "The Burp capture file has not been updated recently; verify the extension and capture path.")
    return warnings


# Return whether a health record matches the proxy, capture file and device and saw valid entries.
def _health_matches(record: dict[str, Any], config, capture_path: Path, proxy_url: str) -> bool:
    return (
        record.get("schema_version") == HEALTH_SCHEMA_VERSION
        and record.get("proxy_url") == normalize_proxy_url(proxy_url)
        and record.get("capture_path") == canonical_capture_path(capture_path)
        and record.get("device_udid_sha256") == device_udid_hash(config.device.udid)
        and isinstance(record.get("valid_entry_count"), int)
        and record["valid_entry_count"] > 0
        and _verified_at(record) is not None
    )


# Return the record's timezone-aware verified_at in UTC, or None when missing or invalid.
def _verified_at(record: dict[str, Any]) -> datetime | None:
    value = record.get("verified_at")
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        logger.debug("ios preflight: health record verified_at %r is not ISO-8601: %s", value, exc, exc_info=True)
        return None
    if parsed.tzinfo is None:
        logger.debug("ios preflight: health record verified_at %r has no timezone", value)
        return None
    return parsed.astimezone(timezone.utc)


# Return the health record's age in seconds, infinite when unverifiable.
def _record_age_seconds(record: dict[str, Any]) -> float:
    verified_at = _verified_at(record)
    if verified_at is None:
        return float("inf")
    age = max(0.0, (datetime.now(timezone.utc) - verified_at).total_seconds())
    logger.debug("ios preflight: health record age %.1fs", age)
    return age


# Return seconds since a file was modified, infinite when it cannot be read.
def _path_age_seconds(path: Path) -> float:
    try:
        modified_at = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    except OSError as exc:
        logger.debug("ios preflight: could not stat %s: %s", path, exc, exc_info=True)
        return float("inf")
    age = max(0.0, (datetime.now(timezone.utc) - modified_at).total_seconds())
    logger.debug("ios preflight: %s last modified %.1fs ago", path, age)
    return age


# Return whether the capture file can be opened for reading.
def _capture_readable(path: Path) -> bool:
    try:
        with path.open("rb"):
            return True
    except OSError as exc:
        logger.debug("ios preflight: capture %s not readable: %s", path, exc, exc_info=True)
        return False


# Return whether the process can write to a path.
def _path_writable(path: Path) -> bool:
    return os.access(path, os.W_OK)


# Record an error when the Appium server is not reachable.
def _check_appium_reachable(appium_server_url: str, errors: list[str]) -> None:
    reachable = _tcp_reachable(appium_server_url)
    logger.debug("ios preflight: Appium %s reachable=%s", appium_server_url, reachable)
    if not reachable:
        errors.append(f"appium: Appium server not reachable at {appium_server_url}. Start it with 'appium'.")


# Record an error when the configured UDID is absent from a non-empty connected-device list.
def _check_device_connected(udid: str, errors: list[str]) -> None:
    connected = connected_device_udids()
    logger.debug(
        "ios preflight: %s connected device(s) listed; configured udid %s present=%s",
        len(connected),
        udid,
        udid in connected,
    )
    if not connected:
        logger.debug("ios preflight: no devices listed, skipping connection check")
    # An empty list means xctrace is unusable, since the Mac itself always appears; skip the check.
    if connected and udid not in connected:
        errors.append(
            f"device.udid '{udid}' is not a connected iOS device. "
            "Run 'xcrun xctrace list devices' to see connected devices — "
            "reconnect/unlock/trust the configured device, or update device.udid to match."
        )


# Return the UDIDs xctrace lists as connected, or an empty set when it cannot run.
def connected_device_udids(timeout: float = 15.0) -> set[str]:
    argv = ["xcrun", "xctrace", "list", "devices"]
    logger.debug("ios preflight: running %s (timeout %ss)", argv, timeout)
    started = time.monotonic()
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.debug("ios preflight: %s failed after %.2fs: %s", argv, time.monotonic() - started, exc, exc_info=True)
        return set()
    logger.debug("ios preflight: %s exited %s in %.2fs", argv, result.returncode, time.monotonic() - started)
    if result.returncode != 0:
        logger.debug("ios preflight: xctrace stderr head %.200r", getattr(result, "stderr", None))
        return set()
    udids = _parse_connected_udids(result.stdout)
    logger.debug("ios preflight: parsed %s device udid(s) from xctrace output", len(udids))
    return udids


# Parse the UDIDs from the Devices section of xctrace output.
def _parse_connected_udids(output: str) -> set[str]:
    udids: set[str] = set()
    in_devices_section = False
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("=="):
            in_devices_section = stripped.strip("= ").strip() == DEVICES_SECTION
            continue
        if not in_devices_section or not stripped:
            continue
        match = re.search(r"\(([0-9A-Fa-f-]+)\)\s*$", stripped)
        if match:
            udids.add(match.group(1))
    return udids
