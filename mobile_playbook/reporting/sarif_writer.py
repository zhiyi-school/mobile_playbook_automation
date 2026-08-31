from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version as package_version
from pathlib import Path, PurePosixPath
from typing import Any

from mobile_playbook.reporting.run_manifest import is_completed, read_manifest

SARIF_NAME = "results.sarif"
SARIF_VERSION = "2.1.0"
SARIF_SCHEMA = "https://json.schemastore.org/sarif-2.1.0.json"
TOOL_NAME = "Mobile Playbook Automation"
TOOL_INFORMATION_URI = "https://github.com/mobile-playbook/mobile-playbook-automation"
FINGERPRINT_KEY = "mobilePlaybook/v1"
AUTOMATION_ID_PREFIX = "mobile-playbook"

RESULTS_NAME = "dashboard_results.json"
RISK_CONFIG_ROOT = Path("configs/split")
RISK_METADATA_FIELDS = ("name", "description", "goal", "tactic")

FAIL_KIND = "fail"
NO_LEVEL = "none"

VERDICT_KIND: dict[str, str] = {
    "At Risk": FAIL_KIND,
    "Reduced Risk": "pass",
    "Inconclusive": "review",
}
DEFAULT_VERDICT = "Inconclusive"

SEVERITY_LEVEL: dict[str, str] = {
    "critical": "error",
    "high": "error",
    "medium": "warning",
    "low": "note",
    "info": "note",
}
DEFAULT_FAIL_LEVEL = "error"


def verdict_kind_and_level(verdict: str, severity: Any = None) -> tuple[str, str]:
    """SARIF 2.1.0 §3.27.10: only a `fail` result may carry a level other than `none`."""
    kind = VERDICT_KIND.get(verdict, VERDICT_KIND[DEFAULT_VERDICT])
    if kind != FAIL_KIND:
        return kind, NO_LEVEL
    key = str(severity or "").strip().lower()
    return kind, SEVERITY_LEVEL.get(key, DEFAULT_FAIL_LEVEL)


def tool_version() -> str:
    try:
        return package_version("mobile-playbook-automation")
    except PackageNotFoundError:
        return "0.0.0+unknown"


def sarif_path(run_dir: Path) -> Path:
    return Path(run_dir) / SARIF_NAME


def result_fingerprint(app_id: str, test_id: str, test_case_id: str = "", platform: str = "") -> str:
    """Stable across runs: only project identifiers, never times or paths."""
    parts = [str(platform), str(app_id), str(test_id), str(test_case_id)]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def build_sarif(
    rows: Sequence[Mapping[str, Any]],
    *,
    run_timestamp: str,
    manifest: Mapping[str, Any] | None = None,
    rule_metadata: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    ordered = sorted(rows, key=_row_sort_key)
    metadata = rule_metadata if rule_metadata is not None else _default_rule_metadata(ordered)
    rule_ids = sorted({str(row.get("test_id") or "") for row in ordered if row.get("test_id")})
    rule_index = {rule_id: index for index, rule_id in enumerate(rule_ids)}

    run: dict[str, Any] = {
        "tool": {
            "driver": {
                "name": TOOL_NAME,
                "version": tool_version(),
                "informationUri": TOOL_INFORMATION_URI,
                "rules": [_rule(rule_id, metadata.get(rule_id), ordered) for rule_id in rule_ids],
            }
        },
        "automationDetails": {
            "id": f"{AUTOMATION_ID_PREFIX}/{run_timestamp}",
            "description": {"text": f"Mobile Playbook automation run {run_timestamp}."},
        },
        "results": [_result(row, rule_index) for row in ordered],
        "properties": {"run_timestamp": run_timestamp},
    }
    invocation = _invocation(manifest)
    if invocation is not None:
        run["invocations"] = [invocation]
    if manifest is not None:
        run["properties"]["platform"] = str(manifest.get("platform") or "")
    return {"version": SARIF_VERSION, "$schema": SARIF_SCHEMA, "runs": [run]}


def build_from_run_dir(
    run_dir: Path,
    rule_metadata: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """`None` when the run did not complete, so a partial feed is never published as results."""
    path = Path(run_dir)
    manifest = read_manifest(path)
    if not is_completed(manifest):
        return None
    results_path = path / RESULTS_NAME
    try:
        rows = json.loads(results_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(rows, list):
        return None
    return build_sarif(
        [row for row in rows if isinstance(row, Mapping)],
        run_timestamp=path.name,
        manifest=manifest,
        rule_metadata=rule_metadata,
    )


def write_sarif(
    run_dir: Path,
    rule_metadata: Mapping[str, Mapping[str, Any]] | None = None,
) -> Path | None:
    document = build_from_run_dir(run_dir, rule_metadata)
    if document is None:
        return None
    path = sarif_path(run_dir)
    path.write_text(dumps(document), encoding="utf-8")
    return path


def dumps(document: Mapping[str, Any]) -> str:
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _row_sort_key(row: Mapping[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(row.get("app_id") or ""),
        str(row.get("test_id") or ""),
        str(_test_case_id(row)),
        str(row.get("report_path") or ""),
    )


def _test_case_id(row: Mapping[str, Any]) -> str:
    raw = row.get("raw")
    if isinstance(raw, Mapping):
        return str(raw.get("test_case_id") or "")
    return ""


def _rule(rule_id: str, metadata: Mapping[str, Any] | None, rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    sample = next((row for row in rows if str(row.get("test_id") or "") == rule_id), {})
    name = str((metadata or {}).get("name") or sample.get("test_name") or rule_id)
    description = str((metadata or {}).get("description") or "").strip()
    goal = str((metadata or {}).get("goal") or "").strip()
    full = " ".join(part for part in (description, goal) if part) or description or name
    rule: dict[str, Any] = {
        "id": rule_id,
        "name": name,
        "shortDescription": {"text": description or name},
        "fullDescription": {"text": full},
        "properties": {
            "category": str(sample.get("category") or ""),
            "platform": str(sample.get("platform") or ""),
        },
    }
    for field in ("tactic", "feature_id"):
        value = (metadata or {}).get(field)
        if value:
            rule["properties"][field] = str(value)
    if (metadata or {}).get("is_blocking") is not None:
        rule["properties"]["is_blocking"] = bool(metadata["is_blocking"])
    tags = [tag for tag in (sample.get("category"), (metadata or {}).get("tactic")) if tag]
    if tags:
        rule["properties"]["tags"] = [str(tag) for tag in tags]
    return rule


def _result(row: Mapping[str, Any], rule_index: Mapping[str, int]) -> dict[str, Any]:
    rule_id = str(row.get("test_id") or "")
    verdict = str(row.get("verdict") or DEFAULT_VERDICT)
    severity = _text_or_none(row.get("severity"))
    kind, level = verdict_kind_and_level(verdict, severity)
    evidence = _evidence(row)
    result: dict[str, Any] = {
        "ruleId": rule_id,
        "kind": kind,
        "level": level,
        "message": {"text": str(row.get("summary") or "").strip() or _fallback_message(row, verdict)},
        "partialFingerprints": {
            FINGERPRINT_KEY: result_fingerprint(
                str(row.get("app_id") or ""),
                rule_id,
                _test_case_id(row),
                str(row.get("platform") or ""),
            )
        },
        "properties": {
            "app_id": str(row.get("app_id") or ""),
            "app_name": str(row.get("app_name") or ""),
            "platform": str(row.get("platform") or ""),
            "package_or_bundle_id": str(row.get("package_or_bundle_id") or ""),
            "test_id": rule_id,
            "test_name": str(row.get("test_name") or ""),
            "test_case_id": _test_case_id(row),
            "category": str(row.get("category") or ""),
            "verdict": verdict,
            "severity": severity,
            "dashboard_verdict": verdict,
            "dashboard_severity": severity,
            "status": str(row.get("status") or ""),
            "run_timestamp": str(row.get("run_timestamp") or ""),
            "report_path": _text_or_none(row.get("report_path")),
            "started_at": _text_or_none(row.get("started_at")),
            "completed_at": _text_or_none(row.get("completed_at")),
            "duration_seconds": row.get("duration_seconds"),
            "evidence": evidence,
        },
    }
    if rule_id in rule_index:
        result["ruleIndex"] = rule_index[rule_id]
    attachments = [
        {"artifactLocation": {"uri": item["uri"]}, "description": {"text": item["label"] or item["kind"]}}
        for item in evidence
        if not item["external"]
    ]
    if attachments:
        result["attachments"] = attachments
    return result


def _fallback_message(row: Mapping[str, Any], verdict: str) -> str:
    name = str(row.get("test_name") or row.get("test_id") or "This check")
    app = str(row.get("app_name") or row.get("app_id") or "the application")
    return f"{name} on {app} was assessed as {verdict}."


def _evidence(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Relative to the run directory the SARIF file sits in; never an absolute host path."""
    items = row.get("evidence")
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
        return []
    run_timestamp = str(row.get("run_timestamp") or "")
    evidence = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        raw_path = str(item.get("path") or "")
        uri, external = _relative_uri(raw_path, run_timestamp)
        evidence.append(
            {
                "kind": str(item.get("kind") or ""),
                "label": str(item.get("label") or ""),
                "uri": uri,
                "external": external,
            }
        )
    return sorted(evidence, key=lambda item: (item["uri"], item["kind"], item["label"]))


def _relative_uri(raw_path: str, run_timestamp: str) -> tuple[str, bool]:
    if not raw_path:
        return "", True
    normalized = PurePosixPath(raw_path)
    parts = normalized.parts
    if run_timestamp and run_timestamp in parts:
        index = parts.index(run_timestamp)
        return "/".join(parts[index + 1 :]), False
    if not normalized.is_absolute():
        return normalized.as_posix(), False
    return normalized.name, True


def _invocation(manifest: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if manifest is None:
        return None
    invocation: dict[str, Any] = {"executionSuccessful": is_completed(manifest)}
    for field, key in (("started_at", "startTimeUtc"), ("completed_at", "endTimeUtc")):
        stamp = _utc(manifest.get(field))
        if stamp is not None:
            invocation[key] = stamp
    return invocation


def _utc(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{parsed.microsecond // 1000:03d}Z"


def _text_or_none(value: Any) -> str | None:
    return str(value) if value not in (None, "") else None


def _default_rule_metadata(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    platforms = {str(row.get("platform") or "") for row in rows}
    metadata: dict[str, dict[str, Any]] = {}
    for platform in sorted(platforms):
        for risk in _platform_risks(platform):
            metadata.setdefault(str(risk.get("risk_id") or ""), dict(risk))
        for risk_id, overrides in _risk_yaml_metadata(platform).items():
            metadata.setdefault(risk_id, {}).update(overrides)
    return metadata


def _risk_yaml_metadata(platform: str) -> dict[str, dict[str, Any]]:
    """The authored risk text, which the dashboard edits and the Risk classes do not carry."""
    path = RISK_CONFIG_ROOT / platform / "risks.yaml"
    try:
        import yaml

        data = yaml.safe_load(path.read_text())
    except Exception:
        return {}
    if not isinstance(data, Mapping):
        return {}
    metadata: dict[str, dict[str, Any]] = {}
    for risk_id, entry in data.items():
        if not isinstance(entry, Mapping):
            continue
        fields = {field: entry[field] for field in RISK_METADATA_FIELDS if entry.get(field)}
        if fields:
            metadata[str(risk_id)] = fields
    return metadata


def _platform_risks(platform: str) -> list[dict[str, Any]]:
    try:
        if platform == "ios":
            from mobile_playbook.platforms.ios.risks import list_risks

            return list(list_risks())
        if platform == "android":
            from mobile_playbook.platforms.android.risks import list_risks

            return list(list_risks())
    except Exception:
        return []
    return []

