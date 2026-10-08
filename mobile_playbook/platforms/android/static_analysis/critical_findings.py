"""
Builds severity-ranked critical findings from APK analysis summaries and renders them as Markdown.
"""

from __future__ import annotations

import logging
from typing import Any

from mobile_playbook.platforms.android.static_analysis.manifest import unprotected_exported_components
from mobile_playbook.platforms.static_analysis import findings as shared_findings
from mobile_playbook.platforms.static_analysis.findings import (
    api_key_reuse_flag,
    critical_report,
    mobsf_findings_flag,
    sensitive_information_flag,
)

logger = logging.getLogger(__name__)

OWASP_REFERENCE = "https://mas.owasp.org/MASTG/techniques/android/MASTG-TECH-0007/"
API_KEY_RESTRICTION = "the app's package name and signing certificate SHA-1"
CONFIG_TERMS = ("config", "firebase", "google", "service", "secret", "token", "key", "credential", "endpoint", "environment")


# Build the severity-sorted critical findings report from a MobSF-backed analysis summary.
def mobsf_critical_findings(summary: dict[str, Any], inventory: dict[str, Any]) -> dict[str, Any]:
    findings = summary.get("mobsf_findings") or []
    sensitive_findings = summary.get("sensitive_information_findings") or []
    api_key_reuse_tests = summary.get("api_key_reuse_test", {}).get("results") or []
    logger.debug(
        "android critical findings (mobsf): inputs findings=%s sensitive=%s api_key_reuse_tests=%s",
        len(findings), len(sensitive_findings), len(api_key_reuse_tests),
    )
    flags: list[dict[str, Any]] = [
        {
            "id": "APK_PACKAGE_ANALYZABLE",
            "severity": "HIGH",
            "title": "APK package can be obtained and analyzed with MobSF",
            "evidence": [
                f"APK: {summary.get('base_apk')}",
                f"Package: {summary.get('package_name')}",
                f"MobSF hash: {summary.get('mobsf', {}).get('hash')}",
            ],
            "recommendation": "Treat the APK as inspectable. Do not embed secrets or rely on client-side obscurity for security decisions.",
        }
    ]
    mobsf_flag = mobsf_findings_flag(findings)
    if mobsf_flag is not None:
        flags.append(mobsf_flag)
    if sensitive_findings:
        flags.append(sensitive_information_flag(sensitive_findings, "MobSF report data"))
    flags.extend(manifest_flags(summary.get("manifest") or {}))
    flags.extend(native_library_flags(inventory))
    if api_key_reuse_tests:
        flags.append(api_key_reuse_flag(api_key_reuse_tests, API_KEY_RESTRICTION))
    return critical_report(_app(summary), flags, OWASP_REFERENCE)


# Build the severity-sorted critical findings report from the builtin package analysis.
def critical_findings(summary: dict[str, Any], inventory: dict[str, Any]) -> dict[str, Any]:
    sensitive_findings = summary.get("sensitive_information_findings") or []
    api_key_reuse_tests = summary.get("api_key_reuse_test", {}).get("results") or []
    candidate_resources = candidate_config_resources(inventory)
    logger.debug(
        "android critical findings: inputs counts=%s manifest_status=%s candidate_resources=%s sensitive=%s api_key_reuse_tests=%s",
        summary.get("counts"), (summary.get("manifest") or {}).get("status"), len(candidate_resources),
        len(sensitive_findings), len(api_key_reuse_tests),
    )
    flags: list[dict[str, Any]] = [
        {
            "id": "APK_PACKAGE_ANALYZABLE",
            "severity": "HIGH",
            "title": "APK package can be obtained and unpacked for static analysis",
            "evidence": [
                f"APK: {summary.get('base_apk')}",
                f"Package: {summary.get('package_name')}",
                f"Files inventoried: {summary['counts']['files']}",
            ],
            "recommendation": "Treat the APK as inspectable. Do not embed secrets or rely on client-side obscurity for security decisions.",
        }
    ]
    flags.extend(manifest_flags(summary.get("manifest") or {}))
    flags.extend(native_library_flags(inventory))
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
        flags.append(sensitive_information_flag(sensitive_findings, "bundled files and DEX code"))
    if api_key_reuse_tests:
        flags.append(api_key_reuse_flag(api_key_reuse_tests, API_KEY_RESTRICTION))
    return critical_report(_app(summary), flags, OWASP_REFERENCE)


# Build the flags the manifest supports: dangerous permissions, components exported without a permission, a
# debuggable build, cleartext traffic and backups; fields the analyzer could not read add no flag.
def manifest_flags(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    flags: list[dict[str, Any]] = []
    application = manifest.get("application") or {}
    target_sdk = manifest.get("target_sdk")
    if manifest.get("dangerous_permissions"):
        flags.append(
            {
                "id": "DANGEROUS_PERMISSIONS_REQUESTED",
                "severity": "MEDIUM",
                "title": "Manifest requests dangerous runtime permissions",
                "evidence": manifest["dangerous_permissions"][:15],
                "recommendation": "Confirm each runtime permission is required, requested in context, and degrades safely when denied.",
            }
        )
    exported = [f"{item['type']} {item['name']}" for item in unprotected_exported_components(manifest.get("components") or [])]
    if not exported:
        exported = [f"{component_type}: {count}" for component_type, count in (manifest.get("exported_counts") or {}).items() if count]
    if exported:
        flags.append(
            {
                "id": "EXPORTED_COMPONENTS",
                "severity": "MEDIUM",
                "title": "Components are exported without a permission",
                "evidence": exported[:15],
                "recommendation": "Set android:exported=\"false\" where other apps need no access, or guard exported components with a signature permission and validate every incoming intent.",
            }
        )
    if application.get("debuggable") is True:
        flags.append(
            {
                "id": "DEBUGGABLE_BUILD",
                "severity": "HIGH",
                "title": "Application is marked debuggable",
                "evidence": ['android:debuggable="true" on <application>'],
                "recommendation": "Ship release builds without android:debuggable; a debuggable app lets anyone with adb read its private data and attach a debugger.",
            }
        )
    cleartext = application.get("uses_cleartext_traffic")
    if cleartext is True or (cleartext is None and isinstance(target_sdk, int) and target_sdk < 28 and manifest.get("status") == "PARSED"):
        flags.append(
            {
                "id": "CLEARTEXT_TRAFFIC_ALLOWED",
                "severity": "MEDIUM",
                "title": "Cleartext HTTP traffic is allowed",
                "evidence": [
                    'android:usesCleartextTraffic="true" on <application>' if cleartext else f"usesCleartextTraffic unset with targetSdkVersion {target_sdk} (cleartext allowed by default below 28)",
                    f"Network security config present: {bool(application.get('network_security_config'))}",
                ],
                "recommendation": "Disable cleartext traffic and use a network security config that limits any exceptions to named domains.",
            }
        )
    allow_backup = application.get("allow_backup")
    if allow_backup is True or (allow_backup is None and manifest.get("status") == "PARSED"):
        flags.append(
            {
                "id": "APP_DATA_BACKUP_ALLOWED",
                "severity": "LOW",
                "title": "App data can be included in backups",
                "evidence": ['android:allowBackup="true" on <application>' if allow_backup else "android:allowBackup is unset (backups default to allowed)"],
                "recommendation": "Set android:allowBackup=\"false\" or exclude sensitive files with backup rules.",
            }
        )
    return flags


# Build the flag listing bundled native libraries, which need a native reverse-engineering and patching review.
def native_library_flags(inventory: dict[str, Any]) -> list[dict[str, Any]]:
    libraries = inventory.get("native_libraries") or []
    if not libraries:
        return []
    return [
        {
            "id": "NATIVE_LIBRARIES_PRESENT",
            "severity": "LOW",
            "title": "Native libraries are visible",
            "evidence": libraries[:10],
            "recommendation": "Review bundled native and third-party libraries for known-vulnerable versions and embedded secrets.",
        }
    ]


# Render an APK critical findings report as a Markdown summary and findings table.
def critical_markdown(report: dict[str, Any]) -> str:
    app = report["app"]
    return shared_findings.critical_markdown(
        report,
        "android-feature-01-risk-01 Critical Findings",
        [
            f"- App: {app.get('display_name') or ''}",
            f"- Package: {app.get('package_name') or ''}",
            f"- Version: {app.get('version') or ''} ({app.get('build') or ''})",
        ],
    )


# Return sorted text resources whose name suggests configuration or secrets.
def candidate_config_resources(inventory: dict[str, Any]) -> list[str]:
    candidates = sorted(path for path in inventory.get("resource_samples") or [] if any(term in path.lower() for term in CONFIG_TERMS))
    logger.debug("android critical findings: %s candidate config resource(s)", len(candidates))
    return candidates


# Return the app identity block of a critical findings report.
def _app(summary: dict[str, Any]) -> dict[str, Any]:
    manifest = summary.get("manifest") or {}
    return {
        "display_name": summary.get("display_name"),
        "package_name": summary.get("package_name") or manifest.get("package_name"),
        "version": summary.get("version_name") or manifest.get("version_name"),
        "build": summary.get("version_code") or manifest.get("version_code"),
        "apk_sha256": summary.get("apk_sha256"),
    }
