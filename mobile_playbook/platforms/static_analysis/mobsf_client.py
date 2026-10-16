"""
MobSF REST client that uploads an app package, scans it and fetches the JSON report, optionally auto-starting MobSF,
plus readers for the report fields every platform shares.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import shlex
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

from mobile_playbook.common.env_file import env_value
from mobile_playbook.common.logging_setup import redacted
from mobile_playbook.platforms.static_analysis.findings import sort_flags_by_severity

logger = logging.getLogger(__name__)


# Upload an app package (IPA or APK), scan it and fetch the MobSF JSON report, auto-starting and stopping MobSF when configured.
def mobsf_scan(package_path: Path, analyzer_config: dict[str, Any]) -> dict[str, Any]:
    base_url = str(analyzer_config.get("mobsf_url") or analyzer_config.get("url") or "http://127.0.0.1:8000").rstrip("/")
    timeout = float(analyzer_config.get("timeout_seconds", 120))
    auto_start_config = analyzer_config.get("auto_start") or {}
    logger.debug(
        "mobsf: base_url=%s timeout=%ss auto_start enabled=%s generate_api_key=%s stop_after_scan=%s",
        base_url,
        timeout,
        bool(auto_start_config.get("enabled", False)),
        bool(auto_start_config.get("generate_api_key", False)),
        bool(auto_start_config.get("stop_after_scan", False)),
    )
    server_already_running = mobsf_is_reachable(base_url, timeout=2)
    generated_api_key = False
    api_key = mobsf_api_key(analyzer_config)
    if not server_already_running and not api_key and bool(auto_start_config.get("generate_api_key", False)):
        api_key = secrets.token_urlsafe(32)
        generated_api_key = True
    logger.debug("mobsf: server_already_running=%s api_key_present=%s generated_api_key=%s", server_already_running, bool(api_key), generated_api_key)
    if not api_key:
        logger.debug("mobsf: no API key available; aborting scan")
        raise RuntimeError("MobSF API key missing. Set MOBSF_API_KEY, analyzer.api_key, or enable analyzer.auto_start.generate_api_key.")

    process = maybe_start_mobsf(base_url, analyzer_config, api_key, server_already_running)
    auto_started = process is not None
    logger.debug("mobsf: auto_started=%s pid=%s", auto_started, getattr(process, "pid", None))
    try:
        logger.debug("mobsf: uploading %s", package_path)
        upload = mobsf_post(
            base_url,
            "/api/v1/upload",
            api_key,
            files={"file": (package_path.name, package_path.read_bytes(), "application/octet-stream")},
            timeout=timeout,
        )
        file_hash = str(upload.get("hash") or "")
        if not file_hash:
            logger.debug("mobsf: upload response had no hash, keys=%s", sorted(str(key) for key in upload) if isinstance(upload, dict) else type(upload).__name__)
            raise RuntimeError(f"MobSF upload did not return a hash: {upload}")
        scan_type = str(upload.get("scan_type") or package_path.suffix.lstrip(".").lower())
        file_name = str(upload.get("file_name") or package_path.name)
        logger.debug("mobsf: upload returned hash=%s scan_type=%s file_name=%s", file_hash, scan_type, file_name)
        mobsf_post(
            base_url,
            "/api/v1/scan",
            api_key,
            data={"hash": file_hash, "scan_type": scan_type, "file_name": file_name},
            timeout=timeout,
        )
        logger.debug("mobsf: scan requested for hash=%s; fetching report_json", file_hash)
        report = mobsf_post(
            base_url,
            "/api/v1/report_json",
            api_key,
            data={"hash": file_hash},
            timeout=timeout,
        )
        return {
            "base_url": base_url,
            "hash": file_hash,
            "scan_type": scan_type,
            "file_name": file_name,
            "auto_started": auto_started,
            "generated_api_key": generated_api_key,
            "upload": {key: value for key, value in upload.items() if key != "api_key"} if isinstance(upload, dict) else upload,
            "report": report,
        }
    finally:
        if process is not None and bool(auto_start_config.get("stop_after_scan", False)):
            logger.debug("mobsf: stop_after_scan set; terminating auto-started MobSF pid=%s", getattr(process, "pid", None))
            terminate_process(process)


# Return the configured MobSF API key, else its named setting from the environment or repository .env.
def mobsf_api_key(analyzer_config: dict[str, Any]) -> str:
    if analyzer_config.get("api_key"):
        logger.debug("mobsf: using API key from analyzer.api_key")
        return str(analyzer_config["api_key"])
    env_name = str(analyzer_config.get("api_key_env") or "MOBSF_API_KEY")
    if env_name in {"SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_URL", "DASHBOARD_SUPABASE_URL"}:
        raise ValueError("MobSF api_key_env must not name a worker-only database setting.")
    value = env_value(env_name) or ""
    logger.debug("mobsf: reading API key setting %s (set=%s)", env_name, bool(value))
    return value


# Launch MobSF with the API key when it is down and auto_start is enabled, waiting until reachable.
def maybe_start_mobsf(
    base_url: str,
    analyzer_config: dict[str, Any],
    api_key: str,
    server_already_running: bool,
) -> subprocess.Popen | None:
    if server_already_running:
        logger.debug("mobsf: server already reachable at %s; not auto-starting", base_url)
        return None
    auto_start = analyzer_config.get("auto_start") or {}
    if not bool(auto_start.get("enabled", False)):
        logger.debug("mobsf: server not reachable at %s and auto_start disabled", base_url)
        return None
    command = auto_start.get("command")
    if not command:
        logger.debug("mobsf: auto_start enabled but command is empty")
        raise RuntimeError("MobSF auto_start.enabled is true but auto_start.command is empty.")
    if isinstance(command, str):
        command = shlex.split(command)
    if not isinstance(command, list) or not all(isinstance(part, str) and part for part in command):
        logger.debug("mobsf: auto_start command has invalid type %s", type(command).__name__)
        raise RuntimeError("MobSF auto_start.command must be a non-empty command list or command string.")

    env = os.environ.copy()
    env_name = str(auto_start.get("api_key_env") or analyzer_config.get("api_key_env") or "MOBSF_API_KEY")
    env[env_name] = api_key
    for key, value in (auto_start.get("env") or {}).items():
        env[str(key)] = str(value)

    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "mobsf: auto-starting MobSF executable=%s arg_count=%s api_key_env=%s extra_env_keys=%s",
            command[0],
            len(command) - 1,
            env_name,
            sorted(str(key) for key in (auto_start.get("env") or {})),
        )
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    wait_seconds = float(auto_start.get("wait_seconds", 90))
    poll_interval = float(auto_start.get("poll_interval_seconds", 1))
    logger.debug("mobsf: started pid=%s; waiting up to %ss (poll every %ss) for %s", getattr(process, "pid", None), wait_seconds, poll_interval, base_url)
    started = time.monotonic()
    deadline = started + wait_seconds
    attempts = 0
    while time.monotonic() < deadline:
        attempts += 1
        if process.poll() is not None:
            logger.debug("mobsf: auto-start process exited with %s after %s poll(s)", getattr(process, "returncode", None), attempts)
            raise RuntimeError(f"MobSF auto-start command exited before the server was ready: {command[0]}")
        if mobsf_is_reachable(base_url, timeout=2):
            logger.debug("mobsf: server ready after %s poll(s) in %.2fs", attempts, time.monotonic() - started)
            return process
        time.sleep(poll_interval)
    logger.debug("mobsf: auto-start timed out after %s poll(s); terminating pid=%s", attempts, getattr(process, "pid", None))
    terminate_process(process)
    raise RuntimeError(f"MobSF auto-start timed out after {wait_seconds:g}s waiting for {base_url}")


# Return whether the MobSF base URL answers without a server error.
def mobsf_is_reachable(base_url: str, timeout: float) -> bool:
    try:
        with urllib.request.urlopen(base_url, timeout=timeout) as response:
            logger.debug("mobsf: GET %s -> %s", base_url, getattr(response, "status", 200))
            return int(getattr(response, "status", 200)) < 500
    except urllib.error.HTTPError as exc:
        logger.debug("mobsf: GET %s -> HTTP %s", base_url, exc.code, exc_info=True)
        return exc.code < 500
    except Exception as exc:
        logger.debug("mobsf: GET %s unreachable: %s", base_url, exc, exc_info=True)
        return False


# Terminate a process, killing it if it does not exit within 10 seconds.
def terminate_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        logger.debug("mobsf: process pid=%s already exited with %s", getattr(process, "pid", None), getattr(process, "returncode", None))
        return
    logger.debug("mobsf: terminating process pid=%s", getattr(process, "pid", None))
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired as exc:
        logger.debug("mobsf: pid=%s did not exit after terminate; killing: %s", getattr(process, "pid", None), exc, exc_info=True)
        process.kill()
        process.wait(timeout=10)
    logger.debug("mobsf: process pid=%s exited with %s", getattr(process, "pid", None), getattr(process, "returncode", None))


# POST form or multipart data to a MobSF endpoint and return the decoded JSON response.
def mobsf_post(
    base_url: str,
    endpoint: str,
    api_key: str,
    *,
    data: dict[str, Any] | None = None,
    files: dict[str, tuple[str, bytes, str]] | None = None,
    timeout: float,
) -> dict[str, Any]:
    headers = {"Authorization": api_key}
    if files:
        body, content_type = multipart_form_data(data or {}, files)
        headers["Content-Type"] = content_type
    else:
        body = urllib.parse.urlencode({key: str(value) for key, value in (data or {}).items()}).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    request = urllib.request.Request(f"{base_url}{endpoint}", data=body, headers=headers, method="POST")
    logger.debug(
        "mobsf: POST %s%s content_type=%s body_bytes=%s data=%s files=%s timeout=%ss",
        base_url,
        endpoint,
        headers["Content-Type"].split(";", 1)[0],
        len(body),
        redacted(data or {}),
        sorted(files) if files else [],
        timeout,
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response_body = response.read()
            logger.debug("mobsf: POST %s -> %s, %s byte(s) in %.2fs", endpoint, getattr(response, "status", None), len(response_body), time.monotonic() - started)
    except urllib.error.HTTPError as exc:
        logger.debug("mobsf: POST %s -> HTTP %s after %.2fs", endpoint, exc.code, time.monotonic() - started, exc_info=True)
        error_body = exc.read(64 * 1024).decode("utf-8", errors="replace")
        logger.debug("mobsf: POST %s error body length=%s", endpoint, len(error_body))
        raise RuntimeError(f"MobSF {endpoint} failed with HTTP {exc.code}: {error_body}") from exc
    try:
        return json.loads(response_body.decode("utf-8", errors="replace"))
    except Exception as exc:
        logger.debug("mobsf: POST %s response is not JSON (%s byte(s)): %s", endpoint, len(response_body), exc, exc_info=True)
        raise RuntimeError(f"MobSF {endpoint} did not return JSON") from exc


# Encode fields and files as a multipart/form-data body and return it with its content type.
def multipart_form_data(fields: dict[str, Any], files: dict[str, tuple[str, bytes, str]]) -> tuple[bytes, str]:
    boundary = f"----mobile-playbook-automation-{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                str(value).encode("utf-8"),
                b"\r\n",
            ]
        )
    for name, (filename, content, content_type) in files.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode(),
                f"Content-Type: {content_type}\r\n\r\n".encode(),
                content,
                b"\r\n",
            ]
        )
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


# Return the sorted unique file paths listed anywhere in a MobSF report.
def mobsf_file_items(report: dict[str, Any]) -> list[str]:
    candidates = []
    for key in ("files", "file_analysis", "file_list", "resources"):
        value = report.get(key)
        if isinstance(value, dict):
            candidates.extend(str(path) for path in value.keys())
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    candidates.append(item)
                elif isinstance(item, dict):
                    path = first_present(item, "file_path", "path", "name")
                    if path:
                        candidates.append(str(path))
    return sorted(set(candidates))


# Return the sorted permission names from a MobSF report.
def mobsf_permissions(report: dict[str, Any]) -> list[str]:
    permissions = report.get("permissions") or report.get("permission_analysis") or {}
    if isinstance(permissions, dict):
        return sorted(str(key) for key in permissions.keys())
    if isinstance(permissions, list):
        return sorted(str(item.get("permission") or item.get("name") or item) for item in permissions)
    return []


# Walk the report for titled non-INFO findings, capped at 100, deduped and sorted by severity.
def extract_mobsf_findings(report: dict[str, Any]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []

    # Recursively collect titled findings from dicts and lists up to depth 7.
    def walk(value: Any, path: str, depth: int = 0) -> None:
        if len(findings) >= 100 or depth > 7:
            return
        if isinstance(value, dict):
            severity = normalize_mobsf_severity(first_present(value, "severity", "risk", "level", "cvss", "owasp"))
            title = first_present(value, "title", "issue", "name", "description", "rule", "check")
            if title and severity != "INFO":
                evidence = first_present(value, "file", "path", "component", "details", "message", "description") or path
                findings.append({"severity": severity, "title": str(title), "evidence": str(evidence)[:500], "source_path": path})
            for child_key, child_value in value.items():
                walk(child_value, f"{path}.{child_key}" if path else str(child_key), depth + 1)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, f"{path}[{index}]", depth + 1)

    walk(report, "mobsf_report")
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for finding in findings:
        key = (finding["severity"], finding["title"], finding["evidence"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(finding)
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "mobsf: extracted %s finding(s), %s after dedupe, by severity=%s",
            len(findings),
            len(deduped),
            dict(Counter(item["severity"] for item in deduped)),
        )
    return sort_flags_by_severity(deduped)


# Return the first non-empty value among the given keys, or None.
def first_present(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = data.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


# Map a MobSF severity label to HIGH, MEDIUM, LOW or INFO.
def normalize_mobsf_severity(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"high", "critical", "danger", "severe"} or text.startswith("high"):
        return "HIGH"
    if text in {"medium", "warning", "warn", "moderate"} or text.startswith("medium"):
        return "MEDIUM"
    if text in {"low", "secure", "passed"} or text.startswith("low"):
        return "LOW"
    return "INFO"
