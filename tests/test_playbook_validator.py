from __future__ import annotations

from pathlib import Path

from mobile_playbook.playbook import catalogue
from mobile_playbook.playbook.contract_fixture import export_contract, main as contract_fixture_main
from mobile_playbook.playbook.identity_preview import preview
from mobile_playbook.playbook.validator import main, validate

FIXTURE = Path(__file__).parent / "fixtures" / "playbook_contract"


def test_sanitized_contract_fixture_parses_and_validates():
    result = validate(FIXTURE, "ios")
    control = catalogue.build("ios", FIXTURE, overrides={})["controls"]["ios-feature-01-risk-01-control-01"]

    assert result["errors"] == 0
    assert [step["step_key"] for step in control["steps"]] == [
        "keep-stable-setting",
        "auto-cda458c3a785",
    ]
    assert control["steps"][0]["step_id_source"] == "declared"
    assert control["steps"][1]["step_id_source"] == "auto"
    assert all(step["content_hash"].startswith("sha256:") for step in control["steps"])
    assert control["source_archives"][0]["exists"] is True


def test_contract_export_matches_the_frontend_transport_shape():
    control = export_contract(FIXTURE, "ios", "ios-feature-01-risk-01-control-01")

    assert control["has_source_archive"] is True
    assert [(step["step_key"], step["content_hash"]) for step in control["steps"]] == [
        ("keep-stable-setting", "sha256:859566a674fcabdb9cb2d6e4aa8f098f"),
        ("auto-cda458c3a785", "sha256:8cbcddf02a90f1851f292379865462ac"),
    ]


def test_contract_check_accepts_generated_artifact_and_rejects_drift(tmp_path, capsys):
    artifact = tmp_path / "playbook-control-v1.json"
    arguments = [
        "--root",
        str(FIXTURE),
        "--platform",
        "ios",
        "--control-id",
        "ios-feature-01-risk-01-control-01",
    ]

    assert contract_fixture_main([*arguments, "--output", str(artifact)]) == 0
    assert contract_fixture_main([*arguments, "--check", str(artifact)]) == 0

    artifact.write_text("{}\n", encoding="utf-8")
    assert contract_fixture_main([*arguments, "--check", str(artifact)]) == 1
    assert "generated playbook contract" in capsys.readouterr().out


def test_validator_reports_actionable_identity_and_reference_errors(tmp_path):
    (tmp_path / "platform-feature-01-risk-01.md").write_text(
        "## platform-feature-01-risk-01\n\n### References\n\n[missing](missing.md#nope)\n"
    )
    (tmp_path / "one.md").write_text(
        "## platform-feature-01-risk-01-control-01\n\n### Remediation\n\n"
        "<!-- playbook-step-id: invalid id -->\n1. Repeat this step.\n1. Repeat this step.\n"
    )
    (tmp_path / "two.md").write_text("## platform-feature-01-risk-01-control-01\n")

    result = validate(tmp_path, "ios")
    codes = {item["code"] for item in result["diagnostics"]}

    assert {
        "duplicate_document_id",
        "duplicate_generated_step_id",
        "malformed_step_id",
        "missing_local_link",
    } <= codes
    assert result["errors"] >= 3


def test_json_is_machine_readable_and_strict_mode_fails_on_warnings(tmp_path, capsys):
    (tmp_path / "notes.md").write_text("[custom](example:opaque)\n")

    assert main(["--root", str(tmp_path), "--platform", "ios", "--format", "json"]) == 0
    assert '"unsupported_link_scheme"' in capsys.readouterr().out
    assert main(["--root", str(tmp_path), "--platform", "ios", "--strict"]) == 1


def test_identity_preview_maps_exact_keys_only(tmp_path):
    old_root = tmp_path / "old"
    new_root = tmp_path / "new"
    old_root.mkdir()
    new_root.mkdir()
    heading = "## platform-feature-01-risk-01-control-01\n\n### Remediation\n\n"
    (old_root / "control.md").write_text(heading + "#### 1. Keep this title\n\nOld body.\n")
    generated = catalogue.build("ios", old_root, overrides={})["controls"]["ios-feature-01-risk-01-control-01"]["steps"][0]["step_key"]
    (new_root / "control.md").write_text(
        heading + f"<!-- playbook-step-id: {generated} -->\n#### 9. Keep this title\n\nNew body.\n"
    )

    result = preview(old_root, new_root, "ios")
    mapped = result["controls"][0]["preserved"][0]

    assert mapped["step_key"] == generated
    assert mapped["old_source"] == "auto"
    assert mapped["new_source"] == "declared"
    assert mapped["content_changed"] is True
