"""
Builtin static analysis of an unpacked IPA: Info.plist metadata, package inventory and a secret scan of bundled resources.
"""

from __future__ import annotations

import logging
from collections import Counter
from pathlib import Path
from typing import Any

from mobile_playbook.platforms.ios.ipa.plist import read_info_plist, read_plist
from mobile_playbook.platforms.ios.models import ArtifactAcquisitionResult
from mobile_playbook.platforms.ios.static_analysis.critical_findings import critical_findings
from mobile_playbook.platforms.static_analysis.sensitive_findings import (
    classify_sensitive_string,
    dedupe_sensitive_findings,
    flatten_value,
    key_looks_sensitive,
    reveal_sensitive_values,
    scan_text_for_sensitive_values,
    sensitive_finding,
    sensitive_key_severity,
    sensitive_summary_fields,
    sensitive_summary_sentences,
    value_looks_secret,
)

logger = logging.getLogger(__name__)


# Analyze an unpacked app bundle and return its summary, file inventory and critical findings.
def analyze_package(
    app_dir: Path,
    acquisition: ArtifactAcquisitionResult,
    binary_inspection,
    risk_config: dict[str, Any],
) -> dict[str, Any]:
    logger.debug("ios ipa inventory: analyzing package %s", app_dir)
    info = read_info_plist(app_dir)
    logger.debug(
        "ios ipa inventory: Info.plist keys=%d CFBundleIdentifier=%s CFBundleShortVersionString=%s CFBundleVersion=%s CFBundleExecutable=%s",
        len(info), info.get("CFBundleIdentifier"), info.get("CFBundleShortVersionString"), info.get("CFBundleVersion"),
        info.get("CFBundleExecutable"),
    )
    package_inventory = inventory(app_dir)
    permissions = sorted(k for k in info if k.endswith("UsageDescription"))
    schemes = url_schemes(info)
    ats = info.get("NSAppTransportSecurity") or {}
    logger.debug("ios ipa inventory: permissions=%s url_schemes=%s ats_present=%s", permissions, schemes, bool(ats))
    findings = [
        "IPA can be acquired and unpacked for local static analysis.",
        "Analyst can inspect Info.plist metadata, code signature metadata, frameworks, plugins, resources, and the app binary.",
    ]
    if permissions:
        findings.append(f"Info.plist exposes permission purpose strings: {', '.join(permissions)}")
    if schemes:
        findings.append(f"Info.plist exposes custom URL schemes: {', '.join(schemes)}")
    if ats:
        findings.append("Info.plist exposes App Transport Security configuration.")
    if package_inventory["counts"]["frameworks"] > 0:
        findings.append(f"Package includes {package_inventory['counts']['frameworks']} framework/native library bundle(s).")
    if package_inventory["counts"]["plugins"] > 0:
        findings.append(f"Package includes {package_inventory['counts']['plugins']} app extension/plugin bundle(s).")
    if binary_inspection.status == "PROTECTED_OR_ENCRYPTED_BINARY":
        findings.append("Main executable appears protected/encrypted, but metadata and bundled resources remain analyzable.")
    logger.debug("ios ipa inventory: binary inspection status=%s", binary_inspection.status)
    sensitive_findings = scan_sensitive_information(app_dir, package_inventory, risk_config)
    sensitive_fields = sensitive_summary_fields(sensitive_findings, risk_config)
    findings.extend(sensitive_summary_sentences(sensitive_fields, "bundled resources"))

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
            "url_schemes": schemes,
            "app_transport_security": ats,
        },
        "counts": package_inventory["counts"],
        "findings": findings,
        **sensitive_fields,
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
    logger.debug("ios ipa inventory: summary findings=%d public_sensitive_findings=%d", len(findings), len(sensitive_fields["sensitive_information_findings"]))
    return {"summary": summary, "inventory": package_inventory, "critical_findings": critical_findings(summary, package_inventory)}


# Walk the app bundle and return file, suffix, framework, plugin and resource-sample inventories.
def inventory(app_dir: Path) -> dict[str, Any]:
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
        if len(resource_samples) < 50 and is_interesting_resource(rel):
            resource_samples.append(str(rel))
        files.append(
            {
                "path": str(rel),
                "size": path.stat().st_size,
                "suffix": suffix,
            }
        )
    logger.debug(
        "ios ipa inventory: %s files=%d frameworks=%d plugins=%d resource_samples=%d suffixes=%d",
        app_dir, len(files), len(frameworks), len(plugins), len(resource_samples), len(suffixes),
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


# Return whether a bundle file is a text-like resource outside signature, framework and plugin dirs.
def is_interesting_resource(relative_path: Path) -> bool:
    if any(part in {"_CodeSignature", "Frameworks", "PlugIns", "SC_Info"} for part in relative_path.parts):
        return False
    return relative_path.suffix.lower() in {".plist", ".json", ".strings", ".xml", ".txt", ".jsbundle", ".html", ".sqlite", ".db"}


# Return the sorted unique custom URL schemes declared in Info.plist.
def url_schemes(info: dict[str, Any]) -> list[str]:
    schemes: list[str] = []
    for item in info.get("CFBundleURLTypes") or []:
        schemes.extend(str(value) for value in item.get("CFBundleURLSchemes") or [])
    return sorted(set(schemes))


# Scan inventoried plist and text resources for secrets, capped at 100 findings.
def scan_sensitive_information(app_dir: Path, inventory: dict[str, Any], risk_config: dict[str, Any]) -> list[dict[str, Any]]:
    reveal_values = reveal_sensitive_values(risk_config)
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


# Return whether a bundle file is in a signature, framework or plugin dir or is a binary asset.
def is_sensitive_scan_excluded(relative_path: Path) -> bool:
    if any(part in {"_CodeSignature", "Frameworks", "PlugIns", "SC_Info"} for part in relative_path.parts):
        return True
    if relative_path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".car", ".nib", ".otf", ".ttf", ".bin"}:
        return True
    return False
