from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from mobile_playbook.platforms.ios.ipa.plist_utils import read_info_plist
from mobile_playbook.platforms.ios.models import ArtifactAcquisitionResult
from mobile_playbook.platforms.ios.risks.critical_markdown import critical_findings
from mobile_playbook.platforms.ios.risks.sensitive_findings import (
    public_sensitive_findings,
    scan_sensitive_information,
    test_google_api_key_reuse,
)


def analyze_package(
    app_dir: Path,
    acquisition: ArtifactAcquisitionResult,
    binary_inspection,
    risk_config: dict[str, Any],
) -> dict[str, Any]:
    info = read_info_plist(app_dir)
    package_inventory = inventory(app_dir)
    sensitive_config = risk_config.get("sensitive_scan") or {}
    reveal_sensitive_values = bool(sensitive_config.get("reveal_values", False))
    api_key_reuse_config = risk_config.get("api_key_reuse_test") or {}
    api_key_reuse_enabled = bool(api_key_reuse_config.get("enabled", False))
    permissions = sorted(k for k in info if k.endswith("UsageDescription"))
    schemes = url_schemes(info)
    ats = info.get("NSAppTransportSecurity") or {}
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
    sensitive_findings = scan_sensitive_information(app_dir, package_inventory, reveal_sensitive_values)
    if sensitive_findings:
        findings.append(f"Potential sensitive information found in bundled resources: {len(sensitive_findings)} finding(s).")
    api_key_reuse_tests = test_google_api_key_reuse(sensitive_findings, api_key_reuse_config, reveal_sensitive_values) if api_key_reuse_enabled else []
    if api_key_reuse_tests:
        reusable = sum(1 for item in api_key_reuse_tests if item["status"] == "REUSABLE_FROM_WORKSTATION")
        findings.append(f"Google API key external reuse test completed: {reusable}/{len(api_key_reuse_tests)} key(s) appeared reusable from this workstation.")
    public_findings = public_sensitive_findings(sensitive_findings)

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
    return {"summary": summary, "inventory": package_inventory, "critical_findings": critical_findings(summary, package_inventory)}


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


def is_interesting_resource(relative_path: Path) -> bool:
    if any(part in {"_CodeSignature", "Frameworks", "PlugIns", "SC_Info"} for part in relative_path.parts):
        return False
    return relative_path.suffix.lower() in {".plist", ".json", ".strings", ".xml", ".txt", ".jsbundle", ".html", ".sqlite", ".db"}


def url_schemes(info: dict[str, Any]) -> list[str]:
    schemes: list[str] = []
    for item in info.get("CFBundleURLTypes") or []:
        schemes.extend(str(value) for value in item.get("CFBundleURLSchemes") or [])
    return sorted(set(schemes))
