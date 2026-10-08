"""
Builds severity-ranked critical findings from IPA analysis summaries and renders them as Markdown.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from mobile_playbook.platforms.static_analysis import findings as shared_findings
from mobile_playbook.platforms.static_analysis.findings import (
    api_key_reuse_flag,
    critical_report,
    mobsf_findings_flag,
    sensitive_information_flag,
)

logger = logging.getLogger(__name__)

OWASP_REFERENCE = "https://mas.owasp.org/MASTG/techniques/ios/MASTG-TECH-0058/"
API_KEY_RESTRICTION = "the iOS bundle ID and Apple Team ID"


# Build the severity-sorted critical findings report from a MobSF-backed analysis summary.
def mobsf_critical_findings(summary: dict[str, Any], inventory: dict[str, Any]) -> dict[str, Any]:
    info = summary["info_plist"]
    findings = summary.get("mobsf_findings") or []
    sensitive_findings = summary.get("sensitive_information_findings") or []
    api_key_reuse_tests = summary.get("api_key_reuse_test", {}).get("results") or []
    logger.debug(
        "ios critical findings (mobsf): inputs findings=%s sensitive=%s permissions=%s api_key_reuse_tests=%s",
        len(findings),
        len(sensitive_findings),
        len(info.get("permissions") or []),
        len(api_key_reuse_tests),
    )
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
    mobsf_flag = mobsf_findings_flag(findings)
    if mobsf_flag is not None:
        flags.append(mobsf_flag)
    if sensitive_findings:
        flags.append(sensitive_information_flag(sensitive_findings, "MobSF report data"))
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
        flags.append(api_key_reuse_flag(api_key_reuse_tests, API_KEY_RESTRICTION))
    app = {
        "display_name": summary.get("display_name"),
        "bundle_id": summary.get("bundle_id"),
        "version": info.get("CFBundleShortVersionString"),
        "build": info.get("CFBundleVersion"),
        "ipa_sha256": summary.get("ipa_sha256"),
    }
    return critical_report(app, flags, OWASP_REFERENCE)


# Build the severity-sorted critical findings report from the builtin package analysis.
def critical_findings(summary: dict[str, Any], inventory: dict[str, Any]) -> dict[str, Any]:
    info = summary["info_plist"]
    permissions = info.get("permissions") or []
    url_schemes = info.get("url_schemes") or []
    ats = info.get("app_transport_security") or {}
    binary = summary["binary_inspection"]
    candidate_resources = candidate_config_resources(inventory)
    sensitive_findings = summary.get("sensitive_information_findings") or []
    api_key_reuse_tests = summary.get("api_key_reuse_test", {}).get("results") or []
    logger.debug(
        "ios critical findings: inputs binary_status=%s permissions=%s url_schemes=%s ats_present=%s allows_arbitrary_loads=%s",
        binary.get("status"),
        len(permissions),
        len(url_schemes),
        bool(ats),
        ats.get("NSAllowsArbitraryLoads") if isinstance(ats, dict) else None,
    )
    logger.debug(
        "ios critical findings: inputs counts=%s candidate_resources=%s sensitive=%s api_key_reuse_tests=%s",
        summary.get("counts"),
        len(candidate_resources),
        len(sensitive_findings),
        len(api_key_reuse_tests),
    )
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
        flags.append(sensitive_information_flag(sensitive_findings, "bundled resources"))
    if api_key_reuse_tests:
        flags.append(api_key_reuse_flag(api_key_reuse_tests, API_KEY_RESTRICTION))
    app = {
        "display_name": summary["display_name"],
        "bundle_id": summary["bundle_id"],
        "version": info.get("CFBundleShortVersionString"),
        "build": info.get("CFBundleVersion"),
        "ipa_sha256": summary["ipa_sha256"],
    }
    return critical_report(app, flags, OWASP_REFERENCE)


# Render an IPA critical findings report as a Markdown summary and findings table.
def critical_markdown(report: dict[str, Any]) -> str:
    app = report["app"]
    return shared_findings.critical_markdown(
        report,
        "ios-feature-01-risk-01 Critical Findings",
        [
            f"- App: {app.get('display_name') or ''}",
            f"- Bundle ID: {app.get('bundle_id') or ''}",
            f"- Version: {app.get('version') or ''} ({app.get('build') or ''})",
        ],
    )


# Return sorted bundle files whose suffix and name suggest configuration or secrets.
def candidate_config_resources(inventory: dict[str, Any]) -> list[str]:
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
    logger.debug("ios critical findings: %s candidate config resource(s)", len(candidates))
    return sorted(candidates)
