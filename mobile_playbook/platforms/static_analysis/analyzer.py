"""
Chooses between the MobSF and built-in package analyzers for a static analysis risk, and writes the analysis report files.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mobile_playbook.platforms.static_analysis.findings import highest_severity, sort_flags_by_severity

logger = logging.getLogger(__name__)

Analysis = dict[str, Any]


# Run the analyzer the risk config names, falling back to the built-in one when MobSF fails or the name is unknown
# and fallback_to_builtin allows it.
def run_configured_analysis(
    risk_id: str,
    risk_config: dict[str, Any],
    *,
    builtin: Callable[[], Analysis],
    mobsf: Callable[[], Analysis],
) -> Analysis:
    analyzer_config = risk_config.get("analyzer") or {}
    provider = str(analyzer_config.get("provider") or "builtin").strip().lower()
    fallback_to_builtin = bool(analyzer_config.get("fallback_to_builtin", True))
    logger.debug("%s: analyzer provider=%s fallback_to_builtin=%s", risk_id, provider, fallback_to_builtin)

    if provider in {"builtin", "package", "package_inventory", "local"}:
        return builtin()

    if provider in {"mobsf", "mobsf_api"}:
        try:
            return mobsf()
        except Exception as exc:
            logger.debug("%s: MobSF analysis failed: %s", risk_id, exc, exc_info=True)
            if not fallback_to_builtin:
                raise
            logger.debug("%s: falling back to built-in package scanner after MobSF failure", risk_id)
            analysis = builtin()
            analysis["summary"]["mobsf_fallback"] = {
                "used": True,
                "reason": str(exc),
                "configured_provider": provider,
            }
            analysis["summary"]["findings"].append(f"MobSF analysis failed; package scanner fallback was used: {exc}")
            analysis["critical_findings"]["flags"].append(
                {
                    "id": "MOBSF_FALLBACK_USED",
                    "severity": "LOW",
                    "title": "MobSF analysis was unavailable, built-in package scanner was used",
                    "evidence": [str(exc)],
                    "recommendation": f"Start MobSF, verify the API key, and rerun {risk_id} for a MobSF-backed report.",
                }
            )
            analysis["critical_findings"]["flags"] = sort_flags_by_severity(analysis["critical_findings"]["flags"])
            analysis["critical_findings"]["flag_count"] = len(analysis["critical_findings"]["flags"])
            analysis["critical_findings"]["highest_severity"] = highest_severity(analysis["critical_findings"]["flags"])
            return analysis

    if fallback_to_builtin:
        logger.debug("%s: unknown analyzer provider %r; using built-in package scanner", risk_id, provider)
        analysis = builtin()
        analysis["summary"]["analyzer_warning"] = f"Unknown analyzer provider '{provider}', built-in package scanner was used."
        return analysis
    raise ValueError(f"Unknown {risk_id} analyzer provider: {provider}")


# Write the analysis summary, inventory, critical findings (JSON and Markdown) and any raw MobSF report, returning
# each file's path keyed as the risk's result records it; mobsf_report_path is None when MobSF was not used.
def write_analysis_reports(report_dir: Path, analysis: Analysis, analysis_filename: str, markdown: str) -> dict[str, Path | None]:
    paths: dict[str, Path | None] = {
        "analysis_path": report_dir / analysis_filename,
        "inventory_path": report_dir / "package_inventory.json",
        "critical_findings_path": report_dir / "critical_findings.json",
        "critical_findings_markdown_path": report_dir / "critical_findings.md",
        "mobsf_report_path": None,
    }
    paths["analysis_path"].write_text(json.dumps(analysis["summary"], indent=2, sort_keys=True))
    paths["inventory_path"].write_text(json.dumps(analysis["inventory"], indent=2, sort_keys=True))
    paths["critical_findings_path"].write_text(json.dumps(analysis["critical_findings"], indent=2, sort_keys=True))
    paths["critical_findings_markdown_path"].write_text(markdown)
    if analysis.get("mobsf_raw_report") is not None:
        paths["mobsf_report_path"] = report_dir / "mobsf_report.json"
        paths["mobsf_report_path"].write_text(json.dumps(analysis["mobsf_raw_report"], indent=2, sort_keys=True))
    logger.debug("static analysis: wrote reports %s", {key: str(value) if value else None for key, value in paths.items()})
    return paths
