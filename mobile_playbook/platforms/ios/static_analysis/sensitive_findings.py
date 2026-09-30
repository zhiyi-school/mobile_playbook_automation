"""
Detects secrets in IPA resources and MobSF reports, masks them, and tests Google API key reuse.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections import Counter
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from mobile_playbook.platforms.ios.ipa.plist import read_plist

logger = logging.getLogger(__name__)


# Find secret patterns and sensitive-key values in a MobSF report, capped at 100 and deduped.
def extract_mobsf_sensitive_findings(report: dict[str, Any], reveal_values: bool) -> list[dict[str, Any]]:
    text = json.dumps(report, sort_keys=True, ensure_ascii=False)
    findings = classify_sensitive_string("mobsf_report_json", text, reveal_values)
    logger.debug("ios sensitive scan: mobsf report json is %s char(s); %s pattern finding(s)", len(text), len(findings))
    pattern_count = len(findings)
    for key, value in flatten_value(report):
        if len(findings) >= 100:
            break
        if isinstance(value, str) and key_looks_sensitive(key) and value_looks_secret(value):
            findings.append(
                sensitive_finding(
                    "mobsf_report_json",
                    "SENSITIVE_KEY_NAME",
                    value,
                    sensitive_key_severity(key),
                    key_path=key,
                    reveal_values=reveal_values,
                )
            )
            logger.debug("ios sensitive scan: SENSITIVE_KEY_NAME match at %s (value length %s)", key, len(value))
    deduped = dedupe_sensitive_findings(findings[:100])
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "ios sensitive scan: mobsf report yielded %s pattern + %s key-name finding(s), %s after dedupe, by type=%s",
            pattern_count,
            len(findings) - pattern_count,
            len(deduped),
            dict(Counter(item.get("match_type") for item in deduped)),
        )
    return deduped


# Scan inventoried plist and text resources for secrets, capped at 100 findings.
def scan_sensitive_information(app_dir: Path, inventory: dict[str, Any], reveal_values: bool) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    scanned: Counter[str] = Counter()
    logger.debug("ios sensitive scan: scanning inventoried files under %s", app_dir)
    for item in inventory.get("files", []):
        if len(findings) >= 100:
            logger.debug("ios sensitive scan: finding cap of 100 reached; stopping")
            break
        relative = Path(str(item.get("path", "")))
        if is_sensitive_scan_excluded(relative):
            scanned["excluded"] += 1
            continue
        path = app_dir / relative
        suffix = str(item.get("suffix", ""))
        if suffix in {".plist", ".xcprivacy"}:
            scanned["plist"] += 1
            findings.extend(scan_plist_for_sensitive_values(path, relative, reveal_values))
        elif suffix in {".json", ".xml", ".strings", ".txt", ".js", ".jsbundle", ".env", ".properties", ".yaml", ".yml", ".html"}:
            scanned["text"] += 1
            findings.extend(scan_text_for_sensitive_values(path, relative, reveal_values))
        else:
            scanned["other"] += 1
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("ios sensitive scan: files by kind=%s; %s finding(s), by type=%s", dict(scanned), len(findings[:100]), dict(Counter(item.get("match_type") for item in findings[:100])))
    return findings[:100]


# Scan a plist's string values for secret patterns and sensitive keys, capped at 25.
def scan_plist_for_sensitive_values(path: Path, relative: Path, reveal_values: bool) -> list[dict[str, Any]]:
    try:
        data = read_plist(path)
    except Exception as exc:
        logger.debug("ios sensitive scan: could not read plist %s: %s", relative, exc, exc_info=True)
        return []
    findings: list[dict[str, Any]] = []
    for key_path, value in flatten_value(data):
        if len(findings) >= 25:
            break
        if isinstance(value, str):
            classified = classify_sensitive_string(str(relative), value, reveal_values, key_path=key_path)
            findings.extend(classified)
            if not classified and key_looks_sensitive(key_path) and value_looks_secret(value):
                findings.append(
                    sensitive_finding(
                        str(relative),
                        "SENSITIVE_KEY_NAME",
                        value,
                        sensitive_key_severity(key_path),
                        key_path=key_path,
                        reveal_values=reveal_values,
                    )
                )
    deduped = dedupe_sensitive_findings(findings)
    if deduped and logger.isEnabledFor(logging.DEBUG):
        logger.debug("ios sensitive scan: plist %s: %s finding(s) at %s", relative, len(deduped), [(item["match_type"], item["key_path"]) for item in deduped])
    return deduped


# Scan a text file under 5 MiB for secret key-value pairs and patterns.
def scan_text_for_sensitive_values(path: Path, relative: Path, reveal_values: bool) -> list[dict[str, Any]]:
    max_size = 5 * 1024 * 1024
    try:
        if path.stat().st_size > max_size:
            logger.debug("ios sensitive scan: skipping %s larger than %s bytes", relative, max_size)
            return []
        data = path.read_bytes()
        if b"\x00" in data[:4096]:
            logger.debug("ios sensitive scan: skipping binary-looking %s (%s bytes)", relative, len(data))
            return []
        text = data.decode("utf-8", errors="ignore")
    except Exception as exc:
        logger.debug("ios sensitive scan: could not read %s: %s", relative, exc, exc_info=True)
        return []
    findings: list[dict[str, Any]] = []
    key_value_pattern = re.compile(
        r"""(?ix)
        (api[_-]?key|apikey|client[_-]?secret|secret|password|passwd|pwd|
        access[_-]?token|refresh[_-]?token|auth[_-]?token|token|private[_-]?key|credential)
        ["']?\s*[:=]\s*["']([^"'\s,;]{6,})
        """
    )
    for match in key_value_pattern.finditer(text):
        value = match.group(2)
        if value_looks_secret(value):
            findings.append(
                sensitive_finding(
                    str(relative),
                    "SENSITIVE_KEY_VALUE",
                    value,
                    sensitive_key_severity(match.group(1)),
                    context=match.group(1),
                    reveal_values=reveal_values,
                )
            )
    findings.extend(classify_sensitive_string(str(relative), text, reveal_values))
    deduped = dedupe_sensitive_findings(findings[:50])
    if deduped and logger.isEnabledFor(logging.DEBUG):
        logger.debug("ios sensitive scan: %s (%s chars): %s finding(s) at %s", relative, len(text), len(deduped), [(item["match_type"], item["context"]) for item in deduped])
    return deduped


# Match known secret formats in text, returning at most 10 findings.
def classify_sensitive_string(
    path: str,
    text: str,
    reveal_values: bool,
    key_path: str | None = None,
) -> list[dict[str, Any]]:
    patterns = [
        ("GOOGLE_API_KEY", "HIGH", re.compile(r"AIza[0-9A-Za-z_-]{20,}")),
        ("AWS_ACCESS_KEY_ID", "HIGH", re.compile(r"AKIA[0-9A-Z]{16}")),
        ("JWT", "HIGH", re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
        ("PRIVATE_KEY_MARKER", "HIGH", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
        ("BASIC_AUTH_URL", "HIGH", re.compile(r"https?://[^/\s:@]+:[^/\s:@]+@")),
        ("SLACK_TOKEN", "HIGH", re.compile(r"xox[baprs]-[0-9A-Za-z-]{20,}")),
    ]
    findings: list[dict[str, Any]] = []
    for match_type, severity, pattern in patterns:
        for match in pattern.finditer(text):
            findings.append(sensitive_finding(path, match_type, match.group(0), severity, key_path=key_path, reveal_values=reveal_values))
            logger.debug("ios sensitive scan: %s pattern matched in %s key_path=%s at offset %s", match_type, path, key_path, match.start())
            if len(findings) >= 10:
                logger.debug("ios sensitive scan: pattern finding cap of 10 reached for %s", path)
                return findings
    return findings


# Test up to max_keys unique unmasked Google API keys for reuse from this workstation.
def test_google_api_key_reuse(
    sensitive_findings: list[dict[str, Any]],
    config: dict[str, Any],
    reveal_values: bool,
) -> list[dict[str, Any]]:
    keys = []
    seen: set[str] = set()
    max_keys = int(config.get("max_keys", 5))
    timeout_seconds = float(config.get("timeout_seconds", 5))
    test_address = str(config.get("test_address", "Singapore"))
    for finding in sensitive_findings:
        if finding.get("match_type") != "GOOGLE_API_KEY":
            continue
        key = str(finding.get("_raw_value") or finding.get("value") or finding.get("reported_value") or "")
        if not key or "*" in key or key in seen:
            continue
        seen.add(key)
        keys.append((key, finding))
        if len(keys) >= max_keys:
            break
    logger.debug(
        "ios api key reuse: %s candidate Google API key(s) selected (max_keys=%s, timeout=%ss, test_address=%s)",
        len(keys),
        max_keys,
        timeout_seconds,
        test_address,
    )

    results: list[dict[str, Any]] = []
    for key, finding in keys:
        logger.debug("ios api key reuse: testing key from %s key_path=%s", finding.get("path"), finding.get("key_path"))
        result = test_google_geocode_key(key, timeout_seconds, test_address)
        result.update(
            {
                "path": finding.get("path"),
                "key_path": finding.get("key_path"),
                "match_type": finding.get("match_type"),
                "masked_key": mask_secret(key),
                "reported_key": key if reveal_values else mask_secret(key),
                "key_revealed": reveal_values,
                "severity": api_key_reuse_status_severity(result["status"]),
            }
        )
        logger.debug(
            "ios api key reuse: key from %s -> status=%s severity=%s http_status=%s google_status=%s message=%s",
            finding.get("path"),
            result["status"],
            result["severity"],
            result.get("http_status"),
            result.get("google_status"),
            str(result.get("message") or "")[:200],
        )
        results.append(result)
    return results


# Call the Geocoding API with a key and classify whether it was accepted.
def test_google_geocode_key(api_key: str, timeout_seconds: float, address: str) -> dict[str, Any]:
    query = urllib.parse.urlencode({"address": address, "key": api_key})
    url = f"https://maps.googleapis.com/maps/api/geocode/json?{query}"
    request = urllib.request.Request(url, headers={"User-Agent": "mobile-playbook-automation/0.1"})
    logger.debug("ios api key reuse: GET maps.googleapis.com/maps/api/geocode/json (timeout=%ss)", timeout_seconds)
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            body = response.read(64 * 1024)
            status_code = int(getattr(response, "status", 200))
    except urllib.error.HTTPError as exc:
        logger.debug("ios api key reuse: geocode returned HTTP %s", exc.code)
        status_code = exc.code
        body = exc.read(64 * 1024)
    except Exception as exc:
        logger.debug("ios api key reuse: geocode request failed after %.2fs with %s", time.monotonic() - started, type(exc).__name__)
        return {
            "provider": "google_geocode",
            "status": "NETWORK_OR_TEST_ERROR",
            "http_status": None,
            "google_status": None,
            "message": str(exc),
        }

    logger.debug("ios api key reuse: geocode response HTTP %s, %s byte(s) in %.2fs", status_code, len(body), time.monotonic() - started)
    try:
        payload = json.loads(body.decode("utf-8", errors="replace"))
    except Exception as exc:
        logger.debug("ios api key reuse: geocode response is not JSON: %s", type(exc).__name__)
        payload = {}
    google_status = str(payload.get("status") or "")
    error_message = str(payload.get("error_message") or "")
    return {
        "provider": "google_geocode",
        "status": classify_google_api_key_reuse_response(status_code, google_status, error_message),
        "http_status": status_code,
        "google_status": google_status or None,
        "message": error_message or f"HTTP {status_code}",
    }


# Classify a Geocoding API response as reusable, restricted, not enabled, blocked or inconclusive.
def classify_google_api_key_reuse_response(http_status: int, google_status: str, message: str) -> str:
    normalized = f"{google_status} {message}".lower()
    if http_status == 200 and google_status in {"OK", "ZERO_RESULTS"}:
        return "REUSABLE_FROM_WORKSTATION"
    if "referer" in normalized or "android" in normalized or "ios" in normalized or "ip address" in normalized or "not authorized" in normalized:
        return "RESTRICTED_OR_DENIED"
    if "api keys with referer restrictions cannot be used" in normalized:
        return "RESTRICTED_OR_DENIED"
    if "api project is not authorized" in normalized or "api has not been used" in normalized or "not enabled" in normalized:
        return "API_NOT_ENABLED_OR_DENIED"
    if "billing" in normalized or "quota" in normalized:
        return "ACCEPTED_BUT_SERVICE_BLOCKED"
    if http_status in {401, 403} or google_status == "REQUEST_DENIED":
        return "RESTRICTED_OR_DENIED"
    return "INCONCLUSIVE"


# Return the severity for an API key reuse status.
def api_key_reuse_status_severity(status: str) -> str:
    if status == "REUSABLE_FROM_WORKSTATION":
        return "HIGH"
    if status in {"ACCEPTED_BUT_SERVICE_BLOCKED", "INCONCLUSIVE", "NETWORK_OR_TEST_ERROR"}:
        return "MEDIUM"
    return "LOW"


# Build a finding record with the value masked unless reveal_values is set.
def sensitive_finding(
    path: str,
    match_type: str,
    value: str,
    severity: str,
    *,
    reveal_values: bool,
    key_path: str | None = None,
    context: str | None = None,
) -> dict[str, Any]:
    masked = mask_secret(value)
    reported = value if reveal_values else masked
    return {
        "path": path,
        "key_path": key_path,
        "context": context,
        "match_type": match_type,
        "masked_value": masked,
        "value": value if reveal_values else None,
        "_raw_value": value,
        "reported_value": reported,
        "value_revealed": reveal_values,
        "severity": severity,
    }


# Return copies of the findings without their raw values.
def public_sensitive_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    public_findings = []
    for finding in findings:
        public = dict(finding)
        public.pop("_raw_value", None)
        public_findings.append(public)
    return public_findings


# Flatten nested dicts and lists into (JSON-path, leaf value) pairs.
def flatten_value(value: Any, prefix: str = "$") -> list[tuple[str, Any]]:
    if isinstance(value, dict):
        flattened: list[tuple[str, Any]] = []
        for key, child in value.items():
            flattened.extend(flatten_value(child, f"{prefix}.{key}"))
        return flattened
    if isinstance(value, list):
        flattened = []
        for index, child in enumerate(value):
            flattened.extend(flatten_value(child, f"{prefix}.{index}"))
        return flattened
    return [(prefix, value)]


# Return whether a key name contains a secret-like term.
def key_looks_sensitive(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    sensitive_terms = (
        "apikey",
        "secret",
        "clientsecret",
        "password",
        "passwd",
        "token",
        "accesstoken",
        "refreshtoken",
        "privatekey",
        "credential",
    )
    return any(term in normalized for term in sensitive_terms)


# Return whether a value is long enough and not a common non-secret word.
def value_looks_secret(value: str) -> bool:
    stripped = value.strip()
    if len(stripped) < 6:
        return False
    common_false_positives = {"true", "false", "null", "none", "production", "staging", "development"}
    return stripped.lower() not in common_false_positives


# Return HIGH for key names with strong secret terms, else MEDIUM.
def sensitive_key_severity(key: str) -> str:
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    high_terms = (
        "apikey",
        "key",
        "credential",
        "password",
        "passwd",
        "clientsecret",
        "secret",
        "privatekey",
        "token",
        "refreshtoken",
        "accesstoken",
        "authtoken",
    )
    return "HIGH" if any(term in normalized for term in high_terms) else "MEDIUM"


# Mask a secret, keeping only a few leading and trailing characters of longer values.
def mask_secret(value: str) -> str:
    if len(value) <= 8:
        return "*" * len(value)
    if len(value) <= 16:
        return f"{value[:2]}...{value[-2:]}"
    return f"{value[:4]}...{value[-4:]}"


# Drop findings that repeat the same path, location, type and masked value.
def dedupe_sensitive_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str, str]] = set()
    deduped: list[dict[str, Any]] = []
    for item in findings:
        key = (
            str(item.get("path")),
            str(item.get("key_path") or item.get("context") or ""),
            str(item.get("match_type")),
            str(item.get("masked_value")),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


# Return whether a bundle file is in a signature, framework or plugin dir or is a binary asset.
def is_sensitive_scan_excluded(relative_path: Path) -> bool:
    if any(part in {"_CodeSignature", "Frameworks", "PlugIns", "SC_Info"} for part in relative_path.parts):
        return True
    if relative_path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".car", ".nib", ".otf", ".ttf", ".bin"}:
        return True
    return False
