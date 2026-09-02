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
