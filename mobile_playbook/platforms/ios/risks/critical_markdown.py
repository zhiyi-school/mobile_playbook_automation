from __future__ import annotations

import json
from typing import Any


def mobsf_critical_findings(summary: dict[str, Any], inventory: dict[str, Any]) -> dict[str, Any]:
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
                "severity": highest_severity(high_medium),
                "title": "MobSF reported static-analysis findings",
                "evidence": [f"{item['severity']} {item['title']}: {item.get('evidence')}" for item in high_medium[:15]],
                "recommendation": "Review the full MobSF report and remediate high-confidence issues based on exploitability and app context.",
            }
        )
    if sensitive_findings:
        flags.append(
            {
                "id": "SENSITIVE_INFORMATION_EXPOSURE",
                "severity": highest_severity(sensitive_findings),
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
        flags.append(api_key_reuse_flag(api_key_reuse_tests))
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
        "highest_severity": highest_severity(flags),
        "flags": sort_flags_by_severity(flags),
        "owasp_reference": "https://mas.owasp.org/MASTG/techniques/ios/MASTG-TECH-0058/",
    }


def critical_findings(summary: dict[str, Any], inventory: dict[str, Any]) -> dict[str, Any]:
    info = summary["info_plist"]
    permissions = info.get("permissions") or []
    url_schemes = info.get("url_schemes") or []
    ats = info.get("app_transport_security") or {}
    binary = summary["binary_inspection"]
    candidate_resources = candidate_config_resources(inventory)
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
                "severity": highest_severity(sensitive_findings),
                "title": "Potential sensitive information is present in bundled resources",
                "evidence": [
                    f"{item['severity']} {item['match_type']} in {item['path']}: {item.get('key_path') or item.get('context') or ''} = {item['reported_value']}"
                    for item in sensitive_findings[:15]
                ],
                "recommendation": "Remove credentials and long-lived secrets from the client bundle. Rotate exposed values if they are sensitive, and move privileged operations server-side.",
            }
        )
    if api_key_reuse_tests:
        flags.append(api_key_reuse_flag(api_key_reuse_tests))

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
        "highest_severity": highest_severity(flags),
        "flags": sort_flags_by_severity(flags),
        "owasp_reference": "https://mas.owasp.org/MASTG/techniques/ios/MASTG-TECH-0058/",
    }


def critical_markdown(report: dict[str, Any]) -> str:
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
    for flag in sort_flags_by_severity(report["flags"]):
        evidence_items = sort_evidence_by_embedded_severity(flag.get("evidence", []))
        evidence = "<br>".join(str(item) for item in evidence_items[:5])
        lines.append(f"| {flag['severity']} | {flag['title']} | {evidence} |")
    lines.append("")
    return "\n".join(lines)


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
    return sorted(candidates)


def api_key_reuse_flag(results: list[dict[str, Any]]) -> dict[str, Any]:
    severity = highest_severity(results)
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


def highest_severity(flags: list[dict[str, Any]]) -> str:
    if not flags:
        return "NONE"
    return max((flag["severity"] for flag in flags), key=severity_rank)


def sort_flags_by_severity(flags: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(flags, key=lambda flag: severity_rank(str(flag.get("severity", ""))), reverse=True)


def sort_evidence_by_embedded_severity(evidence: list[Any]) -> list[Any]:
    return sorted(evidence, key=lambda item: severity_rank(embedded_severity(str(item))), reverse=True)


def embedded_severity(value: str) -> str:
    first_word = value.strip().split(" ", 1)[0].upper()
    return first_word if first_word in {"HIGH", "MEDIUM", "LOW", "INFO"} else "NONE"


def severity_rank(severity: str) -> int:
    order = {"HIGH": 3, "MEDIUM": 2, "LOW": 1, "INFO": 0, "NONE": -1}
    return order.get(severity.upper(), -1)
