from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from mobile_playbook.api import settings
from mobile_playbook.playbook import catalogue, controls, markdown, source

RISK = """## example-feature-01-risk-01

### Description

An example risk description for the placeholder application.

### Goal

As a result, this could lead to _**Discovery**_ - an example security goal.

### Demonstration

| Configuration | Detail |
| ------------- | ------ |
| Prerequisite  | example-feature-01 |

1. An example security demonstration step that security performs, not the developer.

<img src="attachments/example_risk_ss1.png" width="400" alt="Alt text">

*Example demonstration screenshot*

Feature-01-Risk-01 control measures:

- [example-feature-01-risk-01-control-01](example-feature-01-risk-01-control-01.md)

References:

- https://example.test/reference
"""

CONTROL = """## example-feature-01-risk-01-control-01

Your app can reduce the example risk by taking the following steps:

1. The first example remediation step the developer performs.

<img src="attachments/example_control_ss1.png" width="400" alt="Alt text">

*Example screenshot for the first step*

2. The second example remediation step, which includes a command.

``` shell
example --flag value
```

3. The third example remediation step.

| Setting | Value |
| ------- | ----- |
| Example | Enabled |

- An example sub-point for the third step.

### References

- https://example.test/control-reference

The source code with the implemented control can be found [here](implemented_controls/example-feature-01-risk-01-control-01.zip).
"""


def write_playbook(root: Path, *, risk: str = RISK, control: str = CONTROL, archive: bool = True) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "example-feature-01-risk-01.md").write_text(risk)
    (root / "example-feature-01-risk-01-control-01.md").write_text(control)

    attachments = root / "attachments"
    attachments.mkdir(exist_ok=True)
    (attachments / "example_risk_ss1.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (attachments / "example_control_ss1.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    if archive:
        archives = root / "implemented_controls"
        archives.mkdir(exist_ok=True)
        with zipfile.ZipFile(archives / "example-feature-01-risk-01-control-01.zip", "w") as bundle:
            bundle.writestr("README.txt", "example source")
    return root


def build_control(root: Path, monkeypatch, control: str) -> dict:
    """Build a one-off catalogue from a control document and return that control."""
    written = write_playbook(root / "playbooks", control=control)
    monkeypatch.setenv("IOS_PLAYBOOK_DIR", str(written))
    monkeypatch.setattr(settings, "ENV_FILE", root / "absent.env")
    monkeypatch.setattr(source, "RISK_CONFIG_FILES", {})
    monkeypatch.setattr(catalogue, "CONTROL_OVERRIDE_FILES", {})
    catalogue.clear_cache()
    return catalogue.get("ios")["controls"]["ios-feature-01-risk-01-control-01"]


@pytest.fixture
def playbook(tmp_path, monkeypatch):
    root = write_playbook(tmp_path / "playbooks")
    monkeypatch.setenv("IOS_PLAYBOOK_DIR", str(root))
    monkeypatch.setattr(settings, "ENV_FILE", tmp_path / "absent.env")
    monkeypatch.setattr(source, "RISK_CONFIG_FILES", {})
    monkeypatch.setattr(catalogue, "CONTROL_OVERRIDE_FILES", {})
    catalogue.clear_cache()
    yield root
    catalogue.clear_cache()


@pytest.fixture
def unconfigured(tmp_path, monkeypatch):
    monkeypatch.delenv("IOS_PLAYBOOK_DIR", raising=False)
    monkeypatch.setattr(settings, "ENV_FILE", tmp_path / "absent.env")
    monkeypatch.setattr(source, "RISK_CONFIG_FILES", {})
    catalogue.clear_cache()
    yield
    catalogue.clear_cache()


class TestConfiguration:
    def test_reads_the_directory_from_the_platform_environment_variable(self, playbook):
        assert source.playbook_root("ios") == playbook.resolve()

    def test_falls_back_to_the_existing_risk_config_setting(self, tmp_path, monkeypatch):
        root = write_playbook(tmp_path / "from-yaml")
        config = tmp_path / "risks.yaml"
        config.write_text(f"playbook_dir: {root}\n")
        monkeypatch.delenv("IOS_PLAYBOOK_DIR", raising=False)
        monkeypatch.setattr(settings, "ENV_FILE", tmp_path / "absent.env")
        monkeypatch.setattr(source, "RISK_CONFIG_FILES", {"ios": config})
        catalogue.clear_cache()

        assert source.playbook_root("ios") == root.resolve()

    def test_the_environment_variable_wins_over_the_risk_config(self, tmp_path, monkeypatch):
        from_env = write_playbook(tmp_path / "from-env")
        config = tmp_path / "risks.yaml"
        config.write_text(f"playbook_dir: {tmp_path / 'from-yaml'}\n")
        monkeypatch.setenv("IOS_PLAYBOOK_DIR", str(from_env))
        monkeypatch.setattr(settings, "ENV_FILE", tmp_path / "absent.env")
        monkeypatch.setattr(source, "RISK_CONFIG_FILES", {"ios": config})
        catalogue.clear_cache()

        assert source.playbook_root("ios") == from_env.resolve()

    def test_an_unset_directory_raises_rather_than_reporting_an_empty_catalogue(self, unconfigured):
        with pytest.raises(source.PlaybookUnavailableError) as excinfo:
            catalogue.get("ios")
        assert "IOS_PLAYBOOK_DIR" in str(excinfo.value)

    def test_a_missing_directory_raises_rather_than_reporting_an_empty_catalogue(self, tmp_path, monkeypatch):
        monkeypatch.setenv("IOS_PLAYBOOK_DIR", str(tmp_path / "absent"))
        monkeypatch.setattr(settings, "ENV_FILE", tmp_path / "absent.env")
        monkeypatch.setattr(source, "RISK_CONFIG_FILES", {})
        catalogue.clear_cache()

        with pytest.raises(source.PlaybookUnavailableError) as excinfo:
            catalogue.get("ios")
        assert "not readable" in str(excinfo.value)

    def test_the_playbook_directory_key_is_allowlisted_for_every_platform(self):
        assert set(source.PLAYBOOK_DIR_ENV.values()) <= settings.ALLOWED_ENV_KEYS

    def test_the_directory_is_never_written_to(self, playbook):
        before = sorted(path.name for path in playbook.rglob("*"))
        catalogue.get("ios")
        assert sorted(path.name for path in playbook.rglob("*")) == before


class TestCatalogueShape:
    def test_maps_each_risk_to_its_controls(self, playbook):
        index = catalogue.get("ios")
        assert index["risks"]["ios-feature-01-risk-01"]["controls"] == [
            "ios-feature-01-risk-01-control-01"
        ]

    def test_rewrites_the_generic_document_prefix_to_the_real_platform(self, playbook):
        index = catalogue.get("ios")
        assert set(index["controls"]) == {"ios-feature-01-risk-01-control-01"}
        assert index["controls"]["ios-feature-01-risk-01-control-01"]["risk_id"] == "ios-feature-01-risk-01"

    def test_carries_the_risk_description_and_goal(self, playbook):
        risk = catalogue.get("ios")["risks"]["ios-feature-01-risk-01"]
        assert risk["description"].startswith("An example risk description")
        assert "example security goal" in risk["goal"]

    def test_a_clean_playbook_produces_no_warnings(self, playbook):
        assert catalogue.get("ios")["warnings"] == []


class TestControlParsing:
    def test_uses_the_document_heading_as_the_control_id(self, tmp_path, monkeypatch):
        root = write_playbook(tmp_path / "playbooks")
        renamed = root / "example-feature-01-risk-01-control-01-draft-v2.md"
        (root / "example-feature-01-risk-01-control-01.md").rename(renamed)
        monkeypatch.setenv("IOS_PLAYBOOK_DIR", str(root))
        monkeypatch.setattr(settings, "ENV_FILE", tmp_path / "absent.env")
        monkeypatch.setattr(source, "RISK_CONFIG_FILES", {})
        monkeypatch.setattr(catalogue, "CONTROL_OVERRIDE_FILES", {})
        catalogue.clear_cache()

        index = catalogue.get("ios")
        assert set(index["controls"]) == {"ios-feature-01-risk-01-control-01"}
        control = index["controls"]["ios-feature-01-risk-01-control-01"]
        assert control["source_file"] == "example-feature-01-risk-01-control-01-draft-v2.md"
        assert index["risks"]["ios-feature-01-risk-01"]["controls"] == [
            "ios-feature-01-risk-01-control-01"
        ]
        assert "heading_filename_mismatch" in [warning["code"] for warning in index["warnings"]]

    def test_preserves_the_numbered_order_of_steps(self, playbook):
        control = catalogue.get("ios")["controls"]["ios-feature-01-risk-01-control-01"]
        assert [step["number"] for step in control["steps"]] == [1, 2, 3]
        assert [step["step_index"] for step in control["steps"]] == [0, 1, 2]

    def test_steps_without_a_declared_id_fall_back_to_a_content_derived_one(self, playbook):
        control = catalogue.get("ios")["controls"]["ios-feature-01-risk-01-control-01"]
        keys = [step["step_key"] for step in control["steps"]]

        assert all(key.startswith("auto-") for key in keys)
        assert len(set(keys)) == len(keys)
        assert all(step["step_id_source"] == "auto" for step in control["steps"])

    def test_extracts_the_step_text_and_a_short_title(self, playbook):
        step = catalogue.get("ios")["controls"]["ios-feature-01-risk-01-control-01"]["steps"][0]
        assert step["text"] == "The first example remediation step the developer performs."
        assert step["step_title"] == "The first example remediation step the developer performs"

    def test_keeps_code_blocks_with_their_step(self, playbook):
        step = catalogue.get("ios")["controls"]["ios-feature-01-risk-01-control-01"]["steps"][1]
        code = [block for block in step["content"] if block["type"] == "code"]
        assert code == [{"type": "code", "language": "shell", "text": "example --flag value"}]

    def test_keeps_tables_and_lists_with_their_step(self, playbook):
        step = catalogue.get("ios")["controls"]["ios-feature-01-risk-01-control-01"]["steps"][2]
        kinds = [block["type"] for block in step["content"]]
        assert "table" in kinds and "list" in kinds
        table = next(block for block in step["content"] if block["type"] == "table")
        assert table["rows"] == [{"Setting": "Example", "Value": "Enabled"}]

    def test_collects_references_without_the_source_archive_link(self, playbook):
        control = catalogue.get("ios")["controls"]["ios-feature-01-risk-01-control-01"]
        assert [reference["url"] for reference in control["references"]] == [
            "https://example.test/control-reference"
        ]

    def test_carries_the_intro_paragraph_as_a_summary(self, playbook):
        control = catalogue.get("ios")["controls"]["ios-feature-01-risk-01-control-01"]
        assert control["summary"].startswith("Your app can reduce the example risk")

    def test_the_security_demonstration_is_not_served_as_developer_steps(self, playbook):
        index = catalogue.get("ios")
        control = index["controls"]["ios-feature-01-risk-01-control-01"]
        texts = " ".join(step["text"] for step in control["steps"])
        assert "security performs" not in texts
        assert "steps" not in index["risks"]["ios-feature-01-risk-01"]


class TestStableStepIds:
    def test_reads_the_identifier_an_author_declares_above_a_step(self, tmp_path, monkeypatch):
        declared = CONTROL.replace(
            "1. The first example remediation step the developer performs.",
            "<!-- playbook-step-id: rotate-example-key -->\n"
            "1. The first example remediation step the developer performs.",
        )
        control = build_control(tmp_path, monkeypatch, declared)

        assert control["steps"][0]["step_key"] == "rotate-example-key"
        assert control["steps"][0]["step_id_source"] == "declared"
        assert control["steps"][1]["step_id_source"] == "auto"

    def test_a_declared_id_survives_the_step_being_rewritten(self, tmp_path, monkeypatch):
        original = CONTROL.replace(
            "1. The first example remediation step the developer performs.",
            "<!-- playbook-step-id: rotate-example-key -->\n"
            "1. The first example remediation step the developer performs.",
        )
        rewritten = original.replace(
            "The first example remediation step the developer performs.",
            "Completely different wording, and a new screenshot below it.",
        )

        assert build_control(tmp_path / "a", monkeypatch, original)["steps"][0]["step_key"] == (
            build_control(tmp_path / "b", monkeypatch, rewritten)["steps"][0]["step_key"]
        )

    def test_a_declared_id_survives_the_steps_being_reordered(self, tmp_path, monkeypatch):
        one = "<!-- playbook-step-id: step-one -->\n1. Example instruction one.\n\n"
        two = "<!-- playbook-step-id: step-two -->\n2. Example instruction two.\n"
        forwards = build_control(tmp_path / "a", monkeypatch, f"## example-feature-01-risk-01-control-01\n\n{one}{two}")
        backwards = build_control(
            tmp_path / "b",
            monkeypatch,
            "## example-feature-01-risk-01-control-01\n\n"
            "<!-- playbook-step-id: step-two -->\n1. Example instruction two.\n\n"
            "<!-- playbook-step-id: step-one -->\n2. Example instruction one.\n",
        )

        assert [step["step_key"] for step in forwards["steps"]] == ["step-one", "step-two"]
        assert [step["step_key"] for step in backwards["steps"]] == ["step-two", "step-one"]

    def test_a_fallback_id_survives_the_steps_being_renumbered(self, tmp_path, monkeypatch):
        before = build_control(
            tmp_path / "a",
            monkeypatch,
            "## example-feature-01-risk-01-control-01\n\n1. Example instruction one.\n\n2. Example instruction two.\n",
        )
        after = build_control(
            tmp_path / "b",
            monkeypatch,
            "## example-feature-01-risk-01-control-01\n\n"
            "1. Example instruction zero.\n\n2. Example instruction one.\n\n3. Example instruction two.\n",
        )

        surviving = {step["step_key"] for step in before["steps"]}
        assert surviving <= {step["step_key"] for step in after["steps"]}
        assert len(after["steps"]) == 3

    def test_a_reworded_fallback_step_becomes_a_new_step_rather_than_silently_matching(
        self, tmp_path, monkeypatch
    ):
        before = build_control(tmp_path / "a", monkeypatch, CONTROL)
        after = build_control(
            tmp_path / "b",
            monkeypatch,
            CONTROL.replace(
                "The second example remediation step, which includes a command.",
                "A completely different second instruction.",
            ),
        )

        assert before["steps"][1]["step_key"] != after["steps"][1]["step_key"]
        assert before["steps"][0]["step_key"] == after["steps"][0]["step_key"]

    def test_two_identically_worded_steps_still_get_distinct_ids(self, tmp_path, monkeypatch):
        control = build_control(
            tmp_path,
            monkeypatch,
            "## example-feature-01-risk-01-control-01\n\n1. The same instruction.\n\n2. The same instruction.\n",
        )
        keys = [step["step_key"] for step in control["steps"]]
        assert len(set(keys)) == 2

    def test_every_step_carries_a_hash_of_what_it_currently_renders(self, playbook):
        control = catalogue.get("ios")["controls"]["ios-feature-01-risk-01-control-01"]
        assert all(step["content_hash"].startswith("sha256:") for step in control["steps"])
        assert len({step["content_hash"] for step in control["steps"]}) == len(control["steps"])

    def test_the_content_hash_changes_when_a_code_block_changes(self, tmp_path, monkeypatch):
        before = build_control(tmp_path / "a", monkeypatch, CONTROL)
        after = build_control(
            tmp_path / "b", monkeypatch, CONTROL.replace("example --flag value", "example --flag other")
        )

        assert before["steps"][1]["step_key"] == after["steps"][1]["step_key"]
        assert before["steps"][1]["content_hash"] != after["steps"][1]["content_hash"]

    def test_a_blank_line_inside_a_fence_is_content_the_hash_covers(self, tmp_path, monkeypatch):
        spaced = CONTROL.replace("example --flag value", "example --flag value\n")
        before = build_control(tmp_path / "a", monkeypatch, CONTROL)
        after = build_control(tmp_path / "b", monkeypatch, spaced)

        assert before["steps"][1]["step_key"] == after["steps"][1]["step_key"]
        assert before["steps"][1]["content_hash"] != after["steps"][1]["content_hash"]

    def test_a_reformatted_code_block_never_moves_a_step_identity(self, tmp_path, monkeypatch):
        spaced = CONTROL.replace("example --flag value", "\nexample --flag value\n")
        before = build_control(tmp_path / "a", monkeypatch, CONTROL)
        after = build_control(tmp_path / "b", monkeypatch, spaced)

        assert [step["step_key"] for step in before["steps"]] == [
            step["step_key"] for step in after["steps"]
        ]

    def test_a_declared_id_is_not_rendered_as_instruction_text(self, tmp_path, monkeypatch):
        control = build_control(
            tmp_path,
            monkeypatch,
            "## example-feature-01-risk-01-control-01\n\n"
            "<!-- playbook-step-id: rotate-example-key -->\n1. Example instruction one.\n",
        )
        assert control["steps"][0]["text"] == "Example instruction one."
        assert "playbook-step-id" not in control["steps"][0]["text"]
        assert "playbook-step-id" not in str(control["intro"])


class TestImages:
    def test_resolves_images_relative_to_the_playbook_root(self, playbook):
        step = catalogue.get("ios")["controls"]["ios-feature-01-risk-01-control-01"]["steps"][0]
        image = next(block for block in step["content"] if block["type"] == "image")
        assert image["exists"] is True
        assert image["url"].startswith(
            "/platforms/ios/controls/ios-feature-01-risk-01-control-01/assets/attachments/example_control_ss1.png?v="
        )

    def test_attaches_the_italic_caption_that_follows_an_image(self, playbook):
        step = catalogue.get("ios")["controls"]["ios-feature-01-risk-01-control-01"]["steps"][0]
        image = next(block for block in step["content"] if block["type"] == "image")
        assert image["caption"] == "Example screenshot for the first step"

    def test_reports_a_missing_image_rather_than_dropping_the_step(self, tmp_path, monkeypatch):
        root = write_playbook(tmp_path / "playbooks")
        (root / "attachments" / "example_control_ss1.png").unlink()
        monkeypatch.setenv("IOS_PLAYBOOK_DIR", str(root))
        monkeypatch.setattr(settings, "ENV_FILE", tmp_path / "absent.env")
        monkeypatch.setattr(source, "RISK_CONFIG_FILES", {})
        monkeypatch.setattr(catalogue, "CONTROL_OVERRIDE_FILES", {})
        catalogue.clear_cache()

        index = catalogue.get("ios")
        control = index["controls"]["ios-feature-01-risk-01-control-01"]
        image = next(block for block in control["steps"][0]["content"] if block["type"] == "image")
        assert image["exists"] is False and image["url"] is None
        assert len(control["steps"]) == 3
        assert [warning["code"] for warning in index["warnings"]] == ["missing_image"]


class TestSourceArchives:
    def test_resolves_the_implemented_control_archive_with_its_metadata(self, playbook):
        control = catalogue.get("ios")["controls"]["ios-feature-01-risk-01-control-01"]
        archive = control["source_archives"][0]
        assert archive["exists"] is True
        assert archive["file_name"] == "example-feature-01-risk-01-control-01.zip"
        assert archive["size_bytes"] > 0
        assert archive["sha256"].startswith("sha256:")

    def test_the_archive_is_never_extracted(self, playbook):
        catalogue.get("ios")
        assert not (playbook / "implemented_controls" / "README.txt").exists()

    def test_reports_a_missing_archive(self, tmp_path, monkeypatch):
        root = write_playbook(tmp_path / "playbooks", archive=False)
        monkeypatch.setenv("IOS_PLAYBOOK_DIR", str(root))
        monkeypatch.setattr(settings, "ENV_FILE", tmp_path / "absent.env")
        monkeypatch.setattr(source, "RISK_CONFIG_FILES", {})
        monkeypatch.setattr(catalogue, "CONTROL_OVERRIDE_FILES", {})
        catalogue.clear_cache()

        index = catalogue.get("ios")
        assert [warning["code"] for warning in index["warnings"]] == ["missing_source_archive"]
        assert index["controls"]["ios-feature-01-risk-01-control-01"]["source_download_url"] is None


class TestWarnings:
    def _build(self, tmp_path, monkeypatch, **kwargs):
        root = write_playbook(tmp_path / "playbooks", **kwargs)
        monkeypatch.setenv("IOS_PLAYBOOK_DIR", str(root))
        monkeypatch.setattr(settings, "ENV_FILE", tmp_path / "absent.env")
        monkeypatch.setattr(source, "RISK_CONFIG_FILES", {})
        monkeypatch.setattr(catalogue, "CONTROL_OVERRIDE_FILES", {})
        catalogue.clear_cache()
        return catalogue.get("ios")

    def test_reports_a_control_file_the_risk_links_to_but_does_not_exist(self, tmp_path, monkeypatch):
        risk = RISK.replace(
            "example-feature-01-risk-01-control-01.md)",
            "example-feature-01-risk-01-control-09.md)",
        )
        index = self._build(tmp_path, monkeypatch, risk=risk)
        codes = [warning["code"] for warning in index["warnings"]]
        assert "missing_control_file" in codes

    def test_reports_a_link_whose_label_and_target_disagree(self, tmp_path, monkeypatch):
        risk = RISK.replace(
            "[example-feature-01-risk-01-control-01](example-feature-01-risk-01-control-01.md)",
            "[example-feature-01-risk-01-control-02](example-feature-01-risk-01-control-01.md)",
        )
        index = self._build(tmp_path, monkeypatch, risk=risk)
        codes = [warning["code"] for warning in index["warnings"]]
        assert "malformed_control_link" in codes

    def test_reports_a_step_with_no_instruction_text(self, tmp_path, monkeypatch):
        control = CONTROL.replace("1. The first example remediation step the developer performs.", "1. ")
        index = self._build(tmp_path, monkeypatch, control=control)
        codes = [warning["code"] for warning in index["warnings"]]
        assert "empty_step" in codes

    def test_reports_an_archive_built_for_a_different_control(self, tmp_path, monkeypatch):
        root = write_playbook(tmp_path / "playbooks")
        other = root / "implemented_controls" / "example-feature-01-risk-01-control-02.zip"
        with zipfile.ZipFile(other, "w") as bundle:
            bundle.writestr("README.txt", "example")
        control = CONTROL.replace(
            "implemented_controls/example-feature-01-risk-01-control-01.zip",
            "implemented_controls/example-feature-01-risk-01-control-02.zip",
        )
        (root / "example-feature-01-risk-01-control-01.md").write_text(control)
        monkeypatch.setenv("IOS_PLAYBOOK_DIR", str(root))
        monkeypatch.setattr(settings, "ENV_FILE", tmp_path / "absent.env")
        monkeypatch.setattr(source, "RISK_CONFIG_FILES", {})
        monkeypatch.setattr(catalogue, "CONTROL_OVERRIDE_FILES", {})
        catalogue.clear_cache()

        codes = [warning["code"] for warning in catalogue.get("ios")["warnings"]]
        assert "mismatched_source_archive" in codes

    def test_reports_a_risk_with_no_controls(self, tmp_path, monkeypatch):
        root = write_playbook(tmp_path / "playbooks")
        (root / "example-feature-01-risk-01-control-01.md").unlink()
        monkeypatch.setenv("IOS_PLAYBOOK_DIR", str(root))
        monkeypatch.setattr(settings, "ENV_FILE", tmp_path / "absent.env")
        monkeypatch.setattr(source, "RISK_CONFIG_FILES", {})
        monkeypatch.setattr(catalogue, "CONTROL_OVERRIDE_FILES", {})
        catalogue.clear_cache()

        codes = [warning["code"] for warning in catalogue.get("ios")["warnings"]]
        assert "risk_without_controls" in codes and "missing_control_file" in codes


class TestControlStatus:
    def _index(self, tmp_path, monkeypatch, control=CONTROL, overrides=None, filename=None):
        root = write_playbook(tmp_path / "playbooks", control=control)
        if filename:
            (root / "example-feature-01-risk-01-control-01.md").rename(root / filename)
        monkeypatch.setenv("IOS_PLAYBOOK_DIR", str(root))
        monkeypatch.setattr(settings, "ENV_FILE", tmp_path / "absent.env")
        monkeypatch.setattr(source, "RISK_CONFIG_FILES", {})
        if overrides is None:
            monkeypatch.setattr(catalogue, "CONTROL_OVERRIDE_FILES", {})
        else:
            path = tmp_path / "controls.yaml"
            path.write_text(overrides)
            monkeypatch.setattr(catalogue, "CONTROL_OVERRIDE_FILES", {"ios": path})
        catalogue.clear_cache()
        return catalogue.get("ios")["controls"]["ios-feature-01-risk-01-control-01"]

    def test_a_plain_control_is_active_and_required(self, playbook):
        control = catalogue.get("ios")["controls"]["ios-feature-01-risk-01-control-01"]
        assert control["status"] == controls.ACTIVE
        assert control["required"] is True

    def test_a_deprioritised_filename_marks_the_control_not_required(self, tmp_path, monkeypatch):
        control = self._index(
            tmp_path,
            monkeypatch,
            filename="example-feature-01-risk-01-control-01_(deprioritise).md",
        )
        assert control["status"] == controls.DEPRIORITIZED
        assert control["required"] is False
        assert control["status_source"] == "naming"

    def test_front_matter_status_overrides_the_filename_marker(self, tmp_path, monkeypatch):
        control = self._index(
            tmp_path,
            monkeypatch,
            control=f"---\nstatus: active\n---\n{CONTROL}",
            filename="example-feature-01-risk-01-control-01_(deprioritise).md",
        )
        assert control["status"] == controls.ACTIVE
        assert control["status_source"] == "front_matter"

    def test_a_config_override_can_deprecate_a_control_without_editing_the_playbook(
        self, tmp_path, monkeypatch
    ):
        control = self._index(
            tmp_path,
            monkeypatch,
            overrides="ios-feature-01-risk-01-control-01:\n  status: deprecated\n",
        )
        assert control["status"] == controls.DEPRECATED
        assert control["required"] is False
        assert control["status_source"] == "config_override"

    def test_a_deprecated_control_is_never_counted_as_required_work(self, tmp_path, monkeypatch):
        for marker in ("deprecated", "deprioritized"):
            control = self._index(
                tmp_path / marker,
                monkeypatch,
                overrides=f"ios-feature-01-risk-01-control-01:\n  status: {marker}\n",
            )
            assert control["required"] is False, marker


class TestCaching:
    def test_serves_a_cached_catalogue_when_nothing_changed(self, playbook):
        assert catalogue.get("ios") is catalogue.get("ios")

    def test_rebuilds_when_a_control_file_changes(self, playbook):
        first = catalogue.get("ios")
        control_file = playbook / "example-feature-01-risk-01-control-01.md"
        control_file.write_text(CONTROL.replace("3. The third", "3. The rewritten third"))
        second = catalogue.get("ios")

        assert second is not first
        assert second["controls"]["ios-feature-01-risk-01-control-01"]["steps"][2]["text"].startswith(
            "The rewritten third"
        )

    def test_rebuilds_when_a_control_file_is_added(self, playbook):
        first = catalogue.get("ios")
        (playbook / "example-feature-01-risk-01-control-02.md").write_text(
            "## example-feature-01-risk-01-control-02\n\n1. Another example step.\n"
        )
        second = catalogue.get("ios")

        assert second is not first
        assert "ios-feature-01-risk-01-control-02" in second["controls"]

    def test_rebuilds_when_an_attachment_is_added(self, playbook):
        first = catalogue.get("ios")
        (playbook / "attachments" / "example_extra.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        assert catalogue.get("ios") is not first

    def test_rebuilds_when_a_screenshot_is_replaced_in_place(self, playbook):
        first = catalogue.get("ios")
        shot = playbook / "attachments" / "example_control_ss1.png"
        shot.write_bytes(b"\x89PNG\r\n\x1a\n" + b"different pixels")
        second = catalogue.get("ios")

        assert second is not first
        assert second["revision"] != first["revision"]

    def test_a_replaced_screenshot_changes_its_asset_url_so_a_browser_refetches(self, playbook):
        def url_of(index: dict) -> str:
            step = index["controls"]["ios-feature-01-risk-01-control-01"]["steps"][0]
            return next(block["url"] for block in step["content"] if block["type"] == "image")

        before = url_of(catalogue.get("ios"))
        (playbook / "attachments" / "example_control_ss1.png").write_bytes(
            b"\x89PNG\r\n\x1a\n" + b"different pixels"
        )
        after = url_of(catalogue.get("ios"))

        assert before.split("?")[0] == after.split("?")[0]
        assert before != after

    def test_rebuilds_when_a_source_archive_changes(self, playbook):
        first = catalogue.get("ios")
        with zipfile.ZipFile(
            playbook / "implemented_controls" / "example-feature-01-risk-01-control-01.zip", "w"
        ) as bundle:
            bundle.writestr("README.txt", "a different example source")
        second = catalogue.get("ios")

        assert second is not first
        assert second["revision"] != first["revision"]

    def test_a_manual_reload_rebuilds_without_any_file_changing(self, playbook):
        first = catalogue.get("ios")
        assert catalogue.reload("ios") is not first

    def test_the_revision_changes_only_when_content_changes(self, playbook):
        before = catalogue.get("ios")["revision"]
        assert catalogue.reload("ios")["revision"] == before
        (playbook / "example-feature-01-risk-01-control-01.md").write_text(CONTROL + "\nAn extra line.\n")
        assert catalogue.get("ios")["revision"] != before

    def test_each_control_carries_its_own_content_revision(self, playbook):
        control = catalogue.get("ios")["controls"]["ios-feature-01-risk-01-control-01"]
        assert control["playbook_revision"].startswith("sha256:")
        assert len(control["playbook_revision"]) == len("sha256:") + 64


class TestIgnoredFiles:
    def test_ignores_the_shadow_documents_an_unzip_leaves_behind(self, playbook):
        (playbook / ".DS_Store").write_bytes(b"junk")
        (playbook / "__MACOSX").mkdir()
        (playbook / "__MACOSX" / "example-feature-02-risk-01.md").write_text(
            "## example-feature-02-risk-01\n"
        )
        (playbook / ".obsidian").mkdir()
        (playbook / ".obsidian" / "example-feature-03-risk-01.md").write_text(
            "## example-feature-03-risk-01\n"
        )
        catalogue.clear_cache()

        index = catalogue.get("ios")
        assert set(index["risks"]) == {"ios-feature-01-risk-01"}
        assert index["warnings"] == []

    def test_a_nested_document_outside_an_ignored_directory_is_still_indexed(self, playbook):
        nested = playbook / "drafts"
        nested.mkdir()
        (nested / "example-feature-02-risk-01.md").write_text("## example-feature-02-risk-01\n")
        catalogue.clear_cache()

        assert "ios-feature-02-risk-01" in catalogue.get("ios")["risks"]

    def test_never_resolves_an_ignored_file_as_an_asset(self, playbook):
        (playbook / "attachments" / ".DS_Store").write_bytes(b"junk")
        assert source.resolve_within(playbook, "attachments/.DS_Store") is None


class TestPathTraversal:
    @pytest.mark.parametrize(
        "attempt",
        [
            "../../../etc/passwd",
            "attachments/../../../../etc/hosts",
            "/etc/passwd",
            "../.env",
            "attachments/../../secret.txt",
        ],
    )
    def test_refuses_to_resolve_a_path_outside_the_playbook_root(self, playbook, attempt):
        assert source.resolve_within(playbook, attempt) is None

    def test_resolves_a_legitimate_asset(self, playbook):
        resolved = source.resolve_within(playbook, "attachments/example_control_ss1.png")
        assert resolved == (playbook / "attachments" / "example_control_ss1.png").resolve()

    def test_a_symlink_escaping_the_root_is_refused(self, playbook, tmp_path):
        secret = tmp_path / "secret.png"
        secret.write_bytes(b"\x89PNG\r\n\x1a\n")
        (playbook / "attachments" / "escape.png").symlink_to(secret)
        assert source.resolve_within(playbook, "attachments/escape.png") is None


class TestMarkdown:
    def test_parses_links_whose_target_contains_parentheses(self):
        links = list(markdown.iter_links("- [label](example-risk-01-control-01_(deprioritise).md)"))
        assert links == [markdown.Link("label", "example-risk-01-control-01_(deprioritise).md", False)]

    def test_distinguishes_an_image_from_a_link(self):
        links = list(markdown.iter_links("![shot](a.png) and [doc](b.md)"))
        assert [(link.target, link.is_image) for link in links] == [("a.png", True), ("b.md", False)]

    def test_reads_html_image_attributes(self):
        images = markdown.html_images('<img src="attachments/a.png" width="400" alt="Alt text">')
        assert images == [{"path": "attachments/a.png", "alt": "Alt text", "width": "400"}]

    def test_reads_front_matter_and_returns_the_remaining_body(self):
        data, body = markdown.split_front_matter("---\nstatus: deprecated\n---\n## heading\n")
        assert data == {"status": "deprecated"} and body == "## heading\n"

    def test_a_document_without_front_matter_is_untouched(self):
        data, body = markdown.split_front_matter("## heading\n")
        assert data == {} and body == "## heading\n"

    def test_normalizes_the_non_breaking_spaces_the_playbook_contains(self):
        assert markdown.caption_text("*Screenshot shows the plugin*") == "Screenshot shows the plugin"


class TestIdentityHelpers:
    def test_strips_a_trailing_status_marker_from_an_identity(self):
        assert controls.document_id("example-feature-04-risk-02_(deprioritise)") == "example-feature-04-risk-02"

    def test_leaves_an_ordinary_identity_alone(self):
        assert controls.document_id("example-feature-01-risk-01-control-01") == (
            "example-feature-01-risk-01-control-01"
        )

    def test_maps_a_generic_document_prefix_onto_the_platform(self):
        assert catalogue.canonical_id("platform-feature-01-risk-01-control-01", "ios") == (
            "ios-feature-01-risk-01-control-01"
        )
        assert catalogue.canonical_id("example-feature-06-risk-01", "android") == "android-feature-06-risk-01"

    def test_derives_the_owning_risk_from_a_control_id(self):
        assert catalogue.risk_id_of_control("ios-feature-01-risk-01-control-02") == "ios-feature-01-risk-01"
        assert catalogue.risk_id_of_control("ios-feature-01-risk-01") is None


HEADING_CONTROL = """## example-feature-01-risk-01-control-01

### Description

Detect an example repackaging attempt

### Demonstration

<!-- playbook-step-id: example-declared-step -->
#### 01. Configure an example signing key

Generate the key outside the repository.

``` shell
export EXAMPLE_KEY_PATH="/placeholder/key.pem"
```

_The command configures the build environment._

##### Recommended solution

Store the key on the build runner only.

#### 02. Generate and sign the example manifest

Sign the manifest during the release build.

1. An ordinary numbered point inside the step.
2. Another ordinary numbered point.

- An example sub-point.

<img src="attachments/example_control_ss1.png" width="400" alt="Alt text">

*Example screenshot for the second step*

| Setting | Value |
| ------- | ----- |
| Example | Enabled |

### References

- https://example.test/control-reference
"""


def build_risk(root: Path, monkeypatch, risk: str) -> dict:
    written = write_playbook(root / "playbooks", risk=risk)
    monkeypatch.setenv("IOS_PLAYBOOK_DIR", str(written))
    monkeypatch.setattr(settings, "ENV_FILE", root / "absent.env")
    monkeypatch.setattr(source, "RISK_CONFIG_FILES", {})
    monkeypatch.setattr(catalogue, "CONTROL_OVERRIDE_FILES", {})
    catalogue.clear_cache()
    return catalogue.get("ios")["risks"]["ios-feature-01-risk-01"]


def build_catalogue(root: Path, monkeypatch, **documents) -> dict:
    written = write_playbook(root / "playbooks", **documents)
    monkeypatch.setenv("IOS_PLAYBOOK_DIR", str(written))
    monkeypatch.setattr(settings, "ENV_FILE", root / "absent.env")
    monkeypatch.setattr(source, "RISK_CONFIG_FILES", {})
    monkeypatch.setattr(catalogue, "CONTROL_OVERRIDE_FILES", {})
    catalogue.clear_cache()
    return catalogue.get("ios")


def warning_codes(index: dict, code: str) -> list[dict]:
    return [warning for warning in index["warnings"] if warning["code"] == code]


class TestHeadingSteps:
    def test_a_numbered_heading_starts_a_step(self, tmp_path, monkeypatch):
        control = build_control(tmp_path, monkeypatch, HEADING_CONTROL)

        assert control["step_count"] == 2
        assert [step["number"] for step in control["steps"]] == [1, 2]
        assert [step["step_title"] for step in control["steps"]] == [
            "Configure an example signing key",
            "Generate and sign the example manifest",
        ]

    def test_remediation_names_the_same_section_as_demonstration(self, tmp_path, monkeypatch):
        control = build_control(
            tmp_path, monkeypatch, HEADING_CONTROL.replace("### Demonstration", "### Remediation")
        )
        assert control["step_count"] == 2

    def test_the_section_heading_is_matched_regardless_of_case(self, tmp_path, monkeypatch):
        control = build_control(
            tmp_path, monkeypatch, HEADING_CONTROL.replace("### Demonstration", "### DEMONSTRATION")
        )
        assert control["step_count"] == 2

    def test_the_leading_paragraph_becomes_the_step_text_once(self, tmp_path, monkeypatch):
        first = build_control(tmp_path, monkeypatch, HEADING_CONTROL)["steps"][0]

        assert first["text"] == "Generate the key outside the repository."
        assert all(block.get("text") != first["text"] for block in first["content"])

    def test_a_code_block_stays_inside_the_step_that_introduced_it(self, tmp_path, monkeypatch):
        first = build_control(tmp_path, monkeypatch, HEADING_CONTROL)["steps"][0]
        code = [block for block in first["content"] if block["type"] == "code"]

        assert [block["language"] for block in code] == ["shell"]
        assert "EXAMPLE_KEY_PATH" in code[0]["text"]

    def test_a_nested_heading_belongs_to_the_step_it_follows(self, tmp_path, monkeypatch):
        first = build_control(tmp_path, monkeypatch, HEADING_CONTROL)["steps"][0]
        headings = [block for block in first["content"] if block["type"] == "heading"]

        assert [block["text"] for block in headings] == ["Recommended solution"]

    def test_an_ordered_list_inside_a_step_stays_content(self, tmp_path, monkeypatch):
        control = build_control(tmp_path, monkeypatch, HEADING_CONTROL)
        second = control["steps"][1]
        lists = [block for block in second["content"] if block["type"] == "list"]

        assert control["step_count"] == 2
        assert [block["ordered"] for block in lists] == [True, False]
        assert len(lists[0]["items"]) == 2

    def test_images_tables_and_captions_stay_with_their_step(self, tmp_path, monkeypatch):
        second = build_control(tmp_path, monkeypatch, HEADING_CONTROL)["steps"][1]
        images = [block for block in second["content"] if block["type"] == "image"]
        tables = [block for block in second["content"] if block["type"] == "table"]

        assert images[0]["caption"] == "Example screenshot for the second step"
        assert images[0]["exists"] is True
        assert tables[0]["rows"] == [{"Setting": "Example", "Value": "Enabled"}]

    def test_a_number_inside_a_code_block_does_not_start_a_step(self, tmp_path, monkeypatch):
        control = build_control(
            tmp_path,
            monkeypatch,
            HEADING_CONTROL.replace('export EXAMPLE_KEY_PATH="/placeholder/key.pem"', "#### 09. Not a step"),
        )
        assert control["step_count"] == 2

    def test_a_number_inside_a_paragraph_does_not_start_a_step(self, tmp_path, monkeypatch):
        control = build_control(
            tmp_path,
            monkeypatch,
            HEADING_CONTROL.replace(
                "Generate the key outside the repository.", "01. This sentence is prose, not a heading."
            ),
        )
        assert control["step_count"] == 2

    def test_a_closing_parenthesis_numbers_a_step_too(self, tmp_path, monkeypatch):
        control = build_control(
            tmp_path, monkeypatch, HEADING_CONTROL.replace("#### 02.", "#### 02)")
        )
        assert [step["number"] for step in control["steps"]] == [1, 2]

    def test_the_references_section_ends_the_steps(self, tmp_path, monkeypatch):
        control = build_control(tmp_path, monkeypatch, HEADING_CONTROL)

        assert control["step_count"] == 2
        assert [reference["url"] for reference in control["references"]] == [
            "https://example.test/control-reference"
        ]
        assert all("control-reference" not in str(block) for step in control["steps"] for block in step["content"])

    def test_structural_headings_never_reach_the_intro(self, tmp_path, monkeypatch):
        control = build_control(tmp_path, monkeypatch, HEADING_CONTROL)
        rendered = [block.get("text") for block in control["intro"]]

        for structural in ("Description", "Demonstration", "Remediation", "References", "Goal"):
            assert structural not in rendered

    def test_an_unknown_section_heading_stays_visible(self, tmp_path, monkeypatch):
        control = build_control(
            tmp_path,
            monkeypatch,
            HEADING_CONTROL.replace(
                "### Demonstration", "### Additional context\n\nSome extra background.\n\n### Demonstration", 1
            ),
        )
        assert "Additional context" in [block.get("text") for block in control["intro"]]
        assert "Some extra background." in [block.get("text") for block in control["intro"]]


class TestDescriptionTitle:
    def test_the_description_supplies_the_title_and_summary(self, tmp_path, monkeypatch):
        control = build_control(tmp_path, monkeypatch, HEADING_CONTROL)

        assert control["title"] == "Detect an example repackaging attempt"
        assert control["summary"] == "Detect an example repackaging attempt"

    def test_front_matter_wins_over_the_description(self, tmp_path, monkeypatch):
        control = build_control(
            tmp_path, monkeypatch, f"---\ntitle: An explicit placeholder title\n---\n\n{HEADING_CONTROL}"
        )
        assert control["title"] == "An explicit placeholder title"
        assert control["summary"] == "Detect an example repackaging attempt"

    def test_a_control_without_a_description_falls_back_to_its_number(self, tmp_path, monkeypatch):
        control = build_control(tmp_path, monkeypatch, HEADING_CONTROL.replace("### Description", "### Ignored"))
        assert control["title"] == "Control 1"


class TestHeadingStepIds:
    def test_a_declared_id_attaches_to_the_heading_below_it(self, tmp_path, monkeypatch):
        steps = build_control(tmp_path, monkeypatch, HEADING_CONTROL)["steps"]

        assert steps[0]["step_key"] == "example-declared-step"
        assert steps[0]["step_id_source"] == "declared"
        assert steps[1]["step_id_source"] == "auto"

    def test_the_declaration_is_never_rendered(self, tmp_path, monkeypatch):
        control = build_control(tmp_path, monkeypatch, HEADING_CONTROL)
        blocks = list(control["intro"]) + [block for step in control["steps"] for block in step["content"]]

        assert "playbook-step-id" not in str(blocks)
        assert "example-declared-step" not in str(blocks)
        assert all(block["type"] != "step_id" for block in blocks)

    def test_a_generated_key_survives_renumbering(self, tmp_path, monkeypatch):
        before = build_control(tmp_path, monkeypatch, HEADING_CONTROL)["steps"][1]["step_key"]
        after = build_control(
            tmp_path / "again", monkeypatch, HEADING_CONTROL.replace("#### 02.", "#### 07.")
        )["steps"][1]["step_key"]

        assert before == after

    def test_a_generated_key_survives_reordering(self, tmp_path, monkeypatch):
        ordered = (
            "## example-feature-01-risk-01-control-01\n\n### Description\n\nA placeholder.\n\n"
            "### Demonstration\n\n#### 01. Alpha placeholder step\n\nFirst.\n\n"
            "#### 02. Beta placeholder step\n\nSecond.\n"
        )
        swapped = (
            "## example-feature-01-risk-01-control-01\n\n### Description\n\nA placeholder.\n\n"
            "### Demonstration\n\n#### 01. Beta placeholder step\n\nSecond.\n\n"
            "#### 02. Alpha placeholder step\n\nFirst.\n"
        )
        before = {step["step_title"]: step["step_key"] for step in build_control(tmp_path, monkeypatch, ordered)["steps"]}
        after = {
            step["step_title"]: step["step_key"]
            for step in build_control(tmp_path / "again", monkeypatch, swapped)["steps"]
        }

        assert before == after

    def test_a_generated_key_survives_a_body_edit(self, tmp_path, monkeypatch):
        before = build_control(tmp_path, monkeypatch, HEADING_CONTROL)["steps"][1]["step_key"]
        after = build_control(
            tmp_path / "again",
            monkeypatch,
            HEADING_CONTROL.replace("Sign the manifest during the release build.", "Rewritten instruction."),
        )["steps"][1]["step_key"]

        assert before == after

    def test_a_retitled_step_becomes_a_new_step(self, tmp_path, monkeypatch):
        before = build_control(tmp_path, monkeypatch, HEADING_CONTROL)["steps"][1]["step_key"]
        after = build_control(
            tmp_path / "again",
            monkeypatch,
            HEADING_CONTROL.replace("Generate and sign the example manifest", "Publish the example manifest"),
        )["steps"][1]["step_key"]

        assert before != after

    def test_the_content_hash_follows_the_title(self, tmp_path, monkeypatch):
        before = build_control(tmp_path, monkeypatch, HEADING_CONTROL)["steps"][0]["content_hash"]
        after = build_control(
            tmp_path / "again",
            monkeypatch,
            HEADING_CONTROL.replace("Configure an example signing key", "Configure an example signing secret"),
        )["steps"][0]["content_hash"]

        assert before != after

    def test_the_content_hash_follows_the_body(self, tmp_path, monkeypatch):
        before = build_control(tmp_path, monkeypatch, HEADING_CONTROL)["steps"][0]["content_hash"]
        after = build_control(
            tmp_path / "again",
            monkeypatch,
            HEADING_CONTROL.replace("Store the key on the build runner only.", "Rotate the key every release."),
        )["steps"][0]["content_hash"]

        assert before != after


class TestHeadingRiskDemonstration:
    HEADING_RISK = """## example-feature-01-risk-01

### Description

An example risk description for the placeholder application.

### Goal

As a result, this could lead to _**Discovery**_ - an example security goal.

### Demonstration

| Configuration | Detail |
| ------------- | ------ |
| Prerequisite  | example-feature-01 |

#### 01. Prepare the example environment

Install the placeholder tooling.

``` shell
example --prepare
```

#### 02. Perform the example test

Capture the result.

<img src="attachments/example_risk_ss1.png" width="400" alt="Alt text">

*Example demonstration screenshot*

Feature-01-Risk-01 control measures:

- [example-feature-01-risk-01-control-01](example-feature-01-risk-01-control-01.md)

References:

- https://example.test/reference
"""

    def test_numbered_headings_become_manual_testing_steps(self, tmp_path, monkeypatch):
        risk = build_risk(tmp_path, monkeypatch, self.HEADING_RISK)
        steps = [block for block in risk["demonstration"] if block["type"] == "steps"]

        assert [item["title"] for item in steps[0]["items"]] == [
            "Prepare the example environment",
            "Perform the example test",
        ]
        assert steps[0]["items"][0]["commands"] == ["example --prepare"]
        assert steps[0]["items"][1]["images"][0]["path"] == "attachments/example_risk_ss1.png"

    def test_a_leading_table_is_kept_with_its_label(self, tmp_path, monkeypatch):
        risk = build_risk(tmp_path, monkeypatch, self.HEADING_RISK)
        tables = [block for block in risk["demonstration"] if block["type"] == "table"]

        assert tables[0]["rows"] == [{"Configuration": "Prerequisite", "Detail": "example-feature-01"}]

    def test_an_ordered_list_demonstration_still_parses(self, tmp_path, monkeypatch):
        risk = build_risk(tmp_path, monkeypatch, RISK)
        steps = [block for block in risk["demonstration"] if block["type"] == "steps"]

        assert len(steps[0]["items"]) == 1
        assert "security performs" in steps[0]["items"][0]["text"]

    def test_a_risk_without_a_demonstration_section_offers_nothing(self, tmp_path, monkeypatch):
        risk = build_risk(tmp_path, monkeypatch, RISK.replace("### Demonstration", "### Ignored"))
        assert risk["demonstration"] == []


class TestParserWarnings:
    def test_an_active_control_with_no_steps_is_reported(self, tmp_path, monkeypatch):
        index = build_catalogue(
            tmp_path,
            monkeypatch,
            control="## example-feature-01-risk-01-control-01\n\n### Description\n\nA placeholder.\n",
        )
        assert warning_codes(index, "control_without_steps")

    def test_a_demonstration_section_without_steps_is_reported(self, tmp_path, monkeypatch):
        index = build_catalogue(
            tmp_path,
            monkeypatch,
            control=(
                "## example-feature-01-risk-01-control-01\n\n### Description\n\nA placeholder.\n\n"
                "### Demonstration\n\nProse with no numbered steps.\n"
            ),
        )
        assert warning_codes(index, "empty_step_section")

    def test_a_duplicate_declared_id_is_reported(self, tmp_path, monkeypatch):
        index = build_catalogue(
            tmp_path,
            monkeypatch,
            control=HEADING_CONTROL.replace(
                "#### 02. Generate and sign the example manifest",
                "<!-- playbook-step-id: example-declared-step -->\n#### 02. Generate and sign the example manifest",
            ),
        )
        assert warning_codes(index, "duplicate_step_id")
        assert len({step["step_key"] for step in index["controls"]["ios-feature-01-risk-01-control-01"]["steps"]}) == 2

    def test_a_duplicate_step_number_is_reported(self, tmp_path, monkeypatch):
        index = build_catalogue(tmp_path, monkeypatch, control=HEADING_CONTROL.replace("#### 02.", "#### 01."))
        assert warning_codes(index, "duplicate_step_number")

    def test_a_missing_description_is_reported_for_a_sectioned_document(self, tmp_path, monkeypatch):
        index = build_catalogue(
            tmp_path, monkeypatch, control=HEADING_CONTROL.replace("### Description", "### Ignored")
        )
        assert warning_codes(index, "missing_description")

    def test_a_legacy_document_is_not_reported_as_missing_a_description(self, tmp_path, monkeypatch):
        index = build_catalogue(tmp_path, monkeypatch)
        assert warning_codes(index, "missing_description") == []

    def test_a_cross_risk_control_link_is_reported(self, tmp_path, monkeypatch):
        written = write_playbook(tmp_path / "playbooks")
        (written / "example-feature-02-risk-01.md").write_text(
            "## example-feature-02-risk-01\n\n### Description\n\nAnother placeholder risk.\n\n"
            "- [example-feature-02-risk-01-control-01](example-feature-01-risk-01-control-01.md)\n"
        )
        monkeypatch.setenv("IOS_PLAYBOOK_DIR", str(written))
        monkeypatch.setattr(settings, "ENV_FILE", tmp_path / "absent.env")
        monkeypatch.setattr(source, "RISK_CONFIG_FILES", {})
        monkeypatch.setattr(catalogue, "CONTROL_OVERRIDE_FILES", {})
        catalogue.clear_cache()
        index = catalogue.get("ios")

        assert warning_codes(index, "cross_risk_control_link")
        assert warning_codes(index, "malformed_control_link")


class TestAutomaticRefresh:
    def test_a_markdown_edit_appears_without_an_explicit_reload(self, playbook):
        assert catalogue.get("ios")["controls"]["ios-feature-01-risk-01-control-01"]["step_count"] == 3

        (playbook / "example-feature-01-risk-01-control-01.md").write_text(HEADING_CONTROL)

        assert catalogue.get("ios")["controls"]["ios-feature-01-risk-01-control-01"]["step_count"] == 2

    def test_a_replaced_image_changes_the_catalogue_revision(self, playbook):
        before = catalogue.get("ios")["revision"]

        (playbook / "attachments/example_control_ss1.png").write_bytes(b"\x89PNG\r\n\x1a\nchanged")

        assert catalogue.get("ios")["revision"] != before

    def test_a_replaced_archive_changes_the_catalogue_revision(self, playbook):
        before = catalogue.get("ios")["revision"]

        with zipfile.ZipFile(
            playbook / "implemented_controls/example-feature-01-risk-01-control-01.zip", "w"
        ) as bundle:
            bundle.writestr("README.txt", "replaced example source")

        assert catalogue.get("ios")["revision"] != before
