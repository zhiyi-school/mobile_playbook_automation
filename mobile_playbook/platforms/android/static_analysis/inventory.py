"""
Builtin static analysis of an app's APKs: manifest summary, file inventory read straight from the zip archives, and a
secret scan of bundled text files, DEX code and compiled resources.
"""

from __future__ import annotations

import logging
import zipfile
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any

from mobile_playbook.platforms.android.apk_tools import manifest_tree
from mobile_playbook.platforms.android.static_analysis.critical_findings import critical_findings
from mobile_playbook.platforms.android.static_analysis.manifest import parse_manifest_tree
from mobile_playbook.platforms.static_analysis.sensitive_findings import (
    MAX_TEXT_SCAN_BYTES,
    classify_sensitive_string,
    dedupe_sensitive_findings,
    reveal_sensitive_values,
    scan_text_content,
    sensitive_summary_fields,
    sensitive_summary_sentences,
)

logger = logging.getLogger(__name__)

TEXT_SUFFIXES = frozenset({".json", ".xml", ".txt", ".js", ".html", ".properties", ".yaml", ".yml", ".env", ".cfg", ".conf", ".ini"})
# DEX code and compiled resources are binary, so only the fixed secret patterns are matched in them.
PATTERN_SCAN_MAX_BYTES = 64 * 1024 * 1024
MAX_FINDINGS = 100


# Analyze the app's APKs (base first, then splits) and return the summary, file inventory and critical findings.
def analyze_apk(apks: list[Path], apk_info: dict[str, Any], risk_config: dict[str, Any]) -> dict[str, Any]:
    base_apk = apks[0]
    logger.debug("android apk inventory: analyzing %s APK(s), base %s", len(apks), base_apk)
    manifest = read_manifest(base_apk)
    package_inventory = inventory(apks)
    findings = [
        "APK can be obtained and unpacked for local static analysis.",
        "Analyst can inspect the manifest, DEX bytecode, native libraries, resources and bundled assets.",
    ]
    if manifest.get("dangerous_permissions"):
        findings.append(f"Manifest requests dangerous runtime permissions: {', '.join(manifest['dangerous_permissions'])}")
    exported = sum((manifest.get("exported_counts") or {}).values())
    if exported:
        findings.append(f"Manifest exports {exported} component(s) other apps can reach without a permission.")
    if package_inventory["counts"]["native_libraries"]:
        findings.append(f"Package includes {package_inventory['counts']['native_libraries']} native library file(s) for {', '.join(package_inventory['abis'])}.")
    sensitive_findings = scan_sensitive_information(apks, risk_config)
    sensitive_fields = sensitive_summary_fields(sensitive_findings, risk_config)
    findings.extend(sensitive_summary_sentences(sensitive_fields, "bundled files and DEX code"))

    summary = {
        "analysis_provider": "builtin",
        **apk_summary_fields(apk_info),
        "manifest": manifest,
        "counts": package_inventory["counts"],
        "findings": findings,
        **sensitive_fields,
        "mobsf_demonstration": {
            "tool": "Mobile Security Framework (MobSF)",
            "manual_steps": [
                "Start MobSF on the workstation.",
                "Open MobSF in a local web browser.",
                "Upload the base APK from base_apk.",
                "Review the generated static analysis report for manifest, permission, code, network security and certificate findings.",
            ],
            "base_apk": str(base_apk),
        },
    }
    logger.debug("android apk inventory: summary findings=%d sensitive=%d", len(findings), len(sensitive_fields["sensitive_information_findings"]))
    return {"summary": summary, "inventory": package_inventory, "critical_findings": critical_findings(summary, package_inventory)}


# Return the summary fields that identify the analyzed APKs.
def apk_summary_fields(apk_info: dict[str, Any]) -> dict[str, Any]:
    return {
        "package_name": apk_info.get("package_name"),
        "display_name": apk_info.get("display_name"),
        "version_name": apk_info.get("version_name"),
        "version_code": apk_info.get("version_code"),
        "base_apk": apk_info.get("base_apk"),
        "apk_sha256": apk_info.get("apk_sha256"),
        "apks": apk_info.get("apks") or [],
    }


# Parse the base APK's manifest, or record why it could not be read when no SDK tool can dump it.
def read_manifest(base_apk: Path) -> dict[str, Any]:
    try:
        manifest = parse_manifest_tree(manifest_tree(base_apk))
    except (RuntimeError, ValueError, OSError) as exc:
        logger.debug("android apk inventory: manifest of %s unavailable: %s", base_apk, exc, exc_info=True)
        return {"status": "UNAVAILABLE", "reason": str(exc)}
    return {"status": "PARSED", **manifest}


# List every file in the APKs with its size and suffix, and group DEX files, native libraries, assets and resources.
def inventory(apks: list[Path]) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    suffixes: Counter[str] = Counter()
    dex_files: list[str] = []
    native_libraries: set[str] = set()
    abis: set[str] = set()
    resource_samples: list[str] = []
    for apk in apks:
        with zipfile.ZipFile(apk) as archive:
            for entry in archive.infolist():
                if entry.is_dir():
                    continue
                path = PurePosixPath(entry.filename)
                suffix = path.suffix.lower() or "<none>"
                suffixes[suffix] += 1
                files.append({"apk": apk.name, "path": entry.filename, "size": entry.file_size, "suffix": suffix})
                if suffix == ".dex":
                    dex_files.append(f"{apk.name}!/{entry.filename}")
                if len(path.parts) == 3 and path.parts[0] == "lib" and suffix == ".so":
                    abis.add(path.parts[1])
                    native_libraries.add(path.name)
                if len(resource_samples) < 50 and is_interesting_resource(path):
                    resource_samples.append(entry.filename)
    logger.debug(
        "android apk inventory: files=%d dex=%d native_libraries=%d abis=%s resource_samples=%d",
        len(files), len(dex_files), len(native_libraries), sorted(abis), len(resource_samples),
    )
    return {
        "counts": {
            "apks": len(apks),
            "files": len(files),
            "dex_files": len(dex_files),
            "native_libraries": len(native_libraries),
            "assets": sum(1 for item in files if item["path"].startswith("assets/")),
            "resource_samples": len(resource_samples),
        },
        "suffix_counts": dict(sorted(suffixes.items())),
        "dex_files": dex_files,
        "abis": sorted(abis),
        "native_libraries": sorted(native_libraries),
        "resource_samples": resource_samples,
        "files": files,
    }


# Return whether an APK entry is a readable text resource worth reviewing: a text file outside signing metadata
# and compiled res/ XML, or anything under res/raw/.
def is_interesting_resource(path: PurePosixPath) -> bool:
    if not path.parts or path.parts[0] == "META-INF":
        return False
    if path.parts[0] == "res" and (len(path.parts) < 2 or not path.parts[1].startswith("raw")):
        return False
    return path.suffix.lower() in TEXT_SUFFIXES or path.parts[:2] == ("res", "raw")


# Scan the APKs for secrets: text resources for key-value pairs and patterns, DEX code and resources.arsc for the
# fixed secret patterns, capped at 100 findings.
def scan_sensitive_information(apks: list[Path], risk_config: dict[str, Any]) -> list[dict[str, Any]]:
    reveal_values = reveal_sensitive_values(risk_config)
    findings: list[dict[str, Any]] = []
    scanned: Counter[str] = Counter()
    for apk in apks:
        with zipfile.ZipFile(apk) as archive:
            for entry in archive.infolist():
                if len(findings) >= MAX_FINDINGS:
                    logger.debug("android sensitive scan: finding cap of %s reached; stopping", MAX_FINDINGS)
                    return findings[:MAX_FINDINGS]
                if entry.is_dir():
                    continue
                path = PurePosixPath(entry.filename)
                label = entry.filename if apk == apks[0] else f"{apk.name}!/{entry.filename}"
                if is_interesting_resource(path) and entry.file_size <= MAX_TEXT_SCAN_BYTES:
                    scanned["text"] += 1
                    findings.extend(scan_text_content(archive.read(entry), label, reveal_values))
                elif (path.suffix == ".dex" or entry.filename == "resources.arsc") and entry.file_size <= PATTERN_SCAN_MAX_BYTES:
                    scanned["pattern"] += 1
                    text = archive.read(entry).decode("utf-8", errors="ignore")
                    findings.extend(dedupe_sensitive_findings(classify_sensitive_string(label, text, reveal_values)))
                else:
                    scanned["skipped"] += 1
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "android sensitive scan: entries by kind=%s; %s finding(s), by type=%s",
            dict(scanned), len(findings), dict(Counter(item.get("match_type") for item in findings)),
        )
    return findings[:MAX_FINDINGS]
