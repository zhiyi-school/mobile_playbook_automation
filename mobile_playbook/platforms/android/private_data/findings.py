"""
Decides the verdict from the scanned private data files and writes the findings and critical findings reports.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from pathlib import Path
from typing import Any

from mobile_playbook.platforms.android.models import AndroidRiskRunResult
from mobile_playbook.platforms.android.private_data.scan import ScannedFile
from mobile_playbook.platforms.static_analysis.findings import critical_markdown, critical_report, sensitive_information_flag
from mobile_playbook.platforms.static_analysis.sensitive_findings import dedupe_sensitive_findings, public_sensitive_findings

logger = logging.getLogger(__name__)

OWASP_REFERENCE = "https://mas.owasp.org/MASTG/tests/android/MASVS-STORAGE/MASTG-TEST-0001/"
MAX_FINDINGS = 200
DEFAULT_MARKDOWN_TITLE = "Android Private Data Critical Findings"


# Decide the verdict from the scanned files and write the findings and critical findings reports, listing the
# caller's flag for how the data was read first.
def record_scan(
    files: list[ScannedFile],
    access_flag: dict[str, Any],
    app_config,
    report_dir: Path,
    result: AndroidRiskRunResult,
    markdown_title: str = DEFAULT_MARKDOWN_TITLE,
) -> None:
    findings = dedupe_sensitive_findings([item for scanned in files if not scanned.sdk for item in scanned.findings])[:MAX_FINDINGS]
    sdk_findings = dedupe_sensitive_findings([item for scanned in files if scanned.sdk for item in scanned.findings])[:MAX_FINDINGS]
    sdk_files = [scanned for scanned in files if scanned.sdk]
    protected = [scanned.path for scanned in files if scanned.protected]
    databases = [scanned.path for scanned in files if scanned.category == "sqlite"]
    if findings:
        result.final_status, result.verdict = "PRIVATE_DATA_EXPOSED", "At Risk"
    elif protected:
        result.final_status, result.verdict = "PRIVATE_DATA_PROTECTED", "Reduced Risk"
    else:
        result.final_status, result.verdict = "PRIVATE_DATA_NO_FINDINGS", "Inconclusive"
    result.metadata["file_count"] = len(files)
    result.metadata["categories"] = dict(Counter(scanned.category for scanned in files))
    result.metadata["finding_count"] = len(findings)
    result.metadata["sdk_finding_count"] = len(sdk_findings)
    result.metadata["sdk_files"] = [{"path": scanned.path, "sdk": scanned.sdk} for scanned in sdk_files][:50]
    result.metadata["protected_files"] = protected[:50]
    result.metadata["unencrypted_databases"] = databases[:50]
    logger.debug("%s[%s]: %s files, %s findings, %s protected -> %s", result.risk_id, app_config.id, len(files), len(findings), len(protected), result.final_status)

    findings_path = report_dir / "private_data_findings.json"
    findings_path.write_text(json.dumps({"files": [scanned.to_dict() for scanned in files], "findings": public_sensitive_findings(findings), "sdk_findings": public_sensitive_findings(sdk_findings)}, indent=2, sort_keys=True))
    result.evidence.append({"kind": "private_data_findings", "path": str(findings_path), "label": "Files read from the private data directories and their findings"})

    flags = [access_flag]
    if findings:
        flags.append(sensitive_information_flag(findings, "the app's private data directory"))
    if sdk_findings:
        flags.append(
            {
                "id": "SDK_DATA_IN_PLAIN_TEXT",
                "severity": "LOW",
                "title": "Third-party SDKs keep tokens or identifiers in plain text in the private data directory",
                "evidence": [f"{scanned.sdk}: {scanned.path} ({len(scanned.findings)} finding(s))" for scanned in sdk_files if scanned.findings][:15],
                "recommendation": "These values are written by third-party SDKs rather than the app's own code and do not decide the verdict. Check that they grant no access beyond the SDK's own service.",
            }
        )
    if databases:
        flags.append(
            {
                "id": "UNENCRYPTED_DATABASES",
                "severity": "LOW",
                "title": "SQLite databases in the private data directory are not encrypted",
                "evidence": databases[:15],
                "recommendation": "Encrypt databases that hold sensitive data, for example with SQLCipher and a Keystore-protected key.",
            }
        )
    write_critical_findings(flags, app_config, report_dir, result, markdown_title)


# Write the critical findings report as JSON and Markdown and add both to the result's evidence.
def write_critical_findings(flags: list[dict[str, Any]], app_config, report_dir: Path, result: AndroidRiskRunResult, markdown_title: str = DEFAULT_MARKDOWN_TITLE) -> None:
    app = {"id": app_config.id, "name": app_config.name, "package_name": app_config.package_name}
    report = critical_report(app, flags, OWASP_REFERENCE)
    critical_path = report_dir / "critical_findings.json"
    critical_path.write_text(json.dumps(report, indent=2, sort_keys=True))
    markdown_path = report_dir / "critical_findings.md"
    markdown_path.write_text(critical_markdown(report, markdown_title, [f"- App: {app_config.name}", f"- Package: {app_config.package_name}"]))
    result.evidence.append({"kind": "critical_findings", "path": str(critical_path), "label": "Critical findings"})
    result.evidence.append({"kind": "critical_findings", "path": str(markdown_path), "label": "Critical findings (Markdown)"})
