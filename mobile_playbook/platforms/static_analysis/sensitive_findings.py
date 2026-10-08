"""
Detects secrets in package files and MobSF reports, masks them, and tests whether Google API keys can be reused.
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

logger = logging.getLogger(__name__)

MAX_TEXT_SCAN_BYTES = 5 * 1024 * 1024


# Find secret patterns and sensitive-key values in a MobSF report, capped at 100 and deduped.
def extract_mobsf_sensitive_findings(report: dict[str, Any], reveal_values: bool) -> list[dict[str, Any]]:
    text = json.dumps(report, sort_keys=True, ensure_ascii=False)
    findings = classify_sensitive_string("mobsf_report_json", text, reveal_values)
    logger.debug("sensitive scan: mobsf report json is %s char(s); %s pattern finding(s)", len(text), len(findings))
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
            logger.debug("sensitive scan: SENSITIVE_KEY_NAME match at %s (value length %s)", key, len(value))
    deduped = dedupe_sensitive_findings(findings[:100])
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "sensitive scan: mobsf report yielded %s pattern + %s key-name finding(s), %s after dedupe, by type=%s",
            pattern_count,
            len(findings) - pattern_count,
            len(deduped),
            dict(Counter(item.get("match_type") for item in deduped)),
        )
    return deduped


# Scan a text file under 5 MiB for secret key-value pairs and patterns.
def scan_text_for_sensitive_values(path: Path, relative: Path, reveal_values: bool) -> list[dict[str, Any]]:
    try:
        if path.stat().st_size > MAX_TEXT_SCAN_BYTES:
            logger.debug("sensitive scan: skipping %s larger than %s bytes", relative, MAX_TEXT_SCAN_BYTES)
            return []
        data = path.read_bytes()
    except Exception as exc:
        logger.debug("sensitive scan: could not read %s: %s", relative, exc, exc_info=True)
        return []
    return scan_text_content(data, str(relative), reveal_values)


# Scan a text file's bytes, reported under `label`, for secret key-value pairs and patterns; binary-looking content
# is skipped.
def scan_text_content(data: bytes, label: str, reveal_values: bool) -> list[dict[str, Any]]:
    if b"\x00" in data[:4096]:
        logger.debug("sensitive scan: skipping binary-looking %s (%s bytes)", label, len(data))
        return []
    text = data.decode("utf-8", errors="ignore")
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
                    label,
                    "SENSITIVE_KEY_VALUE",
                    value,
                    sensitive_key_severity(match.group(1)),
                    context=match.group(1),
                    reveal_values=reveal_values,
                )
            )
    findings.extend(classify_sensitive_string(label, text, reveal_values))
    deduped = dedupe_sensitive_findings(findings[:50])
    if deduped and logger.isEnabledFor(logging.DEBUG):
        logger.debug("sensitive scan: %s (%s chars): %s finding(s) at %s", label, len(text), len(deduped), [(item["match_type"], item["context"]) for item in deduped])
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
            logger.debug("sensitive scan: %s pattern matched in %s key_path=%s at offset %s", match_type, path, key_path, match.start())
            if len(findings) >= 10:
                logger.debug("sensitive scan: pattern finding cap of 10 reached for %s", path)
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
        "api key reuse: %s candidate Google API key(s) selected (max_keys=%s, timeout=%ss, test_address=%s)",
        len(keys),
        max_keys,
        timeout_seconds,
        test_address,
    )

    results: list[dict[str, Any]] = []
    for key, finding in keys:
        logger.debug("api key reuse: testing key from %s key_path=%s", finding.get("path"), finding.get("key_path"))
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
            "api key reuse: key from %s -> status=%s severity=%s http_status=%s google_status=%s message=%s",
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
    logger.debug("api key reuse: GET maps.googleapis.com/maps/api/geocode/json (timeout=%ss)", timeout_seconds)
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            body = response.read(64 * 1024)
            status_code = int(getattr(response, "status", 200))
    except urllib.error.HTTPError as exc:
        logger.debug("api key reuse: geocode returned HTTP %s", exc.code)
        status_code = exc.code
        body = exc.read(64 * 1024)
    except Exception as exc:
        logger.debug("api key reuse: geocode request failed after %.2fs with %s", time.monotonic() - started, type(exc).__name__)
        return {
            "provider": "google_geocode",
            "status": "NETWORK_OR_TEST_ERROR",
            "http_status": None,
            "google_status": None,
            "message": str(exc),
        }

    logger.debug("api key reuse: geocode response HTTP %s, %s byte(s) in %.2fs", status_code, len(body), time.monotonic() - started)
    try:
        payload = json.loads(body.decode("utf-8", errors="replace"))
    except Exception as exc:
        logger.debug("api key reuse: geocode response is not JSON: %s", type(exc).__name__)
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


# Return whether the risk config asks for sensitive values to be reported unmasked.
def reveal_sensitive_values(risk_config: dict[str, Any]) -> bool:
    return bool((risk_config.get("sensitive_scan") or {}).get("reveal_values", False))


# Run the configured Google API key reuse test on the findings and return the analysis summary fields reporting both.
def sensitive_summary_fields(sensitive_findings: list[dict[str, Any]], risk_config: dict[str, Any]) -> dict[str, Any]:
    reveal_values = reveal_sensitive_values(risk_config)
    api_key_reuse_config = risk_config.get("api_key_reuse_test") or {}
    api_key_reuse_enabled = bool(api_key_reuse_config.get("enabled", False))
    api_key_reuse_tests = test_google_api_key_reuse(sensitive_findings, api_key_reuse_config, reveal_values) if api_key_reuse_enabled else []
    logger.debug(
        "sensitive scan: %s finding(s), reveal_values=%s, api key reuse enabled=%s with %s test(s)",
        len(sensitive_findings), reveal_values, api_key_reuse_enabled, len(api_key_reuse_tests),
    )
    return {
        "sensitive_information_findings": public_sensitive_findings(sensitive_findings),
        "sensitive_scan": {
            "enabled": True,
            "reveal_values": reveal_values,
        },
        "api_key_reuse_test": {
            "enabled": api_key_reuse_enabled,
            "provider": api_key_reuse_config.get("provider", "google_geocode"),
            "results": api_key_reuse_tests,
        },
    }


# Return the summary sentences describing the sensitive findings and API key reuse results in `fields`, where
# `location` names where the findings were found.
def sensitive_summary_sentences(fields: dict[str, Any], location: str) -> list[str]:
    sentences = []
    findings = fields["sensitive_information_findings"]
    if findings:
        sentences.append(f"Potential sensitive information found in {location}: {len(findings)} finding(s).")
    results = fields["api_key_reuse_test"]["results"]
    if results:
        reusable = sum(1 for item in results if item["status"] == "REUSABLE_FROM_WORKSTATION")
        sentences.append(f"Google API key external reuse test completed: {reusable}/{len(results)} key(s) appeared reusable from this workstation.")
    return sentences


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
