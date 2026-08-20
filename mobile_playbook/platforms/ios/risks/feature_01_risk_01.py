from __future__ import annotations

import json
import os
import re
import secrets
import shlex
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mobile_playbook.core.config_files import merge_dicts
from mobile_playbook.platforms.ios.artifacts.registry import get_provider
from mobile_playbook.platforms.ios.mutations.hashing import sha256_file
from mobile_playbook.platforms.ios.mutations.mutability import inspect_main_executable
from mobile_playbook.platforms.ios.ipa.plist_utils import read_info_plist, read_plist
from mobile_playbook.platforms.ios.ipa.unpacker import unpack_ipa
from mobile_playbook.platforms.ios.models import ArtifactAcquisitionResult, RiskRunResult
from mobile_playbook.platforms.ios.risks.base import Risk


class Feature01Risk01(Risk):
    risk_id = "ios-feature-01-risk-01"
    feature_id = "feature-01"
    name = "IPA acquisition static analysis exposure"
    description = (
        "An acquired IPA can be unpacked and statically analyzed on a workstation, exposing metadata, "
        "bundled resources, permissions, URL schemes, binary characteristics, and embedded sensitive "
        "strings such as API keys or credentials."
    )
    goal = "Demonstrate what a workstation-side static analysis of an acquired IPA can reveal about the app."
    mitre_attack_mobile_technique_id = "Discovery"
    requires_ipa_artifact = True
    requires_device = False

    def run(self, app_config, global_config, device_client, report_writer):
        risk_config = merge_dicts(global_config.ipa_static_analysis, app_config.risks.get(self.risk_id) or {})
        result = self._base_result(report_writer.run_timestamp, app_config)
        report_dir = report_writer.test_report_dir(app_config.id, self.risk_id, "ipa_static_analysis")
        work_dir = Path(global_config.runner.work_dir) / report_writer.run_timestamp / app_config.id / self.risk_id / "ipa_static_analysis"
        work_dir.mkdir(parents=True, exist_ok=True)
        try:
            artifact_source = app_config.artifact.get("source", "")
            if artifact_source == "installed_app_reference":
                result.final_status = "ARTIFACT_REQUIRED"
                result.errors.append("ios-feature-01-risk-01 requires an IPA artifact; installed_app_reference is black-box only")
                return [result]
            provider = get_provider(artifact_source)
            if provider is None:
                result.final_status = "UNSUPPORTED_ARTIFACT_SOURCE"
                result.errors.append(f"Unsupported artifact source: {app_config.artifact.get('source', '')}")
                return [result]

            acquisition = provider.acquire(
                app_config,
                global_config,
                device_client,
                report_writer.run_timestamp,
                Path(app_config.artifact.get("workspace_dir") or "work/ios/acquired"),
            )
            result.artifact_result = acquisition
            if acquisition.status == "INSTALLED_APP_VERIFIED":
                result.final_status = "ARTIFACT_REQUIRED"
                result.errors.append("ios-feature-01-risk-01 requires an IPA artifact; installed_app_reference is black-box only")
                return [result]
            if acquisition.status != "ACQUIRED" or acquisition.ipa_path is None:
                result.final_status = self._artifact_status_to_final(acquisition.status)
                result.errors.extend(acquisition.errors)
                return [result]

            result.acquired_ipa = acquisition.ipa_path
            result.acquired_ipa_sha256 = acquisition.input_sha256
            result.input_ipa = acquisition.ipa_path
            result.input_ipa_sha256 = acquisition.input_sha256 or sha256_file(acquisition.ipa_path)
            if acquisition.bundle_id:
                result.original_bundle_id = acquisition.bundle_id

            try:
                app_dir = unpack_ipa(acquisition.ipa_path, work_dir / "unpacked")
            except Exception as exc:
                result.final_status = "UNPACK_FAILED"
                result.errors.append(str(exc))
                return [result]

            binary_inspection = inspect_main_executable(app_dir)
            result.binary_inspection_result = binary_inspection
            analysis = self._run_configured_analysis(acquisition.ipa_path, app_dir, acquisition, binary_inspection, risk_config)
            analysis_path = report_dir / "ipa_analysis.json"
            inventory_path = report_dir / "package_inventory.json"
            critical_path = report_dir / "critical_findings.json"
            critical_md_path = report_dir / "critical_findings.md"
            analysis_path.write_text(json.dumps(analysis["summary"], indent=2, sort_keys=True))
            inventory_path.write_text(json.dumps(analysis["inventory"], indent=2, sort_keys=True))
            critical_path.write_text(json.dumps(analysis["critical_findings"], indent=2, sort_keys=True))
            critical_md_path.write_text(self._critical_markdown(analysis["critical_findings"]))
            mobsf_report_path = None
            if analysis.get("mobsf_raw_report") is not None:
                mobsf_report_path = report_dir / "mobsf_report.json"
                mobsf_report_path.write_text(json.dumps(analysis["mobsf_raw_report"], indent=2, sort_keys=True))
            result.final_status = "IPA_ANALYSIS_COMPLETE"
            # A completed static analysis is itself the finding — an acquired IPA
            # can always be unpacked and inventoried for exposure once analysis runs.
            result.verdict = "At Risk"
            result.errors.extend(item["title"] for item in analysis["critical_findings"]["flags"][:3])
            result.launch_result = {
                "analysis_provider": analysis["summary"].get("analysis_provider", "builtin"),
                "analysis_path": str(analysis_path),
                "inventory_path": str(inventory_path),
                "critical_findings_path": str(critical_path),
                "critical_findings_markdown_path": str(critical_md_path),
                "mobsf_report_path": str(mobsf_report_path) if mobsf_report_path else None,
                "owasp_reference": "https://mas.owasp.org/MASTG/techniques/ios/MASTG-TECH-0058/",
            }
            return [result]
        except Exception as exc:
            result.final_status = "FAILED"
            result.errors.append(str(exc))
            return [result]
        finally:
            result.timestamp_end = datetime.now(timezone.utc).isoformat()
            report_writer.write_result(result, report_dir)

    def _base_result(self, run_timestamp: str, app_config) -> RiskRunResult:
        return RiskRunResult(
            run_timestamp=run_timestamp,
            timestamp_start=datetime.now(timezone.utc).isoformat(),
            timestamp_end=None,
            app_id=app_config.id,
            app_name=app_config.name,
            original_bundle_id=app_config.bundle_id,
            test_bundle_id=app_config.test_bundle_id,
            risk_id=self.risk_id,
            feature_id=self.feature_id,
            test_case_id="ipa_static_analysis",
            test_case_type="mobsf_or_package_analysis",
            artifact_source=app_config.artifact.get("source", ""),
        )

    def _run_configured_analysis(
        self,
        ipa_path: Path,
        app_dir: Path,
        acquisition: ArtifactAcquisitionResult,
        binary_inspection,
        risk_config: dict[str, Any],
    ) -> dict[str, Any]:
        analyzer_config = risk_config.get("analyzer") or {}
        provider = str(analyzer_config.get("provider") or "builtin").strip().lower()
        fallback_to_builtin = bool(analyzer_config.get("fallback_to_builtin", True))

        if provider in {"builtin", "package", "package_inventory", "local"}:
            return self._analyze_package(app_dir, acquisition, binary_inspection, risk_config)

        if provider in {"mobsf", "mobsf_api"}:
            try:
                return self._analyze_with_mobsf(ipa_path, acquisition, binary_inspection, risk_config)
            except Exception as exc:
                if not fallback_to_builtin:
                    raise
                analysis = self._analyze_package(app_dir, acquisition, binary_inspection, risk_config)
                analysis["summary"]["mobsf_fallback"] = {
                    "used": True,
                    "reason": str(exc),
                    "configured_provider": provider,
                }
                analysis["summary"]["findings"].append(f"MobSF analysis failed; package scanner fallback was used: {exc}")
                analysis["critical_findings"]["flags"].append(
                    {
                        "id": "MOBSF_FALLBACK_USED",
                        "severity": "LOW",
                        "title": "MobSF analysis was unavailable, built-in package scanner was used",
                        "evidence": [str(exc)],
                        "recommendation": "Start MobSF, verify the API key, and rerun ios-feature-01-risk-01 for a MobSF-backed report.",
                    }
                )
                analysis["critical_findings"]["flags"] = self._sort_flags_by_severity(analysis["critical_findings"]["flags"])
                analysis["critical_findings"]["flag_count"] = len(analysis["critical_findings"]["flags"])
                analysis["critical_findings"]["highest_severity"] = self._highest_severity(analysis["critical_findings"]["flags"])
                return analysis

        if fallback_to_builtin:
            analysis = self._analyze_package(app_dir, acquisition, binary_inspection, risk_config)
            analysis["summary"]["analyzer_warning"] = f"Unknown analyzer provider '{provider}', built-in package scanner was used."
            return analysis
        raise ValueError(f"Unknown ios-feature-01-risk-01 analyzer provider: {provider}")

    def _analyze_with_mobsf(
        self,
        ipa_path: Path,
        acquisition: ArtifactAcquisitionResult,
        binary_inspection,
        risk_config: dict[str, Any],
    ) -> dict[str, Any]:
        analyzer_config = risk_config.get("analyzer") or {}
        mobsf = self._mobsf_scan(ipa_path, analyzer_config)
        report = mobsf["report"]
        sensitive_config = risk_config.get("sensitive_scan") or {}
        reveal_sensitive_values = bool(sensitive_config.get("reveal_values", False))
        api_key_reuse_config = risk_config.get("api_key_reuse_test") or {}
        api_key_reuse_enabled = bool(api_key_reuse_config.get("enabled", False))
        sensitive_findings = self._extract_mobsf_sensitive_findings(report, reveal_sensitive_values)
        api_key_reuse_tests = self._test_google_api_key_reuse(sensitive_findings, api_key_reuse_config, reveal_sensitive_values) if api_key_reuse_enabled else []
        public_sensitive_findings = self._public_sensitive_findings(sensitive_findings)
        findings = self._extract_mobsf_findings(report)
        info_plist = self._mobsf_info_plist(report)
        inventory = self._mobsf_inventory(report)
        summary = {
            "analysis_provider": "mobsf",
            "app_bundle": self._first_present(report, "file_name", "app_file", "name") or ipa_path.name,
            "bundle_id": self._first_present(report, "bundle_id", "packagename", "package_name") or acquisition.bundle_id,
            "display_name": self._first_present(report, "app_name", "title", "name") or acquisition.display_name,
            "executable_name": acquisition.executable_name,
            "ipa_sha256": self._first_present(report, "sha256", "file_sha256") or acquisition.input_sha256,
            "binary_inspection": binary_inspection.to_dict(),
            "info_plist": info_plist,
            "counts": inventory["counts"],
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
            "sensitive_information_findings": public_sensitive_findings,
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
        critical_findings = self._mobsf_critical_findings(summary, inventory)
        return {
            "summary": summary,
            "inventory": inventory,
            "critical_findings": critical_findings,
            "mobsf_raw_report": report,
        }

    def _mobsf_scan(self, ipa_path: Path, analyzer_config: dict[str, Any]) -> dict[str, Any]:
        base_url = str(analyzer_config.get("mobsf_url") or analyzer_config.get("url") or "http://127.0.0.1:8000").rstrip("/")
        timeout = float(analyzer_config.get("timeout_seconds", 120))
        auto_start_config = analyzer_config.get("auto_start") or {}
        server_already_running = self._mobsf_is_reachable(base_url, timeout=2)
        generated_api_key = False
        api_key = self._mobsf_api_key(analyzer_config)
        if not server_already_running and not api_key and bool(auto_start_config.get("generate_api_key", False)):
            api_key = secrets.token_urlsafe(32)
            generated_api_key = True
        if not api_key:
            raise RuntimeError("MobSF API key missing. Set MOBSF_API_KEY, analyzer.api_key, or enable analyzer.auto_start.generate_api_key.")

        process = self._maybe_start_mobsf(base_url, analyzer_config, api_key, server_already_running)
        auto_started = process is not None
        try:
            upload = self._mobsf_post(
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
            self._mobsf_post(
                base_url,
                "/api/v1/scan",
                api_key,
                data={"hash": file_hash, "scan_type": scan_type, "file_name": file_name},
                timeout=timeout,
            )
            report = self._mobsf_post(
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
                self._terminate_process(process)

    def _mobsf_api_key(self, analyzer_config: dict[str, Any]) -> str:
        if analyzer_config.get("api_key"):
            return str(analyzer_config["api_key"])
        env_name = str(analyzer_config.get("api_key_env") or "MOBSF_API_KEY")
        return os.environ.get(env_name, "")

    def _maybe_start_mobsf(
        self,
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
            if self._mobsf_is_reachable(base_url, timeout=2):
                return process
            time.sleep(poll_interval)
        self._terminate_process(process)
        raise RuntimeError(f"MobSF auto-start timed out after {wait_seconds:g}s waiting for {base_url}")

    def _mobsf_is_reachable(self, base_url: str, timeout: float) -> bool:
        try:
            with urllib.request.urlopen(base_url, timeout=timeout) as response:
                return int(getattr(response, "status", 200)) < 500
        except urllib.error.HTTPError as exc:
            return exc.code < 500
        except Exception:
            return False

    def _terminate_process(self, process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)

    def _mobsf_post(
        self,
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
            body, content_type = self._multipart_form_data(data or {}, files)
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

    def _multipart_form_data(self, fields: dict[str, Any], files: dict[str, tuple[str, bytes, str]]) -> tuple[bytes, str]:
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

    def _mobsf_info_plist(self, report: dict[str, Any]) -> dict[str, Any]:
        info = report.get("info_plist") if isinstance(report.get("info_plist"), dict) else {}
        permissions = self._mobsf_permissions(report)
        ats = self._first_present(report, "ats_analysis", "app_transport_security") or info.get("NSAppTransportSecurity") or {}
        url_schemes = self._mobsf_url_schemes(report, info)
        return {
            "CFBundleIdentifier": self._first_present(report, "bundle_id", "packagename") or info.get("CFBundleIdentifier"),
            "CFBundleShortVersionString": self._first_present(report, "version", "app_version") or info.get("CFBundleShortVersionString"),
            "CFBundleVersion": self._first_present(report, "build", "build_number") or info.get("CFBundleVersion"),
            "CFBundleDisplayName": self._first_present(report, "app_name", "name") or info.get("CFBundleDisplayName"),
            "CFBundleExecutable": info.get("CFBundleExecutable"),
            "permissions": permissions,
            "url_schemes": url_schemes,
            "app_transport_security": ats,
        }

    def _mobsf_inventory(self, report: dict[str, Any]) -> dict[str, Any]:
        file_items = self._mobsf_file_items(report)
        frameworks = sorted({path for path in file_items if ".framework" in path})
        plugins = sorted({path for path in file_items if ".appex" in path or "/PlugIns/" in path})
        suffixes: Counter[str] = Counter(Path(path).suffix.lower() or "<none>" for path in file_items)
        resource_samples = [path for path in file_items if self._is_interesting_resource(Path(path))][:50]
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

    def _mobsf_file_items(self, report: dict[str, Any]) -> list[str]:
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
                        path = self._first_present(item, "file_path", "path", "name")
                        if path:
                            candidates.append(str(path))
        return sorted(set(candidates))

    def _mobsf_permissions(self, report: dict[str, Any]) -> list[str]:
        permissions = report.get("permissions") or report.get("permission_analysis") or {}
        if isinstance(permissions, dict):
            return sorted(str(key) for key in permissions.keys())
        if isinstance(permissions, list):
            return sorted(str(item.get("permission") or item.get("name") or item) for item in permissions)
        return []

    def _mobsf_url_schemes(self, report: dict[str, Any], info: dict[str, Any]) -> list[str]:
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

    def _extract_mobsf_sensitive_findings(self, report: dict[str, Any], reveal_values: bool) -> list[dict[str, Any]]:
        text = json.dumps(report, sort_keys=True, ensure_ascii=False)
        findings = self._classify_sensitive_string("mobsf_report_json", text, reveal_values)
        for key, value in self._flatten_value(report):
            if len(findings) >= 100:
                break
            if isinstance(value, str) and self._key_looks_sensitive(key) and self._value_looks_secret(value):
                findings.append(self._sensitive_finding("mobsf_report_json", "SENSITIVE_KEY_NAME", value, self._sensitive_key_severity(key), key_path=key, reveal_values=reveal_values))
        return self._dedupe_sensitive_findings(findings[:100])

    def _extract_mobsf_findings(self, report: dict[str, Any]) -> list[dict[str, Any]]:
        findings: list[dict[str, Any]] = []

        def walk(value: Any, path: str, depth: int = 0) -> None:
            if len(findings) >= 100 or depth > 7:
                return
            if isinstance(value, dict):
                severity = self._normalize_mobsf_severity(self._first_present(value, "severity", "risk", "level", "cvss", "owasp"))
                title = self._first_present(value, "title", "issue", "name", "description", "rule", "check")
                if title and severity != "INFO":
                    evidence = self._first_present(value, "file", "path", "component", "details", "message", "description") or path
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
        return self._sort_flags_by_severity(deduped)

    def _mobsf_critical_findings(self, summary: dict[str, Any], inventory: dict[str, Any]) -> dict[str, Any]:
        info = summary["info_plist"]
        findings = summary.get("mobsf_findings") or []
        sensitive_findings = summary.get("sensitive_information_findings") or []
        api_key_reuse_tests = summary.get("api_key_reuse_test", {}).get("results") or []
        flags: list[dict[str, Any]] = [
            {
                "id": "IPA_PACKAGE_ANALYZABLE",
                "severity": "HIGH",
                "title": "IPA package can be acquired and analyzed with MobSF",
                "evidence": [
                    f"Bundle: {summary.get('app_bundle')}",
                    f"Bundle ID: {summary.get('bundle_id')}",
                    f"MobSF hash: {summary.get('mobsf', {}).get('hash')}",
                ],
                "recommendation": "Treat the IPA as inspectable. Do not embed secrets or rely on client-side obscurity for security decisions.",
            }
        ]
        high_medium = [item for item in findings if item.get("severity") in {"HIGH", "MEDIUM"}]
        if high_medium:
            flags.append(
                {
                    "id": "MOBSF_STATIC_ANALYSIS_FINDINGS",
                    "severity": self._highest_severity(high_medium),
                    "title": "MobSF reported static-analysis findings",
                    "evidence": [f"{item['severity']} {item['title']}: {item.get('evidence')}" for item in high_medium[:15]],
                    "recommendation": "Review the full MobSF report and remediate high-confidence issues based on exploitability and app context.",
                }
            )
        if sensitive_findings:
            flags.append(
                {
                    "id": "SENSITIVE_INFORMATION_EXPOSURE",
                    "severity": self._highest_severity(sensitive_findings),
                    "title": "Potential sensitive information is present in MobSF report data",
                    "evidence": [
                        f"{item['severity']} {item['match_type']} in {item['path']}: {item.get('key_path') or item.get('context') or ''} = {item['reported_value']}"
                        for item in sensitive_findings[:15]
                    ],
                    "recommendation": "Remove credentials and long-lived secrets from the client bundle. Rotate exposed values if they are sensitive, and move privileged operations server-side.",
                }
            )
        permissions = info.get("permissions") or []
        if permissions:
            flags.append(
                {
                    "id": "SENSITIVE_CAPABILITY_DISCLOSURE",
                    "severity": "MEDIUM",
                    "title": "MobSF reported sensitive capability usage",
                    "evidence": permissions[:15],
                    "recommendation": "Confirm each permission is required and purpose strings do not reveal unnecessary implementation detail.",
                }
            )
        if api_key_reuse_tests:
            flags.append(self._api_key_reuse_flag(api_key_reuse_tests))
        return {
            "app": {
                "display_name": summary.get("display_name"),
                "bundle_id": summary.get("bundle_id"),
                "version": info.get("CFBundleShortVersionString"),
                "build": info.get("CFBundleVersion"),
                "ipa_sha256": summary.get("ipa_sha256"),
            },
            "status": "FLAGGED" if flags else "NO_CRITICAL_FINDINGS",
            "flag_count": len(flags),
            "highest_severity": self._highest_severity(flags),
            "flags": self._sort_flags_by_severity(flags),
            "owasp_reference": "https://mas.owasp.org/MASTG/techniques/ios/MASTG-TECH-0058/",
        }

    def _first_present(self, data: dict[str, Any], *keys: str) -> Any:
        for key in keys:
            value = data.get(key)
            if value not in (None, "", [], {}):
                return value
        return None

    def _normalize_mobsf_severity(self, value: Any) -> str:
        text = str(value or "").strip().lower()
        if text in {"high", "critical", "danger", "severe"} or text.startswith("high"):
            return "HIGH"
        if text in {"medium", "warning", "warn", "moderate"} or text.startswith("medium"):
            return "MEDIUM"
        if text in {"low", "secure", "passed"} or text.startswith("low"):
            return "LOW"
        return "INFO"

    def _analyze_package(self, app_dir: Path, acquisition: ArtifactAcquisitionResult, binary_inspection, risk_config: dict[str, Any]) -> dict[str, Any]:
        info = read_info_plist(app_dir)
        inventory = self._inventory(app_dir)
        sensitive_config = risk_config.get("sensitive_scan") or {}
        reveal_sensitive_values = bool(sensitive_config.get("reveal_values", False))
        api_key_reuse_config = risk_config.get("api_key_reuse_test") or {}
        api_key_reuse_enabled = bool(api_key_reuse_config.get("enabled", False))
        permissions = sorted(k for k in info if k.endswith("UsageDescription"))
        url_schemes = self._url_schemes(info)
        ats = info.get("NSAppTransportSecurity") or {}
        findings = [
            "IPA can be acquired and unpacked for local static analysis.",
            "Analyst can inspect Info.plist metadata, code signature metadata, frameworks, plugins, resources, and the app binary.",
        ]
        if permissions:
            findings.append(f"Info.plist exposes permission purpose strings: {', '.join(permissions)}")
        if url_schemes:
            findings.append(f"Info.plist exposes custom URL schemes: {', '.join(url_schemes)}")
        if ats:
            findings.append("Info.plist exposes App Transport Security configuration.")
        if inventory["counts"]["frameworks"] > 0:
            findings.append(f"Package includes {inventory['counts']['frameworks']} framework/native library bundle(s).")
        if inventory["counts"]["plugins"] > 0:
            findings.append(f"Package includes {inventory['counts']['plugins']} app extension/plugin bundle(s).")
        if binary_inspection.status == "PROTECTED_OR_ENCRYPTED_BINARY":
            findings.append("Main executable appears protected/encrypted, but metadata and bundled resources remain analyzable.")
        sensitive_findings = self._scan_sensitive_information(app_dir, inventory, reveal_sensitive_values)
        if sensitive_findings:
            findings.append(f"Potential sensitive information found in bundled resources: {len(sensitive_findings)} finding(s).")
        api_key_reuse_tests = self._test_google_api_key_reuse(sensitive_findings, api_key_reuse_config, reveal_sensitive_values) if api_key_reuse_enabled else []
        if api_key_reuse_tests:
            reusable = sum(1 for item in api_key_reuse_tests if item["status"] == "REUSABLE_FROM_WORKSTATION")
            findings.append(f"Google API key external reuse test completed: {reusable}/{len(api_key_reuse_tests)} key(s) appeared reusable from this workstation.")
        public_sensitive_findings = self._public_sensitive_findings(sensitive_findings)

        summary = {
            "analysis_provider": "builtin",
            "app_bundle": app_dir.name,
            "bundle_id": acquisition.bundle_id or info.get("CFBundleIdentifier"),
            "display_name": acquisition.display_name or info.get("CFBundleDisplayName") or info.get("CFBundleName"),
            "executable_name": acquisition.executable_name or info.get("CFBundleExecutable"),
            "ipa_sha256": acquisition.input_sha256,
            "binary_inspection": binary_inspection.to_dict(),
            "info_plist": {
                "CFBundleIdentifier": info.get("CFBundleIdentifier"),
                "CFBundleShortVersionString": info.get("CFBundleShortVersionString"),
                "CFBundleVersion": info.get("CFBundleVersion"),
                "CFBundleDisplayName": info.get("CFBundleDisplayName"),
                "CFBundleExecutable": info.get("CFBundleExecutable"),
                "permissions": permissions,
                "url_schemes": url_schemes,
                "app_transport_security": ats,
            },
            "counts": inventory["counts"],
            "findings": findings,
            "sensitive_information_findings": public_sensitive_findings,
            "sensitive_scan": {
                "enabled": True,
                "reveal_values": reveal_sensitive_values,
            },
            "api_key_reuse_test": {
                "enabled": api_key_reuse_enabled,
                "provider": api_key_reuse_config.get("provider", "google_geocode"),
                "results": api_key_reuse_tests,
            },
            "mobsf_demonstration": {
                "tool": "Mobile Security Framework (MobSF)",
                "manual_steps": [
                    "Start MobSF on the workstation.",
                    "Open MobSF in a local web browser.",
                    "Upload the acquired IPA from acquired_ipa.",
                    "Review the generated static analysis report for metadata, permissions, frameworks, strings, and binary findings.",
                ],
                "acquired_ipa": str(acquisition.ipa_path) if acquisition.ipa_path else None,
                "owasp_reference": "https://mas.owasp.org/MASTG/techniques/ios/MASTG-TECH-0058/",
            },
        }
        critical_findings = self._critical_findings(summary, inventory)
        return {"summary": summary, "inventory": inventory, "critical_findings": critical_findings}

    def _critical_findings(self, summary: dict[str, Any], inventory: dict[str, Any]) -> dict[str, Any]:
        info = summary["info_plist"]
        permissions = info.get("permissions") or []
        url_schemes = info.get("url_schemes") or []
        ats = info.get("app_transport_security") or {}
        binary = summary["binary_inspection"]
        candidate_resources = self._candidate_config_resources(inventory)
        sensitive_findings = summary.get("sensitive_information_findings") or []
        api_key_reuse_tests = summary.get("api_key_reuse_test", {}).get("results") or []
        flags: list[dict[str, Any]] = [
            {
                "id": "IPA_PACKAGE_ANALYZABLE",
                "severity": "HIGH",
                "title": "IPA package can be acquired and unpacked for static analysis",
                "evidence": [
                    f"Bundle: {summary['app_bundle']}",
                    f"Bundle ID: {summary['bundle_id']}",
                    f"Files inventoried: {summary['counts']['files']}",
                ],
                "recommendation": "Treat the IPA as inspectable. Do not embed secrets or rely on client-side obscurity for security decisions.",
            }
        ]
        if binary.get("status") == "PROTECTED_OR_ENCRYPTED_BINARY":
            flags.append(
                {
                    "id": "ENCRYPTED_EXECUTABLE_METADATA_STILL_EXPOSED",
                    "severity": "MEDIUM",
                    "title": "Main executable is encrypted, but metadata and bundled resources remain analyzable",
                    "evidence": [f"cryptid: {binary.get('cryptid')}", "Info.plist and bundle resources were still inventoried."],
                    "recommendation": "Continue reviewing bundled resources for sensitive configuration; binary encryption does not protect plist/resource metadata.",
                }
            )
        elif binary.get("status") == "MUTABLE_AS_PROVIDED":
            flags.append(
                {
                    "id": "UNENCRYPTED_MAIN_EXECUTABLE",
                    "severity": "HIGH",
                    "title": "Main executable appears analyzable as provided",
                    "evidence": [f"Executable: {summary['executable_name']}", f"Inspection status: {binary.get('status')}"],
                    "recommendation": "Assume reverse engineering of the executable is practical. Keep sensitive logic server-side and add tamper/repackaging controls where appropriate.",
                }
            )
        if permissions:
            flags.append(
                {
                    "id": "SENSITIVE_CAPABILITY_DISCLOSURE",
                    "severity": "MEDIUM",
                    "title": "Info.plist exposes sensitive capability usage",
                    "evidence": permissions,
                    "recommendation": "Confirm each permission is required and purpose strings do not reveal unnecessary implementation detail.",
                }
            )
        if url_schemes:
            flags.append(
                {
                    "id": "CUSTOM_URL_SCHEMES_EXPOSED",
                    "severity": "MEDIUM",
                    "title": "Custom URL schemes are exposed",
                    "evidence": url_schemes,
                    "recommendation": "Review deep-link handlers for authentication, authorization, input validation, and unsafe routing assumptions.",
                }
            )
        if ats:
            severity = "HIGH" if ats.get("NSAllowsArbitraryLoads") is True else "LOW"
            flags.append(
                {
                    "id": "ATS_CONFIGURATION_EXPOSED",
                    "severity": severity,
                    "title": "App Transport Security configuration is exposed",
                    "evidence": [json.dumps(ats, sort_keys=True)],
                    "recommendation": "Avoid broad ATS exceptions. Verify any local-network or domain exceptions are intentional and documented.",
                }
            )
        if summary["counts"]["frameworks"] > 0:
            flags.append(
                {
                    "id": "NATIVE_FRAMEWORKS_PRESENT",
                    "severity": "LOW",
                    "title": "Native framework dependencies are visible",
                    "evidence": inventory.get("frameworks", [])[:10],
                    "recommendation": "Review third-party/native framework exposure and ensure vulnerable SDK versions are not bundled.",
                }
            )
        if summary["counts"]["plugins"] > 0:
            flags.append(
                {
                    "id": "APP_EXTENSIONS_PRESENT",
                    "severity": "MEDIUM",
                    "title": "App extensions/plugins are present",
                    "evidence": inventory.get("plugins", [])[:10],
                    "recommendation": "Review extension entitlements, app group sharing, and extension-specific attack surfaces.",
                }
            )
        if candidate_resources:
            flags.append(
                {
                    "id": "CANDIDATE_CONFIG_RESOURCES",
                    "severity": "MEDIUM",
                    "title": "Candidate configuration resources are bundled",
                    "evidence": candidate_resources[:15],
                    "recommendation": "Review bundled config resources for API keys, environment names, endpoints, feature flags, and client-side assumptions.",
                }
            )
        if sensitive_findings:
            flags.append(
                {
                    "id": "SENSITIVE_INFORMATION_EXPOSURE",
                    "severity": self._highest_severity(sensitive_findings),
                    "title": "Potential sensitive information is present in bundled resources",
                    "evidence": [
                        f"{item['severity']} {item['match_type']} in {item['path']}: {item.get('key_path') or item.get('context') or ''} = {item['reported_value']}"
                        for item in sensitive_findings[:15]
                    ],
                    "recommendation": "Remove credentials and long-lived secrets from the client bundle. Rotate exposed values if they are sensitive, and move privileged operations server-side.",
                }
            )
        if api_key_reuse_tests:
            flags.append(self._api_key_reuse_flag(api_key_reuse_tests))

        return {
            "app": {
                "display_name": summary["display_name"],
                "bundle_id": summary["bundle_id"],
                "version": info.get("CFBundleShortVersionString"),
                "build": info.get("CFBundleVersion"),
                "ipa_sha256": summary["ipa_sha256"],
            },
            "status": "FLAGGED" if flags else "NO_CRITICAL_FINDINGS",
            "flag_count": len(flags),
            "highest_severity": self._highest_severity(flags),
            "flags": self._sort_flags_by_severity(flags),
            "owasp_reference": "https://mas.owasp.org/MASTG/techniques/ios/MASTG-TECH-0058/",
        }

    def _critical_markdown(self, report: dict[str, Any]) -> str:
        app = report["app"]
        lines = [
            "# ios-feature-01-risk-01 Critical Findings",
            "",
            f"- App: {app.get('display_name') or ''}",
            f"- Bundle ID: {app.get('bundle_id') or ''}",
            f"- Version: {app.get('version') or ''} ({app.get('build') or ''})",
            f"- Highest severity: {report['highest_severity']}",
            f"- Flag count: {report['flag_count']}",
            "",
            "| Severity | Finding | Evidence |",
            "| --- | --- | --- |",
        ]
        for flag in self._sort_flags_by_severity(report["flags"]):
            evidence_items = self._sort_evidence_by_embedded_severity(flag.get("evidence", []))
            evidence = "<br>".join(str(item) for item in evidence_items[:5])
            lines.append(f"| {flag['severity']} | {flag['title']} | {evidence} |")
        lines.append("")
        return "\n".join(lines)

    def _scan_sensitive_information(self, app_dir: Path, inventory: dict[str, Any], reveal_values: bool) -> list[dict[str, Any]]:
        findings: list[dict[str, Any]] = []
        for item in inventory.get("files", []):
            if len(findings) >= 100:
                break
            relative = Path(str(item.get("path", "")))
            if self._is_sensitive_scan_excluded(relative):
                continue
            path = app_dir / relative
            suffix = str(item.get("suffix", ""))
            if suffix in {".plist", ".xcprivacy"}:
                findings.extend(self._scan_plist_for_sensitive_values(path, relative, reveal_values))
            elif suffix in {".json", ".xml", ".strings", ".txt", ".js", ".jsbundle", ".env", ".properties", ".yaml", ".yml", ".html"}:
                findings.extend(self._scan_text_for_sensitive_values(path, relative, reveal_values))
        return findings[:100]

    def _scan_plist_for_sensitive_values(self, path: Path, relative: Path, reveal_values: bool) -> list[dict[str, Any]]:
        try:
            data = read_plist(path)
        except Exception:
            return []
        findings: list[dict[str, Any]] = []
        for key_path, value in self._flatten_value(data):
            if len(findings) >= 25:
                break
            if isinstance(value, str):
                classified = self._classify_sensitive_string(str(relative), value, reveal_values, key_path=key_path)
                findings.extend(classified)
                if not classified and self._key_looks_sensitive(key_path) and self._value_looks_secret(value):
                    findings.append(self._sensitive_finding(str(relative), "SENSITIVE_KEY_NAME", value, self._sensitive_key_severity(key_path), key_path=key_path, reveal_values=reveal_values))
        return self._dedupe_sensitive_findings(findings)

    def _scan_text_for_sensitive_values(self, path: Path, relative: Path, reveal_values: bool) -> list[dict[str, Any]]:
        max_size = 5 * 1024 * 1024
        try:
            if path.stat().st_size > max_size:
                return []
            data = path.read_bytes()
            if b"\x00" in data[:4096]:
                return []
            text = data.decode("utf-8", errors="ignore")
        except Exception:
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
            if self._value_looks_secret(value):
                findings.append(self._sensitive_finding(str(relative), "SENSITIVE_KEY_VALUE", value, self._sensitive_key_severity(match.group(1)), context=match.group(1), reveal_values=reveal_values))
        findings.extend(self._classify_sensitive_string(str(relative), text, reveal_values))
        return self._dedupe_sensitive_findings(findings[:50])

    def _classify_sensitive_string(self, path: str, text: str, reveal_values: bool, key_path: str | None = None) -> list[dict[str, Any]]:
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
                findings.append(self._sensitive_finding(path, match_type, match.group(0), severity, key_path=key_path, reveal_values=reveal_values))
                if len(findings) >= 10:
                    return findings
        return findings

    def _test_google_api_key_reuse(self, sensitive_findings: list[dict[str, Any]], config: dict[str, Any], reveal_values: bool) -> list[dict[str, Any]]:
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

        results: list[dict[str, Any]] = []
        for key, finding in keys:
            result = self._test_google_geocode_key(key, timeout_seconds, test_address)
            result.update(
                {
                    "path": finding.get("path"),
                    "key_path": finding.get("key_path"),
                    "match_type": finding.get("match_type"),
                    "masked_key": self._mask_secret(key),
                    "reported_key": key if reveal_values else self._mask_secret(key),
                    "key_revealed": reveal_values,
                    "severity": self._api_key_reuse_status_severity(result["status"]),
                }
            )
            results.append(result)
        return results

    def _test_google_geocode_key(self, api_key: str, timeout_seconds: float, address: str) -> dict[str, Any]:
        query = urllib.parse.urlencode({"address": address, "key": api_key})
        url = f"https://maps.googleapis.com/maps/api/geocode/json?{query}"
        request = urllib.request.Request(url, headers={"User-Agent": "mobile-playbook-automation/0.1"})
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                body = response.read(64 * 1024)
                status_code = int(getattr(response, "status", 200))
        except urllib.error.HTTPError as exc:
            status_code = exc.code
            body = exc.read(64 * 1024)
        except Exception as exc:
            return {
                "provider": "google_geocode",
                "status": "NETWORK_OR_TEST_ERROR",
                "http_status": None,
                "google_status": None,
                "message": str(exc),
            }

        try:
            payload = json.loads(body.decode("utf-8", errors="replace"))
        except Exception:
            payload = {}
        google_status = str(payload.get("status") or "")
        error_message = str(payload.get("error_message") or "")
        return {
            "provider": "google_geocode",
            "status": self._classify_google_api_key_reuse_response(status_code, google_status, error_message),
            "http_status": status_code,
            "google_status": google_status or None,
            "message": error_message or f"HTTP {status_code}",
        }

    def _classify_google_api_key_reuse_response(self, http_status: int, google_status: str, message: str) -> str:
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

    def _api_key_reuse_flag(self, results: list[dict[str, Any]]) -> dict[str, Any]:
        severity = self._highest_severity(results)
        reusable_count = sum(1 for item in results if item.get("status") == "REUSABLE_FROM_WORKSTATION")
        evidence = [
            f"{item['severity']} {item['status']} via {item['provider']} for {item.get('path')}: key={item['reported_key']}, google_status={item.get('google_status')}, message={item.get('message')}"
            for item in results[:10]
        ]
        return {
            "id": "GOOGLE_API_KEY_REUSE_TEST",
            "severity": severity,
            "title": "Google API key external reuse test",
            "evidence": evidence,
            "recommendation": (
                "Restrict Google API keys to the iOS bundle ID and Apple Team ID, restrict allowed APIs, and review Firebase/Google Cloud usage logs."
                if reusable_count
                else "Review key restrictions in Google Cloud Console. A denied low-impact test does not prove every enabled API is protected."
            ),
        }

    def _api_key_reuse_status_severity(self, status: str) -> str:
        if status == "REUSABLE_FROM_WORKSTATION":
            return "HIGH"
        if status in {"ACCEPTED_BUT_SERVICE_BLOCKED", "INCONCLUSIVE", "NETWORK_OR_TEST_ERROR"}:
            return "MEDIUM"
        return "LOW"

    def _sensitive_finding(
        self,
        path: str,
        match_type: str,
        value: str,
        severity: str,
        *,
        reveal_values: bool,
        key_path: str | None = None,
        context: str | None = None,
    ) -> dict[str, Any]:
        masked = self._mask_secret(value)
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

    def _public_sensitive_findings(self, findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
        public_findings = []
        for finding in findings:
            public = dict(finding)
            public.pop("_raw_value", None)
            public_findings.append(public)
        return public_findings

    def _flatten_value(self, value: Any, prefix: str = "$") -> list[tuple[str, Any]]:
        if isinstance(value, dict):
            flattened: list[tuple[str, Any]] = []
            for key, child in value.items():
                flattened.extend(self._flatten_value(child, f"{prefix}.{key}"))
            return flattened
        if isinstance(value, list):
            flattened = []
            for index, child in enumerate(value):
                flattened.extend(self._flatten_value(child, f"{prefix}.{index}"))
            return flattened
        return [(prefix, value)]

    def _key_looks_sensitive(self, key: str) -> bool:
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

    def _value_looks_secret(self, value: str) -> bool:
        stripped = value.strip()
        if len(stripped) < 6:
            return False
        common_false_positives = {"true", "false", "null", "none", "production", "staging", "development"}
        return stripped.lower() not in common_false_positives

    def _sensitive_key_severity(self, key: str) -> str:
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

    def _mask_secret(self, value: str) -> str:
        if len(value) <= 8:
            return "*" * len(value)
        if len(value) <= 16:
            return f"{value[:2]}...{value[-2:]}"
        return f"{value[:4]}...{value[-4:]}"

    def _dedupe_sensitive_findings(self, findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
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

    def _is_sensitive_scan_excluded(self, relative_path: Path) -> bool:
        if any(part in {"_CodeSignature", "Frameworks", "PlugIns", "SC_Info"} for part in relative_path.parts):
            return True
        if relative_path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".car", ".nib", ".otf", ".ttf", ".bin"}:
            return True
        return False

    def _candidate_config_resources(self, inventory: dict[str, Any]) -> list[str]:
        interesting_terms = ("config", "firebase", "google", "service", "secret", "token", "key", "credential", "endpoint", "environment")
        interesting_suffixes = {".plist", ".json", ".xml", ".strings", ".jsbundle", ".db", ".sqlite"}
        candidates: list[str] = []
        for item in inventory.get("files", []):
            path = str(item.get("path", ""))
            suffix = str(item.get("suffix", ""))
            lower = path.lower()
            if suffix not in interesting_suffixes:
                continue
            if any(term in lower for term in interesting_terms):
                candidates.append(path)
        return sorted(candidates)

    def _highest_severity(self, flags: list[dict[str, Any]]) -> str:
        if not flags:
            return "NONE"
        return max((flag["severity"] for flag in flags), key=self._severity_rank)

    def _sort_flags_by_severity(self, flags: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(flags, key=lambda flag: self._severity_rank(str(flag.get("severity", ""))), reverse=True)

    def _sort_evidence_by_embedded_severity(self, evidence: list[Any]) -> list[Any]:
        return sorted(evidence, key=lambda item: self._severity_rank(self._embedded_severity(str(item))), reverse=True)

    def _embedded_severity(self, value: str) -> str:
        first_word = value.strip().split(" ", 1)[0].upper()
        return first_word if first_word in {"HIGH", "MEDIUM", "LOW", "INFO"} else "NONE"

    def _severity_rank(self, severity: str) -> int:
        order = {"HIGH": 3, "MEDIUM": 2, "LOW": 1, "INFO": 0, "NONE": -1}
        return order.get(severity.upper(), -1)

    def _inventory(self, app_dir: Path) -> dict[str, Any]:
        files: list[dict[str, Any]] = []
        suffixes: Counter[str] = Counter()
        frameworks: set[str] = set()
        plugins: set[str] = set()
        resource_samples: list[str] = []
        for path in sorted(p for p in app_dir.rglob("*") if p.is_file()):
            rel = path.relative_to(app_dir)
            suffix = path.suffix.lower() or "<none>"
            suffixes[suffix] += 1
            parts = rel.parts
            if "Frameworks" in parts:
                framework = next((part for part in parts if part.endswith(".framework")), None)
                if framework:
                    frameworks.add(framework)
            if "PlugIns" in parts:
                plugin = next((part for part in parts if part.endswith(".appex")), None)
                if plugin:
                    plugins.add(plugin)
            if len(resource_samples) < 50 and self._is_interesting_resource(rel):
                resource_samples.append(str(rel))
            files.append(
                {
                    "path": str(rel),
                    "size": path.stat().st_size,
                    "suffix": suffix,
                }
            )
        return {
            "counts": {
                "files": len(files),
                "frameworks": len(frameworks),
                "plugins": len(plugins),
                "resource_samples": len(resource_samples),
            },
            "suffix_counts": dict(sorted(suffixes.items())),
            "frameworks": sorted(frameworks),
            "plugins": sorted(plugins),
            "resource_samples": resource_samples,
            "files": files,
        }

    def _is_interesting_resource(self, relative_path: Path) -> bool:
        if any(part in {"_CodeSignature", "Frameworks", "PlugIns", "SC_Info"} for part in relative_path.parts):
            return False
        return relative_path.suffix.lower() in {".plist", ".json", ".strings", ".xml", ".txt", ".jsbundle", ".html", ".sqlite", ".db"}

    def _url_schemes(self, info: dict[str, Any]) -> list[str]:
        schemes: list[str] = []
        for item in info.get("CFBundleURLTypes") or []:
            schemes.extend(str(value) for value in item.get("CFBundleURLSchemes") or [])
        return sorted(set(schemes))

    def _artifact_status_to_final(self, status: str) -> str:
        mapping = {
            "ARTIFACT_REQUIRED": "ARTIFACT_REQUIRED",
            "ARTIFACT_NOT_FOUND": "ARTIFACT_NOT_FOUND",
            "ARTIFACT_INVALID": "ARTIFACT_INVALID",
            "ARTIFACT_BUNDLE_ID_MISMATCH": "ARTIFACT_BUNDLE_ID_MISMATCH",
            "ORIGINAL_APP_NOT_INSTALLED": "ORIGINAL_APP_NOT_INSTALLED",
            "UNSUPPORTED_ARTIFACT_SOURCE": "UNSUPPORTED_ARTIFACT_SOURCE",
        }
        return mapping.get(status, "ARTIFACT_ACQUISITION_FAILED")
