"""
Severity ranking, the critical-finding flags every platform shares, and the Markdown rendering of a critical findings report.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


# Build the flag listing MobSF's high and medium findings, or None when it reported none.
def mobsf_findings_flag(findings: list[dict[str, Any]]) -> dict[str, Any] | None:
    high_medium = [item for item in findings if item.get("severity") in {"HIGH", "MEDIUM"}]
    logger.debug("critical findings: %s high/medium MobSF finding(s)", len(high_medium))
    if not high_medium:
        return None
    return {
        "id": "MOBSF_STATIC_ANALYSIS_FINDINGS",
        "severity": highest_severity(high_medium),
        "title": "MobSF reported static-analysis findings",
        "evidence": [f"{item['severity']} {item['title']}: {item.get('evidence')}" for item in high_medium[:15]],
        "recommendation": "Review the full MobSF report and remediate high-confidence issues based on exploitability and app context.",
    }


# Build the flag listing potential secrets found in `location`, such as "bundled resources".
def sensitive_information_flag(sensitive_findings: list[dict[str, Any]], location: str) -> dict[str, Any]:
    return {
        "id": "SENSITIVE_INFORMATION_EXPOSURE",
        "severity": highest_severity(sensitive_findings),
        "title": f"Potential sensitive information is present in {location}",
        "evidence": [
            f"{item['severity']} {item['match_type']} in {item['path']}: {item.get('key_path') or item.get('context') or ''} = {item['reported_value']}"
            for item in sensitive_findings[:15]
        ],
        "recommendation": "Remove credentials and long-lived secrets from the client bundle. Rotate exposed values if they are sensitive, and move privileged operations server-side.",
    }


# Build the Google API key reuse flag from reuse test results; `restrict_to` names what a key should be locked to.
def api_key_reuse_flag(results: list[dict[str, Any]], restrict_to: str) -> dict[str, Any]:
    severity = highest_severity(results)
    reusable_count = sum(1 for item in results if item.get("status") == "REUSABLE_FROM_WORKSTATION")
    logger.debug("critical findings: api key reuse flag from %s result(s), %s reusable, severity %s", len(results), reusable_count, severity)
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
            f"Restrict Google API keys to {restrict_to}, restrict allowed APIs, and review Firebase/Google Cloud usage logs."
            if reusable_count
            else "Review key restrictions in Google Cloud Console. A denied low-impact test does not prove every enabled API is protected."
        ),
    }


# Wrap severity-sorted flags and the app's identity into a critical findings report.
def critical_report(app: dict[str, Any], flags: list[dict[str, Any]], owasp_reference: str) -> dict[str, Any]:
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "critical findings: %s flag(s) %s, highest severity %s",
            len(flags),
            [(flag["id"], flag["severity"]) for flag in flags],
            highest_severity(flags),
        )
    return {
        "app": app,
        "status": "FLAGGED" if flags else "NO_CRITICAL_FINDINGS",
        "flag_count": len(flags),
        "highest_severity": highest_severity(flags),
        "flags": sort_flags_by_severity(flags),
        "owasp_reference": owasp_reference,
    }


# Render a critical findings report as a Markdown heading, the given app summary lines and a findings table.
def critical_markdown(report: dict[str, Any], title: str, app_lines: list[str]) -> str:
    lines = [
        f"# {title}",
        "",
        *app_lines,
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
    markdown = "\n".join(lines)
    logger.debug("critical markdown: rendered %s flag row(s), %s char(s)", len(report["flags"]), len(markdown))
    return markdown


# Return the highest severity among flags, or NONE when there are none.
def highest_severity(flags: list[dict[str, Any]]) -> str:
    if not flags:
        return "NONE"
    return max((flag["severity"] for flag in flags), key=severity_rank)


# Return flags sorted from highest to lowest severity.
def sort_flags_by_severity(flags: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(flags, key=lambda flag: severity_rank(str(flag.get("severity", ""))), reverse=True)


# Return evidence items sorted by the severity word each starts with, highest first.
def sort_evidence_by_embedded_severity(evidence: list[Any]) -> list[Any]:
    return sorted(evidence, key=lambda item: severity_rank(embedded_severity(str(item))), reverse=True)


# Return the leading severity word of a string, or NONE when it has none.
def embedded_severity(value: str) -> str:
    first_word = value.strip().split(" ", 1)[0].upper()
    return first_word if first_word in {"HIGH", "MEDIUM", "LOW", "INFO"} else "NONE"


# Return a severity's numeric rank, with unknown values ranked lowest.
def severity_rank(severity: str) -> int:
    order = {"HIGH": 3, "MEDIUM": 2, "LOW": 1, "INFO": 0, "NONE": -1}
    return order.get(severity.upper(), -1)
