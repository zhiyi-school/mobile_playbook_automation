from __future__ import annotations

import json
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

from mobile_playbook.platforms.ios.models import ArtifactAcquisitionResult
from mobile_playbook.platforms.ios.risks.critical_markdown import mobsf_critical_findings, sort_flags_by_severity
from mobile_playbook.platforms.ios.risks.ipa_inventory import is_interesting_resource
from mobile_playbook.platforms.ios.risks.sensitive_findings import (
    extract_mobsf_sensitive_findings,
    public_sensitive_findings,
    test_google_api_key_reuse,
)


def analyze_with_mobsf(
    ipa_path: Path,
    acquisition: ArtifactAcquisitionResult,
    binary_inspection,
    risk_config: dict[str, Any],
) -> dict[str, Any]:
    analyzer_config = risk_config.get("analyzer") or {}
    mobsf = mobsf_scan(ipa_path, analyzer_config)
    report = mobsf["report"]
    sensitive_config = risk_config.get("sensitive_scan") or {}
    reveal_sensitive_values = bool(sensitive_config.get("reveal_values", False))
    api_key_reuse_config = risk_config.get("api_key_reuse_test") or {}
    api_key_reuse_enabled = bool(api_key_reuse_config.get("enabled", False))
    sensitive_findings = extract_mobsf_sensitive_findings(report, reveal_sensitive_values)
    api_key_reuse_tests = test_google_api_key_reuse(sensitive_findings, api_key_reuse_config, reveal_sensitive_values) if api_key_reuse_enabled else []
    public_findings = public_sensitive_findings(sensitive_findings)
    findings = extract_mobsf_findings(report)
    info_plist = mobsf_info_plist(report)
    mobsf_package_inventory = mobsf_inventory(report)
    summary = {
        "analysis_provider": "mobsf",
        "app_bundle": first_present(report, "file_name", "app_file", "name") or ipa_path.name,
        "bundle_id": first_present(report, "bundle_id", "packagename", "package_name") or acquisition.bundle_id,
        "display_name": first_present(report, "app_name", "title", "name") or acquisition.display_name,
        "executable_name": acquisition.executable_name,
        "ipa_sha256": first_present(report, "sha256", "file_sha256") or acquisition.input_sha256,
        "binary_inspection": binary_inspection.to_dict(),
        "info_plist": info_plist,
        "counts": mobsf_package_inventory["counts"],
        "findings": [
            "MobSF static analysis completed for the acquired IPA.",
            "MobSF report was normalized into the framework's standard IPA analysis reports.",
        ],
        "mobsf": {
            "status": "USED",
            "base_url": mobsf["base_url"],
            "hash": mobsf["hash"],
            "scan_type": mobsf["scan_type"],
            "file_name": mobsf["file_name"],
            "auto_started": bool(mobsf.get("auto_started", False)),
            "generated_api_key": bool(mobsf.get("generated_api_key", False)),
            "report_key_count": len(report) if isinstance(report, dict) else 0,
        },
        "mobsf_findings": findings,
        "sensitive_information_findings": public_findings,
        "sensitive_scan": {
            "enabled": True,
            "reveal_values": reveal_sensitive_values,
        },
        "api_key_reuse_test": {
            "enabled": api_key_reuse_enabled,
            "provider": api_key_reuse_config.get("provider", "google_geocode"),
            "results": api_key_reuse_tests,
        },
    }
    if sensitive_findings:
        summary["findings"].append(f"Potential sensitive information found in MobSF report data: {len(sensitive_findings)} finding(s).")
    if findings:
        high_or_medium = sum(1 for item in findings if item.get("severity") in {"HIGH", "MEDIUM"})
        summary["findings"].append(f"MobSF reported {len(findings)} normalized finding(s), including {high_or_medium} high/medium finding(s).")
    if api_key_reuse_tests:
        reusable = sum(1 for item in api_key_reuse_tests if item["status"] == "REUSABLE_FROM_WORKSTATION")
        summary["findings"].append(f"Google API key external reuse test completed: {reusable}/{len(api_key_reuse_tests)} key(s) appeared reusable from this workstation.")
    return {
        "summary": summary,
        "inventory": mobsf_package_inventory,
        "critical_findings": mobsf_critical_findings(summary, mobsf_package_inventory),
        "mobsf_raw_report": report,
    }


def mobsf_scan(ipa_path: Path, analyzer_config: dict[str, Any]) -> dict[str, Any]:
    base_url = str(analyzer_config.get("mobsf_url") or analyzer_config.get("url") or "http://127.0.0.1:8000").rstrip("/")
    timeout = float(analyzer_config.get("timeout_seconds", 120))
    auto_start_config = analyzer_config.get("auto_start") or {}
    server_already_running = mobsf_is_reachable(base_url, timeout=2)
    generated_api_key = False
    api_key = mobsf_api_key(analyzer_config)
    if not server_already_running and not api_key and bool(auto_start_config.get("generate_api_key", False)):
        api_key = secrets.token_urlsafe(32)
        generated_api_key = True
    if not api_key:
        raise RuntimeError("MobSF API key missing. Set MOBSF_API_KEY, analyzer.api_key, or enable analyzer.auto_start.generate_api_key.")

    process = maybe_start_mobsf(base_url, analyzer_config, api_key, server_already_running)
    auto_started = process is not None
    try:
        upload = mobsf_post(
            base_url,
            "/api/v1/upload",
            api_key,
            files={"file": (ipa_path.name, ipa_path.read_bytes(), "application/octet-stream")},
            timeout=timeout,
        )
        file_hash = str(upload.get("hash") or "")
        if not file_hash:
            raise RuntimeError(f"MobSF upload did not return a hash: {upload}")
        scan_type = str(upload.get("scan_type") or "ipa")
        file_name = str(upload.get("file_name") or ipa_path.name)
        mobsf_post(
            base_url,
            "/api/v1/scan",
            api_key,
            data={"hash": file_hash, "scan_type": scan_type, "file_name": file_name},
            timeout=timeout,
        )
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
            terminate_process(process)


def mobsf_api_key(analyzer_config: dict[str, Any]) -> str:
    if analyzer_config.get("api_key"):
        return str(analyzer_config["api_key"])
    env_name = str(analyzer_config.get("api_key_env") or "MOBSF_API_KEY")
    return os.environ.get(env_name, "")


def maybe_start_mobsf(
    base_url: str,
    analyzer_config: dict[str, Any],
    api_key: str,
    server_already_running: bool,
) -> subprocess.Popen | None:
    if server_already_running:
        return None
    auto_start = analyzer_config.get("auto_start") or {}
    if not bool(auto_start.get("enabled", False)):
        return None
    command = auto_start.get("command")
    if not command:
        raise RuntimeError("MobSF auto_start.enabled is true but auto_start.command is empty.")
    if isinstance(command, str):
        command = shlex.split(command)
    if not isinstance(command, list) or not all(isinstance(part, str) and part for part in command):
        raise RuntimeError("MobSF auto_start.command must be a non-empty command list or command string.")

    env = os.environ.copy()
    env_name = str(auto_start.get("api_key_env") or analyzer_config.get("api_key_env") or "MOBSF_API_KEY")
    env[env_name] = api_key
    for key, value in (auto_start.get("env") or {}).items():
        env[str(key)] = str(value)

    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    wait_seconds = float(auto_start.get("wait_seconds", 90))
    poll_interval = float(auto_start.get("poll_interval_seconds", 1))
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"MobSF auto-start command exited before the server was ready: {command[0]}")
        if mobsf_is_reachable(base_url, timeout=2):
            return process
        time.sleep(poll_interval)
    terminate_process(process)
    raise RuntimeError(f"MobSF auto-start timed out after {wait_seconds:g}s waiting for {base_url}")


def mobsf_is_reachable(base_url: str, timeout: float) -> bool:
    try:
        with urllib.request.urlopen(base_url, timeout=timeout) as response:
            return int(getattr(response, "status", 200)) < 500
    except urllib.error.HTTPError as exc:
        return exc.code < 500
    except Exception:
        return False


def terminate_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


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
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response_body = response.read()
    except urllib.error.HTTPError as exc:
        error_body = exc.read(64 * 1024).decode("utf-8", errors="replace")
        raise RuntimeError(f"MobSF {endpoint} failed with HTTP {exc.code}: {error_body}") from exc
    try:
        return json.loads(response_body.decode("utf-8", errors="replace"))
    except Exception as exc:
        raise RuntimeError(f"MobSF {endpoint} did not return JSON") from exc


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


def mobsf_info_plist(report: dict[str, Any]) -> dict[str, Any]:
    info = report.get("info_plist") if isinstance(report.get("info_plist"), dict) else {}
    permissions = mobsf_permissions(report)
    ats = first_present(report, "ats_analysis", "app_transport_security") or info.get("NSAppTransportSecurity") or {}
    schemes = mobsf_url_schemes(report, info)
    return {
        "CFBundleIdentifier": first_present(report, "bundle_id", "packagename") or info.get("CFBundleIdentifier"),
        "CFBundleShortVersionString": first_present(report, "version", "app_version") or info.get("CFBundleShortVersionString"),
        "CFBundleVersion": first_present(report, "build", "build_number") or info.get("CFBundleVersion"),
        "CFBundleDisplayName": first_present(report, "app_name", "name") or info.get("CFBundleDisplayName"),
        "CFBundleExecutable": info.get("CFBundleExecutable"),
        "permissions": permissions,
        "url_schemes": schemes,
        "app_transport_security": ats,
    }


def mobsf_inventory(report: dict[str, Any]) -> dict[str, Any]:
    file_items = mobsf_file_items(report)
    frameworks = sorted({path for path in file_items if ".framework" in path})
    plugins = sorted({path for path in file_items if ".appex" in path or "/PlugIns/" in path})
    suffixes: Counter[str] = Counter(Path(path).suffix.lower() or "<none>" for path in file_items)
    resource_samples = [path for path in file_items if is_interesting_resource(Path(path))][:50]
    return {
        "counts": {
            "files": len(file_items),
            "frameworks": len(frameworks),
            "plugins": len(plugins),
            "resource_samples": len(resource_samples),
        },
        "suffix_counts": dict(sorted(suffixes.items())),
        "frameworks": frameworks[:100],
        "plugins": plugins[:100],
        "resource_samples": resource_samples,
        "files": [{"path": path, "suffix": Path(path).suffix.lower() or "<none>"} for path in file_items[:5000]],
        "mobsf_report_keys": sorted(report.keys()) if isinstance(report, dict) else [],
    }


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


def mobsf_permissions(report: dict[str, Any]) -> list[str]:
    permissions = report.get("permissions") or report.get("permission_analysis") or {}
    if isinstance(permissions, dict):
        return sorted(str(key) for key in permissions.keys())
    if isinstance(permissions, list):
        return sorted(str(item.get("permission") or item.get("name") or item) for item in permissions)
    return []


def mobsf_url_schemes(report: dict[str, Any], info: dict[str, Any]) -> list[str]:
    schemes: set[str] = set()
    for key in ("url_schemes", "url_scheme", "urls"):
        value = report.get(key)
        if isinstance(value, list):
            schemes.update(str(item) for item in value if not str(item).startswith("http"))
        elif isinstance(value, dict):
            schemes.update(str(item) for item in value.keys() if not str(item).startswith("http"))
    for item in info.get("CFBundleURLTypes") or []:
        if isinstance(item, dict):
            schemes.update(str(value) for value in item.get("CFBundleURLSchemes") or [])
    return sorted(schemes)


def extract_mobsf_findings(report: dict[str, Any]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []

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
    return sort_flags_by_severity(deduped)


def first_present(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = data.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def normalize_mobsf_severity(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"high", "critical", "danger", "severe"} or text.startswith("high"):
        return "HIGH"
    if text in {"medium", "warning", "warn", "moderate"} or text.startswith("medium"):
        return "MEDIUM"
    if text in {"low", "secure", "passed"} or text.startswith("low"):
        return "LOW"
    return "INFO"
