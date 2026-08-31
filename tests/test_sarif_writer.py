from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from mobile_playbook.reporting.run_manifest import write_manifest
from mobile_playbook.reporting.sarif_writer import (
    FINGERPRINT_KEY,
    SARIF_VERSION,
    build_from_run_dir,
    build_sarif,
    dumps,
    result_fingerprint,
    sarif_path,
    write_sarif,
)

SARIF_LEVELS = {"none", "note", "warning", "error"}
SARIF_KINDS = {"notApplicable", "pass", "fail", "review", "open", "informational"}


def assert_valid_sarif(document: Any) -> None:
    """Structural SARIF 2.1.0 checks, in place of a schema validator dependency."""
    assert isinstance(document, dict)
    assert document["version"] == SARIF_VERSION
    assert isinstance(document["$schema"], str) and document["$schema"].startswith("https://")
    assert isinstance(document["runs"], list) and document["runs"]

    for run in document["runs"]:
        driver = run["tool"]["driver"]
        assert isinstance(driver["name"], str) and driver["name"]
        assert isinstance(driver["version"], str) and driver["version"]
        rule_ids = [rule["id"] for rule in driver["rules"]]
        assert len(rule_ids) == len(set(rule_ids)), "rule ids must be unique"
        for rule in driver["rules"]:
            assert isinstance(rule["id"], str) and rule["id"]
            assert isinstance(rule["name"], str) and rule["name"]
            assert isinstance(rule["shortDescription"]["text"], str)
            assert isinstance(rule["fullDescription"]["text"], str)

        assert isinstance(run["automationDetails"]["id"], str)
        for invocation in run.get("invocations", []):
            assert isinstance(invocation["executionSuccessful"], bool)
            for key in ("startTimeUtc", "endTimeUtc"):
                if key in invocation:
                    assert invocation[key].endswith("Z")

        for result in run["results"]:
            assert isinstance(result["ruleId"], str) and result["ruleId"]
            assert isinstance(result["message"]["text"], str) and result["message"]["text"]
            assert result["level"] in SARIF_LEVELS
            assert result["kind"] in SARIF_KINDS
            # SARIF 2.1.0 §3.27.10: a level other than "none" is only for kind "fail".
            if result["kind"] != "fail":
                assert result["level"] == "none", result
            assert result["ruleId"] in rule_ids
            assert rule_ids[result["ruleIndex"]] == result["ruleId"]
            assert isinstance(result["partialFingerprints"][FINGERPRINT_KEY], str)
            # Mobile runtime findings have no source location to point at.
            assert "locations" not in result
            for attachment in result.get("attachments", []):
                uri = attachment["artifactLocation"]["uri"]
                assert not uri.startswith("/") and "://" not in uri


def _row(**overrides: Any) -> dict[str, Any]:
    row = {
        "app_id": "example_app",
        "app_name": "Example Banking App",
        "category": "ios",
        "completed_at": "2026-01-01T00:00:02+00:00",
        "duration_seconds": 2,
        "evidence": [],
        "package_or_bundle_id": "com.example.app",
        "platform": "ios",
        "raw": {"test_case_id": "example_case"},
        "report_path": "ios/example_app/example_risk/example_case",
        "run_timestamp": "2026-01-01_00-00-00",
        "severity": "high",
        "started_at": "2026-01-01T00:00:00+00:00",
        "status": "RISK_EXISTS",
        "summary": "Example summary",
        "test_id": "example_risk",
        "test_name": "Example Risk",
        "verdict": "At Risk",
    }
    row.update(overrides)
    return row


def _run_dir(tmp_path: Path, rows: list[dict[str, Any]] | None = None, status: str = "completed") -> Path:
    run_dir = tmp_path / "2026-01-01_00-00-00"
    run_dir.mkdir(parents=True, exist_ok=True)
    run_dir.joinpath("dashboard_results.json").write_text(json.dumps(rows if rows is not None else [_row()]))
    write_manifest(
        run_dir,
        run_timestamp=run_dir.name,
        platform="ios",
        attempted=[{"app_id": "example_app", "risk_id": "example_risk"}],
        status=status,
        started_at="2026-01-01T08:00:00+08:00",
        completed_at="2026-01-01T08:00:02+08:00",
        error="device disconnected" if status == "failed" else None,
    )
    return run_dir


def test_a_single_result_produces_a_valid_sarif_document(tmp_path):
    document = build_from_run_dir(_run_dir(tmp_path))

    assert_valid_sarif(document)
    assert len(document["runs"][0]["results"]) == 1
    assert len(document["runs"][0]["tool"]["driver"]["rules"]) == 1


def test_a_completed_run_with_no_results_is_still_a_valid_empty_report(tmp_path):
    document = build_from_run_dir(_run_dir(tmp_path, rows=[]))

    assert document["version"] == SARIF_VERSION
    assert document["runs"][0]["results"] == []
    assert document["runs"][0]["tool"]["driver"]["rules"] == []
    assert document["runs"][0]["automationDetails"]["id"].endswith("2026-01-01_00-00-00")


def test_several_risks_produce_one_rule_each_and_one_result_each(tmp_path):
    rows = [
        _row(test_id="risk_b", test_name="Risk B"),
        _row(test_id="risk_a", test_name="Risk A"),
        _row(test_id="risk_a", test_name="Risk A", raw={"test_case_id": "second_case"}),
    ]

    document = build_from_run_dir(_run_dir(tmp_path, rows))

    assert_valid_sarif(document)
    assert [rule["id"] for rule in document["runs"][0]["tool"]["driver"]["rules"]] == ["risk_a", "risk_b"]
    assert len(document["runs"][0]["results"]) == 3


def test_project_identifiers_map_onto_the_sarif_rule_and_result(tmp_path):
    document = build_from_run_dir(_run_dir(tmp_path))
    run = document["runs"][0]
    rule = run["tool"]["driver"]["rules"][0]
    result = run["results"][0]

    assert rule["id"] == "example_risk"
    assert rule["name"] == "Example Risk"
    assert result["ruleId"] == "example_risk"
    assert result["message"]["text"] == "Example summary"
    assert result["properties"]["app_id"] == "example_app"
    assert result["properties"]["test_name"] == "Example Risk"
    assert result["properties"]["run_timestamp"] == "2026-01-01_00-00-00"


def test_every_product_specific_field_survives_into_result_properties(tmp_path):
    document = build_from_run_dir(_run_dir(tmp_path))

    properties = document["runs"][0]["results"][0]["properties"]
    for field in (
        "app_id",
        "app_name",
        "platform",
        "test_id",
        "test_name",
        "category",
        "verdict",
        "severity",
        "run_timestamp",
        "report_path",
        "evidence",
        "dashboard_verdict",
        "dashboard_severity",
    ):
        assert field in properties, field
    assert properties["verdict"] == "At Risk"
    assert properties["severity"] == "high"
    assert properties["dashboard_verdict"] == "At Risk"
    assert properties["dashboard_severity"] == "high"


@pytest.mark.parametrize(
    "verdict,severity,kind,level",
    [
        ("At Risk", "critical", "fail", "error"),
        ("At Risk", "high", "fail", "error"),
        ("At Risk", "medium", "fail", "warning"),
        ("At Risk", "low", "fail", "note"),
        ("At Risk", "info", "fail", "note"),
        ("At Risk", None, "fail", "error"),
        ("At Risk", "nonsense", "fail", "error"),
        ("Reduced Risk", "high", "pass", "none"),
        ("Reduced Risk", None, "pass", "none"),
        ("Inconclusive", "high", "review", "none"),
        ("Inconclusive", None, "review", "none"),
    ],
)
def test_each_verdict_maps_to_its_documented_kind_and_level(tmp_path, verdict, severity, kind, level):
    document = build_from_run_dir(_run_dir(tmp_path, [_row(verdict=verdict, severity=severity)]))

    result = document["runs"][0]["results"][0]
    assert (result["kind"], result["level"]) == (kind, level)
    assert_valid_sarif(document)


def test_only_a_failing_result_may_carry_a_level(tmp_path):
    rows = [
        _row(test_id="risk_a", verdict="At Risk", severity="high"),
        _row(test_id="risk_b", verdict="Reduced Risk", severity="high"),
        _row(test_id="risk_c", verdict="Inconclusive", severity="critical"),
    ]

    document = build_from_run_dir(_run_dir(tmp_path, rows))

    levels = {r["kind"]: r["level"] for r in document["runs"][0]["results"]}
    assert levels == {"fail": "error", "pass": "none", "review": "none"}


def test_severity_survives_even_where_sarif_forbids_a_level(tmp_path):
    rows = [
        _row(test_id="risk_b", verdict="Reduced Risk", severity="high"),
        _row(test_id="risk_c", verdict="Inconclusive", severity="critical"),
    ]

    document = build_from_run_dir(_run_dir(tmp_path, rows))

    by_rule = {r["ruleId"]: r["properties"] for r in document["runs"][0]["results"]}
    assert by_rule["risk_b"]["dashboard_severity"] == "high"
    assert by_rule["risk_b"]["dashboard_verdict"] == "Reduced Risk"
    assert by_rule["risk_c"]["dashboard_severity"] == "critical"
    assert by_rule["risk_c"]["dashboard_verdict"] == "Inconclusive"


def test_an_unknown_verdict_is_treated_as_inconclusive_rather_than_a_pass(tmp_path):
    document = build_from_run_dir(_run_dir(tmp_path, [_row(verdict="Something New")]))

    result = document["runs"][0]["results"][0]
    assert (result["kind"], result["level"]) == ("review", "none")
    assert result["properties"]["verdict"] == "Something New"
    assert result["properties"]["dashboard_verdict"] == "Something New"


def test_a_result_with_no_summary_still_carries_a_message(tmp_path):
    document = build_from_run_dir(_run_dir(tmp_path, [_row(summary="")]))

    assert_valid_sarif(document)
    assert "Example Risk" in document["runs"][0]["results"][0]["message"]["text"]


def test_the_fingerprint_is_the_same_for_the_same_check_in_a_later_run(tmp_path):
    first = build_sarif([_row()], run_timestamp="2026-01-01_00-00-00")
    second = build_sarif(
        [_row(run_timestamp="2027-06-06_12-00-00", summary="different", severity="low")],
        run_timestamp="2027-06-06_12-00-00",
    )

    assert (
        first["runs"][0]["results"][0]["partialFingerprints"][FINGERPRINT_KEY]
        == second["runs"][0]["results"][0]["partialFingerprints"][FINGERPRINT_KEY]
    )


def test_the_fingerprint_differs_per_app_risk_and_test_case(tmp_path):
    base = result_fingerprint("example_app", "example_risk", "example_case", "ios")

    assert base != result_fingerprint("other_app", "example_risk", "example_case", "ios")
    assert base != result_fingerprint("example_app", "other_risk", "example_case", "ios")
    assert base != result_fingerprint("example_app", "example_risk", "other_case", "ios")
    assert base != result_fingerprint("example_app", "example_risk", "example_case", "android")


def test_the_fingerprint_contains_no_timestamp_or_path(tmp_path):
    document = build_from_run_dir(_run_dir(tmp_path))
    fingerprint = document["runs"][0]["results"][0]["partialFingerprints"][FINGERPRINT_KEY]

    assert len(fingerprint) == 64 and all(char in "0123456789abcdef" for char in fingerprint)
    assert fingerprint == result_fingerprint("example_app", "example_risk", "example_case", "ios")


def test_evidence_is_recorded_relative_to_the_run_directory(tmp_path):
    evidence = [
        {"kind": "screenshot", "label": "After launch", "path": "reports/2026-01-01_00-00-00/evidence/shot.png"},
        {"kind": "log", "label": "Device log", "path": "ios/example_app/example_risk/logs.txt"},
    ]
    document = build_from_run_dir(_run_dir(tmp_path, [_row(evidence=evidence)]))

    result = document["runs"][0]["results"][0]
    uris = [item["uri"] for item in result["properties"]["evidence"]]
    assert uris == ["evidence/shot.png", "ios/example_app/example_risk/logs.txt"]
    assert all(item["external"] is False for item in result["properties"]["evidence"])
    assert [a["artifactLocation"]["uri"] for a in result["attachments"]] == uris


def test_evidence_outside_the_run_directory_is_reduced_to_its_file_name(tmp_path):
    evidence = [{"kind": "log", "label": "Appium", "path": "/Users/someone/secret/work/appium.log"}]
    document = build_from_run_dir(_run_dir(tmp_path, [_row(evidence=evidence)]))

    result = document["runs"][0]["results"][0]
    assert result["properties"]["evidence"][0] == {
        "kind": "log",
        "label": "Appium",
        "uri": "appium.log",
        "external": True,
    }
    assert "attachments" not in result


def test_no_absolute_host_path_reaches_the_document(tmp_path):
    evidence = [{"kind": "log", "label": "x", "path": "/Users/someone/private/appium.log"}]
    document = build_from_run_dir(_run_dir(tmp_path, [_row(evidence=evidence)]))

    text = dumps(document)
    assert "/Users/" not in text
    assert str(tmp_path) not in text


def test_no_environment_secret_reaches_the_document(tmp_path, monkeypatch):
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "eyJhbGciOiJIUzI1NiJ9.eyJyb2xlIjoic2VydmljZSJ9.sig")
    document = build_from_run_dir(_run_dir(tmp_path))

    text = dumps(document)
    assert "eyJhbGciOiJIUzI1NiJ9" not in text
    assert "SUPABASE" not in text


def test_the_same_run_serializes_byte_for_byte_the_same(tmp_path):
    run_dir = _run_dir(tmp_path, [_row(test_id="risk_b"), _row(test_id="risk_a")])

    first = dumps(build_from_run_dir(run_dir))
    second = dumps(build_from_run_dir(run_dir))

    assert first == second


def test_result_order_does_not_depend_on_the_order_rows_were_written(tmp_path):
    rows = [_row(test_id="risk_b"), _row(test_id="risk_a"), _row(app_id="aaa_app", test_id="risk_c")]

    forward = build_sarif(rows, run_timestamp="2026-01-01_00-00-00")
    reversed_rows = build_sarif(list(reversed(rows)), run_timestamp="2026-01-01_00-00-00")

    assert dumps(forward) == dumps(reversed_rows)
    assert [r["properties"]["app_id"] for r in forward["runs"][0]["results"]] == [
        "aaa_app",
        "example_app",
        "example_app",
    ]


def test_a_failed_run_produces_no_sarif_at_all(tmp_path):
    run_dir = _run_dir(tmp_path, status="failed")

    assert build_from_run_dir(run_dir) is None
    assert write_sarif(run_dir) is None
    assert not sarif_path(run_dir).exists()


def test_a_run_without_a_manifest_produces_no_sarif(tmp_path):
    run_dir = tmp_path / "2026-01-01_00-00-00"
    run_dir.mkdir()
    run_dir.joinpath("dashboard_results.json").write_text(json.dumps([_row()]))

    assert build_from_run_dir(run_dir) is None


def test_a_completed_run_whose_feed_is_missing_produces_no_sarif(tmp_path):
    run_dir = _run_dir(tmp_path)
    (run_dir / "dashboard_results.json").unlink()

    assert build_from_run_dir(run_dir) is None


def test_a_completed_run_whose_feed_is_corrupt_produces_no_sarif(tmp_path):
    run_dir = _run_dir(tmp_path)
    (run_dir / "dashboard_results.json").write_text("{ not json")

    assert build_from_run_dir(run_dir) is None


def test_a_missing_run_directory_produces_no_sarif(tmp_path):
    assert build_from_run_dir(tmp_path / "does_not_exist") is None


def test_writing_produces_utf8_json_on_disk(tmp_path):
    run_dir = _run_dir(tmp_path, [_row(summary="Café — naïve ✓")])

    path = write_sarif(run_dir)

    assert path == sarif_path(run_dir)
    text = path.read_text(encoding="utf-8")
    assert "Café — naïve ✓" in text
    assert json.loads(text)["version"] == SARIF_VERSION


def test_the_invocation_reports_the_runs_own_start_and_end_in_utc(tmp_path):
    document = build_from_run_dir(_run_dir(tmp_path))

    invocation = document["runs"][0]["invocations"][0]
    assert invocation["executionSuccessful"] is True
    assert invocation["startTimeUtc"] == "2026-01-01T00:00:00.000Z"
    assert invocation["endTimeUtc"] == "2026-01-01T00:00:02.000Z"


def test_an_unparseable_manifest_time_is_omitted_rather_than_guessed(tmp_path):
    document = build_sarif(
        [_row()],
        run_timestamp="2026-01-01_00-00-00",
        manifest={"status": "completed", "started_at": "not a time", "completed_at": None},
    )

    invocation = document["runs"][0]["invocations"][0]
    assert "startTimeUtc" not in invocation
    assert "endTimeUtc" not in invocation


def test_rule_text_comes_from_the_authored_risk_metadata_when_available(tmp_path):
    metadata = {
        "example_risk": {
            "name": "Authored name",
            "description": "Authored description.",
            "goal": "Authored goal.",
            "tactic": "Discovery",
        }
    }

    document = build_sarif([_row()], run_timestamp="2026-01-01_00-00-00", rule_metadata=metadata)

    rule = document["runs"][0]["tool"]["driver"]["rules"][0]
    assert rule["name"] == "Authored name"
    assert rule["shortDescription"]["text"] == "Authored description."
    assert rule["fullDescription"]["text"] == "Authored description. Authored goal."
    assert rule["properties"]["tactic"] == "Discovery"


def test_rule_text_falls_back_to_the_result_row_when_no_metadata_exists(tmp_path):
    document = build_sarif([_row()], run_timestamp="2026-01-01_00-00-00", rule_metadata={})

    rule = document["runs"][0]["tool"]["driver"]["rules"][0]
    assert rule["name"] == "Example Risk"
    assert rule["shortDescription"]["text"] == "Example Risk"
