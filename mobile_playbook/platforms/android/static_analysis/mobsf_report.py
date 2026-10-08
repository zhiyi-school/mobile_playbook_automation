"""
Turns a MobSF APK report into the framework's standard APK analysis summary, package inventory and critical findings.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any

from mobile_playbook.platforms.android.static_analysis.critical_findings import mobsf_critical_findings
from mobile_playbook.platforms.android.static_analysis.inventory import apk_summary_fields, is_interesting_resource
from mobile_playbook.platforms.android.static_analysis.manifest import DANGEROUS_PERMISSIONS
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

# MobSF's exported_count keys, mapped to the manifest component tags the builtin analyzer counts under.
EXPORTED_COUNT_KEYS = {
    "exported_activities": "activity",
    "exported_services": "service",
    "exported_receivers": "receiver",
    "exported_providers": "provider",
}


# Scan the base APK with MobSF and normalize the report into the standard analysis summary and findings.
def analyze_apk_with_mobsf(base_apk: Path, apk_info: dict[str, Any], risk_config: dict[str, Any]) -> dict[str, Any]:
    analyzer_config = risk_config.get("analyzer") or {}
    logger.debug("android mobsf: analyzing %s (package=%s)", base_apk, apk_info.get("package_name"))
    started = time.monotonic()
    mobsf = mobsf_client.mobsf_scan(base_apk, analyzer_config)
    report = mobsf["report"]
    logger.debug("android mobsf: scan finished in %.2fs, hash=%s, report_keys=%s", time.monotonic() - started, mobsf.get("hash"), len(report) if isinstance(report, dict) else 0)
    sensitive_findings = extract_mobsf_sensitive_findings(report, reveal_sensitive_values(risk_config))
    sensitive_fields = sensitive_summary_fields(sensitive_findings, risk_config)
    findings = extract_mobsf_findings(report)
    manifest = mobsf_manifest(report)
    package_inventory = mobsf_inventory(report)
    logger.debug("android mobsf: %s normalized finding(s), inventory counts=%s", len(findings), package_inventory["counts"])
    identity = apk_summary_fields(apk_info)
    summary = {
        "analysis_provider": "mobsf",
        **identity,
        "package_name": manifest["package_name"] or identity["package_name"],
        "display_name": first_present(report, "app_name") or identity["display_name"],
        "version_name": manifest["version_name"] or identity["version_name"],
        "version_code": manifest["version_code"] or identity["version_code"],
        "manifest": manifest,
        "counts": package_inventory["counts"],
        "findings": [
            "MobSF static analysis completed for the base APK.",
            "MobSF report was normalized into the framework's standard APK analysis reports.",
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
    logger.debug("android mobsf: summary findings=%s", summary["findings"])
    return {
        "summary": summary,
        "inventory": package_inventory,
        "critical_findings": mobsf_critical_findings(summary, package_inventory),
        "mobsf_raw_report": report,
    }


# Build the manifest summary from a MobSF APK report: identity, SDK levels, permissions and exported component counts.
def mobsf_manifest(report: dict[str, Any]) -> dict[str, Any]:
    permissions = mobsf_permissions(report)
    statuses = report.get("permissions") if isinstance(report.get("permissions"), dict) else {}
    dangerous = sorted(name for name in permissions if _is_dangerous(name, statuses.get(name)))
    exported_raw = report.get("exported_count") if isinstance(report.get("exported_count"), dict) else {}
    exported_counts = {tag: _as_int(exported_raw.get(key)) for key, tag in EXPORTED_COUNT_KEYS.items()}
    manifest = {
        "status": "FROM_MOBSF",
        "package_name": first_present(report, "package_name", "packagename"),
        "version_name": _as_text(first_present(report, "version_name")),
        "version_code": _as_text(first_present(report, "version_code")),
        "min_sdk": _as_int(first_present(report, "min_sdk")) or None,
        "target_sdk": _as_int(first_present(report, "target_sdk")) or None,
        "main_activity": first_present(report, "main_activity"),
        "permissions": permissions,
        "dangerous_permissions": dangerous,
        "exported_counts": exported_counts,
    }
    logger.debug(
        "android mobsf: manifest package=%s target_sdk=%s permissions=%s dangerous=%s exported=%s",
        manifest["package_name"], manifest["target_sdk"], len(permissions), len(dangerous), exported_counts,
    )
    return manifest


# Build the standard package inventory from the file paths in a MobSF report.
def mobsf_inventory(report: dict[str, Any]) -> dict[str, Any]:
    file_items = mobsf_file_items(report)
    paths = [PurePosixPath(item) for item in file_items]
    dex_files = [str(path) for path in paths if path.suffix == ".dex"]
    native = [path for path in paths if len(path.parts) >= 3 and path.parts[-3] == "lib" and path.suffix == ".so"]
    resource_samples = [str(path) for path in paths if is_interesting_resource(path)][:50]
    suffixes: Counter[str] = Counter(path.suffix.lower() or "<none>" for path in paths)
    logger.debug("android mobsf: inventory files=%s dex=%s native=%s resource_samples=%s", len(file_items), len(dex_files), len(native), len(resource_samples))
    return {
        "counts": {
            "files": len(file_items),
            "dex_files": len(dex_files),
            "native_libraries": len({path.name for path in native}),
            "assets": sum(1 for path in paths if path.parts[:1] == ("assets",)),
            "resource_samples": len(resource_samples),
        },
        "suffix_counts": dict(sorted(suffixes.items())),
        "dex_files": dex_files,
        "abis": sorted({path.parts[-2] for path in native}),
        "native_libraries": sorted({path.name for path in native}),
        "resource_samples": resource_samples,
        "files": [{"path": item, "suffix": PurePosixPath(item).suffix.lower() or "<none>"} for item in file_items[:5000]],
        "mobsf_report_keys": sorted(report.keys()) if isinstance(report, dict) else [],
    }


# Return whether a permission is a runtime permission, by name or by the protection level MobSF reported for it.
def _is_dangerous(name: str, details: Any) -> bool:
    return name in DANGEROUS_PERMISSIONS or (isinstance(details, dict) and str(details.get("status", "")).lower() == "dangerous")


# Return a report value as an integer, or 0 when it is missing or not numeric.
def _as_int(value: Any) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0


# Return a report value as text, or None when it is missing.
def _as_text(value: Any) -> str | None:
    return None if value in (None, "") else str(value)
