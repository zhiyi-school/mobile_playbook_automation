"""
Turns a MobSF IPA report into the framework's standard IPA analysis summary, package inventory and critical findings.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from pathlib import Path
from typing import Any

from mobile_playbook.platforms.ios.models import ArtifactAcquisitionResult
from mobile_playbook.platforms.ios.static_analysis.critical_findings import mobsf_critical_findings
from mobile_playbook.platforms.ios.static_analysis.inventory import is_interesting_resource
from mobile_playbook.platforms.static_analysis import mobsf_client
from mobile_playbook.platforms.static_analysis.mobsf_client import (
    extract_mobsf_findings,
    first_present,
    mobsf_file_items,
    mobsf_permissions,
)
from mobile_playbook.platforms.static_analysis.sensitive_findings import (
    extract_mobsf_sensitive_findings,
    reveal_sensitive_values,
    sensitive_summary_fields,
    sensitive_summary_sentences,
)

logger = logging.getLogger(__name__)


# Scan the IPA with MobSF and normalize the report into the standard analysis summary and findings.
def analyze_with_mobsf(
    ipa_path: Path,
    acquisition: ArtifactAcquisitionResult,
    binary_inspection,
    risk_config: dict[str, Any],
) -> dict[str, Any]:
    analyzer_config = risk_config.get("analyzer") or {}
    logger.debug("ios mobsf: analyzing %s (bundle_id=%s, analyzer_config_keys=%s)", ipa_path, acquisition.bundle_id, sorted(str(key) for key in analyzer_config))
    started = time.monotonic()
    mobsf = mobsf_client.mobsf_scan(ipa_path, analyzer_config)
    report = mobsf["report"]
    logger.debug("ios mobsf: scan finished in %.2fs, hash=%s, report_keys=%s", time.monotonic() - started, mobsf.get("hash"), len(report) if isinstance(report, dict) else 0)
    sensitive_findings = extract_mobsf_sensitive_findings(report, reveal_sensitive_values(risk_config))
    sensitive_fields = sensitive_summary_fields(sensitive_findings, risk_config)
    findings = extract_mobsf_findings(report)
    info_plist = mobsf_info_plist(report)
    mobsf_package_inventory = mobsf_inventory(report)
    logger.debug("ios mobsf: %s normalized finding(s), inventory counts=%s", len(findings), mobsf_package_inventory["counts"])
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
        **sensitive_fields,
    }
    if findings:
        high_or_medium = sum(1 for item in findings if item.get("severity") in {"HIGH", "MEDIUM"})
        summary["findings"].append(f"MobSF reported {len(findings)} normalized finding(s), including {high_or_medium} high/medium finding(s).")
    summary["findings"].extend(sensitive_summary_sentences(sensitive_fields, "MobSF report data"))
    logger.debug("ios mobsf: summary findings=%s", summary["findings"])
    return {
        "summary": summary,
        "inventory": mobsf_package_inventory,
        "critical_findings": mobsf_critical_findings(summary, mobsf_package_inventory),
        "mobsf_raw_report": report,
    }


# Build the standard Info.plist summary from a MobSF report.
def mobsf_info_plist(report: dict[str, Any]) -> dict[str, Any]:
    info = report.get("info_plist") if isinstance(report.get("info_plist"), dict) else {}
    permissions = mobsf_permissions(report)
    ats = first_present(report, "ats_analysis", "app_transport_security") or info.get("NSAppTransportSecurity") or {}
    schemes = mobsf_url_schemes(report, info)
    logger.debug("ios mobsf: info_plist present=%s permissions=%s url_schemes=%s ats_present=%s", bool(info), len(permissions), schemes, bool(ats))
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


# Build the standard package inventory from the file paths in a MobSF report.
def mobsf_inventory(report: dict[str, Any]) -> dict[str, Any]:
    file_items = mobsf_file_items(report)
    frameworks = sorted({path for path in file_items if ".framework" in path})
    plugins = sorted({path for path in file_items if ".appex" in path or "/PlugIns/" in path})
    suffixes: Counter[str] = Counter(Path(path).suffix.lower() or "<none>" for path in file_items)
    resource_samples = [path for path in file_items if is_interesting_resource(Path(path))][:50]
    logger.debug(
        "ios mobsf: inventory files=%s frameworks=%s plugins=%s resource_samples=%s",
        len(file_items),
        len(frameworks),
        len(plugins),
        len(resource_samples),
    )
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


# Return sorted non-HTTP URL schemes from the MobSF report and Info.plist.
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
