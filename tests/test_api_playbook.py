from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

from mobile_playbook.api import settings
from mobile_playbook.api.routes import playbook as playbook_route
from mobile_playbook.playbook import catalogue, source
from tests.test_playbook_catalogue import write_playbook

CONTROL_ID = "ios-feature-01-risk-01-control-01"
RISK_ID = "ios-feature-01-risk-01"


@pytest.fixture
def playbook(tmp_path, monkeypatch):
    root = write_playbook(tmp_path / "playbooks")
    monkeypatch.setenv("IOS_PLAYBOOK_DIR", str(root))
    monkeypatch.delenv("PLAYBOOK_SOURCE_DOWNLOAD_ENABLED", raising=False)
    monkeypatch.setattr(settings, "ENV_FILE", tmp_path / "absent.env")
    monkeypatch.setattr(source, "RISK_CONFIG_FILES", {})
    monkeypatch.setattr(catalogue, "CONTROL_OVERRIDE_FILES", {})
    catalogue.clear_cache()
    yield root
    catalogue.clear_cache()


@pytest.fixture
def broken(tmp_path, monkeypatch):
    monkeypatch.setenv("IOS_PLAYBOOK_DIR", str(tmp_path / "absent"))
    monkeypatch.setattr(settings, "ENV_FILE", tmp_path / "absent.env")
    monkeypatch.setattr(source, "RISK_CONFIG_FILES", {})
    catalogue.clear_cache()
    yield
    catalogue.clear_cache()


class TestControlEndpoints:
    def test_lists_the_controls_for_a_risk(self, playbook):
        controls = playbook_route.risk_controls("ios", RISK_ID)
        assert [control["control_id"] for control in controls] == [CONTROL_ID]

    def test_accepts_the_generic_playbook_risk_id_as_well(self, playbook):
        controls = playbook_route.risk_controls("ios", "example-feature-01-risk-01")
        assert [control["control_id"] for control in controls] == [CONTROL_ID]

    def test_an_unknown_risk_is_a_404(self, playbook):
        with pytest.raises(HTTPException) as excinfo:
            playbook_route.risk_controls("ios", "ios-feature-77-risk-01")
        assert excinfo.value.status_code == 404

    def test_the_control_detail_carries_the_contracted_fields(self, playbook):
        control = playbook_route.control_detail("ios", CONTROL_ID)
        for field in (
            "control_id",
            "risk_id",
            "platform",
            "title",
            "status",
            "required",
            "playbook_revision",
            "steps",
            "references",
            "source_archives",
            "step_count",
        ):
            assert field in control, field
        assert control["platform"] == "ios"
        assert control["risk_id"] == RISK_ID

    def test_every_step_carries_a_stable_key_an_index_and_a_title(self, playbook):
        steps = playbook_route.control_detail("ios", CONTROL_ID)["steps"]
        assert [step["step_index"] for step in steps] == [0, 1, 2]
        assert all(step["step_key"] for step in steps)
        assert all(step["step_title"] for step in steps)
        assert len({step["step_key"] for step in steps}) == len(steps)

    def test_step_content_is_structured_json_not_raw_markdown(self, playbook):
        steps = playbook_route.control_detail("ios", CONTROL_ID)["steps"]
        kinds = {block["type"] for step in steps for block in step["content"]}
        assert kinds <= {"paragraph", "caption", "image", "code", "list", "table", "heading"}
        assert all(isinstance(block, dict) for step in steps for block in step["content"])

    def test_an_unknown_control_is_a_404(self, playbook):
        with pytest.raises(HTTPException) as excinfo:
            playbook_route.control_detail("ios", "ios-feature-01-risk-01-control-99")
        assert excinfo.value.status_code == 404


class TestLiveContent:
    def test_a_step_carries_its_stable_id_and_current_content_hash(self, playbook):
        control = playbook_route.control_detail("ios", CONTROL_ID)
        for step in control["steps"]:
            assert step["step_key"]
            assert step["step_id_source"] in {"declared", "auto"}
            assert step["content_hash"].startswith("sha256:")

    def test_editing_the_markdown_serves_the_new_text_without_a_restart(self, playbook):
        before = playbook_route.control_detail("ios", CONTROL_ID)["steps"][2]["text"]
        document = playbook / "example-feature-01-risk-01-control-01.md"
        document.write_text(document.read_text().replace("3. The third", "3. The rewritten third"))
        after = playbook_route.control_detail("ios", CONTROL_ID)["steps"][2]["text"]

        assert before.startswith("The third")
        assert after.startswith("The rewritten third")

    def test_editing_a_code_block_serves_the_new_command_without_a_restart(self, playbook):
        document = playbook / "example-feature-01-risk-01-control-01.md"
        document.write_text(document.read_text().replace("example --flag value", "example --flag rotated"))
        blocks = playbook_route.control_detail("ios", CONTROL_ID)["steps"][1]["content"]

        assert any(block["type"] == "code" and "rotated" in block["text"] for block in blocks)

    def test_replacing_a_screenshot_changes_the_reported_revision(self, playbook):
        before = playbook_route.playbook_status("ios")["revision"]
        (playbook / "attachments" / "example_control_ss1.png").write_bytes(b"\x89PNG\r\n\x1a\nnew")

        assert playbook_route.playbook_status("ios")["revision"] != before

    def test_the_api_offers_no_way_to_ask_for_an_older_playbook_version(self):
        paths = [route.path for route in playbook_route.router.routes]
        assert not any("version" in path or "revision" in path or "history" in path for path in paths)
        assert all("{revision}" not in path for path in paths)


class TestRiskListIntegration:
    def test_the_risk_list_gains_control_summaries(self, playbook, monkeypatch):
        from mobile_playbook.api.services import catalog as catalog_service

        monkeypatch.setattr(
            catalog_service, "list_ios_risks", lambda: [{"risk_id": RISK_ID, "name": "Example risk"}]
        )
        monkeypatch.setattr(catalog_service.config_editor, "get_risk_metadata", lambda *_: {})
        monkeypatch.setattr(catalog_service.config_editor, "get_risk_demonstration", lambda *_: [])

        risk = catalog_service.list_platform_risks("ios")[0]
        assert risk["controls_available"] is True
        assert risk["controls_error"] is None
        assert risk["controls"] == [
            {
                "control_id": CONTROL_ID,
                "risk_id": RISK_ID,
                "title": "Control 1",
                "status": "active",
                "required": True,
                "step_count": 3,
                "playbook_revision": risk["controls"][0]["playbook_revision"],
                "has_source_archive": True,
            }
        ]

    def test_an_unreadable_playbook_reports_the_reason_instead_of_an_empty_list(
        self, broken, monkeypatch
    ):
        from mobile_playbook.api.services import catalog as catalog_service

        monkeypatch.setattr(
            catalog_service, "list_ios_risks", lambda: [{"risk_id": RISK_ID, "name": "Example risk"}]
        )
        monkeypatch.setattr(catalog_service.config_editor, "get_risk_metadata", lambda *_: {})
        monkeypatch.setattr(catalog_service.config_editor, "get_risk_demonstration", lambda *_: [])

        risk = catalog_service.list_platform_risks("ios")[0]
        assert risk["controls"] == []
        assert risk["controls_available"] is False
        assert "not readable" in risk["controls_error"]


class TestAssets:
    def test_serves_a_referenced_screenshot(self, playbook):
        response = playbook_route.control_asset(
            "ios", CONTROL_ID, "attachments/example_control_ss1.png"
        )
        assert Path(response.path) == (playbook / "attachments" / "example_control_ss1.png").resolve()

    @pytest.mark.parametrize(
        "attempt",
        [
            "../../../etc/passwd",
            "attachments/../../../../etc/hosts",
            "/etc/passwd",
            "../.env",
        ],
    )
    def test_refuses_a_path_that_escapes_the_playbook_root(self, playbook, attempt):
        with pytest.raises(HTTPException) as excinfo:
            playbook_route.control_asset("ios", CONTROL_ID, attempt)
        assert excinfo.value.status_code == 404

    def test_refuses_a_file_that_is_not_an_approved_image(self, playbook):
        (playbook / "notes.txt").write_text("not an image")
        with pytest.raises(HTTPException) as excinfo:
            playbook_route.control_asset("ios", CONTROL_ID, "notes.txt")
        assert excinfo.value.status_code == 404

    def test_refuses_the_source_archive_through_the_image_route(self, playbook):
        with pytest.raises(HTTPException) as excinfo:
            playbook_route.control_asset(
                "ios", CONTROL_ID, "implemented_controls/example-feature-01-risk-01-control-01.zip"
            )
        assert excinfo.value.status_code == 404

    def test_a_missing_asset_is_a_404(self, playbook):
        with pytest.raises(HTTPException) as excinfo:
            playbook_route.control_asset("ios", CONTROL_ID, "attachments/absent.png")
        assert excinfo.value.status_code == 404


class TestSourceArchive:
    def test_reports_the_archive_metadata_without_serving_the_bytes(self, playbook):
        metadata = playbook_route.control_source("ios", CONTROL_ID)
        assert metadata["exists"] is True
        assert metadata["file_name"] == "example-feature-01-risk-01-control-01.zip"
        assert metadata["size_bytes"] > 0
        assert metadata["sha256"].startswith("sha256:")
        assert "download_url" in metadata

    def test_downloads_the_archive_only_on_the_download_route(self, playbook):
        response = playbook_route.control_source_download("ios", CONTROL_ID)
        assert Path(response.path) == (
            playbook / "implemented_controls" / "example-feature-01-risk-01-control-01.zip"
        ).resolve()

    def test_the_download_can_be_switched_off_for_a_wider_deployment(self, playbook, monkeypatch):
        monkeypatch.setenv("PLAYBOOK_SOURCE_DOWNLOAD_ENABLED", "false")
        with pytest.raises(HTTPException) as excinfo:
            playbook_route.control_source_download("ios", CONTROL_ID)
        assert excinfo.value.status_code == 403
        assert playbook_route.control_source("ios", CONTROL_ID)["download_enabled"] is False

    def test_a_control_with_no_archive_reports_that_rather_than_failing(self, tmp_path, monkeypatch):
        root = write_playbook(tmp_path / "playbooks", archive=False)
        monkeypatch.setenv("IOS_PLAYBOOK_DIR", str(root))
        monkeypatch.setattr(settings, "ENV_FILE", tmp_path / "absent.env")
        monkeypatch.setattr(source, "RISK_CONFIG_FILES", {})
        monkeypatch.setattr(catalogue, "CONTROL_OVERRIDE_FILES", {})
        catalogue.clear_cache()

        metadata = playbook_route.control_source("ios", CONTROL_ID)
        assert metadata["exists"] is False
        with pytest.raises(HTTPException) as excinfo:
            playbook_route.control_source_download("ios", CONTROL_ID)
        assert excinfo.value.status_code == 404


class TestStatusAndReload:
    def test_reports_the_configured_directory_and_its_contents(self, playbook):
        status = playbook_route.playbook_status("ios")
        assert status["configured_path"] == str(playbook)
        assert status["env_key"] == "IOS_PLAYBOOK_DIR"
        assert status["readable"] is True
        assert status["risk_count"] == 1 and status["control_count"] == 1
        assert status["warnings"] == []

    def test_reports_a_broken_configuration_as_an_error_not_an_empty_catalogue(self, broken):
        status = playbook_route.playbook_status("ios")
        assert status["readable"] is False
        assert status["control_count"] == 0
        assert "not readable" in status["error"]

    def test_surfaces_playbook_warnings_for_an_operator(self, playbook):
        (playbook / "attachments" / "example_control_ss1.png").unlink()
        catalogue.clear_cache()
        assert [warning["code"] for warning in playbook_route.playbook_status("ios")["warnings"]] == [
            "missing_image"
        ]

    def test_a_reload_picks_up_a_new_control(self, playbook):
        assert playbook_route.playbook_status("ios")["control_count"] == 1
        (playbook / "example-feature-01-risk-01-control-02.md").write_text(
            "## example-feature-01-risk-01-control-02\n\n1. Another example step.\n"
        )
        assert playbook_route.reload_playbook("ios")["control_count"] == 2

    def test_a_reload_on_a_broken_configuration_is_a_503(self, broken):
        with pytest.raises(HTTPException) as excinfo:
            playbook_route.reload_playbook("ios")
        assert excinfo.value.status_code == 503


class TestSecretBoundary:
    def test_no_response_carries_a_filesystem_path_from_outside_the_playbook(self, playbook):
        control = playbook_route.control_detail("ios", CONTROL_ID)
        rendered = repr(control)
        assert str(playbook) not in rendered
        assert control["source_file"] == "example-feature-01-risk-01-control-01.md"

    def test_the_playbook_settings_stay_inside_the_allowlist(self):
        assert "IOS_PLAYBOOK_DIR" in settings.ALLOWED_ENV_KEYS
        assert "ANDROID_PLAYBOOK_DIR" in settings.ALLOWED_ENV_KEYS
        assert "SUPABASE_SERVICE_ROLE_KEY" not in settings.ALLOWED_ENV_KEYS
        with pytest.raises(settings.DisallowedSettingError):
            settings.env_setting("SUPABASE_SERVICE_ROLE_KEY")


class TestHeadingFormatResponse:
    """The heading-based format must reach the dashboard in the shape it already reads."""

    def _heading_playbook(self, tmp_path, monkeypatch):
        from tests.test_playbook_catalogue import HEADING_CONTROL

        root = write_playbook(tmp_path / "playbooks", control=HEADING_CONTROL)
        monkeypatch.setenv("IOS_PLAYBOOK_DIR", str(root))
        monkeypatch.delenv("PLAYBOOK_SOURCE_DOWNLOAD_ENABLED", raising=False)
        monkeypatch.setattr(settings, "ENV_FILE", tmp_path / "absent.env")
        monkeypatch.setattr(source, "RISK_CONFIG_FILES", {})
        monkeypatch.setattr(catalogue, "CONTROL_OVERRIDE_FILES", {})
        catalogue.clear_cache()
        return root

    def test_the_control_response_keeps_every_documented_field(self, tmp_path, monkeypatch):
        self._heading_playbook(tmp_path, monkeypatch)
        control = playbook_route.control_detail("ios", CONTROL_ID)

        for field in (
            "control_id",
            "risk_id",
            "platform",
            "title",
            "summary",
            "status",
            "required",
            "playbook_revision",
            "source_file",
            "step_count",
            "intro",
            "steps",
            "references",
            "source_archives",
        ):
            assert field in control, field
        assert control["step_count"] == 2

    def test_each_step_keeps_every_documented_field(self, tmp_path, monkeypatch):
        self._heading_playbook(tmp_path, monkeypatch)
        step = playbook_route.control_detail("ios", CONTROL_ID)["steps"][0]

        assert set(step) == {
            "step_key",
            "step_id_source",
            "step_index",
            "number",
            "step_title",
            "text",
            "content",
            "content_hash",
        }
        assert step["step_id_source"] == "declared"
        assert step["content_hash"].startswith("sha256:")

    def test_the_summary_endpoint_still_reports_the_step_count(self, tmp_path, monkeypatch):
        self._heading_playbook(tmp_path, monkeypatch)
        summary = playbook_route.risk_controls("ios", RISK_ID)[0]

        assert summary["step_count"] == 2
        assert summary["title"] == "Detect an example repackaging attempt"

    def test_an_active_control_without_steps_is_reported_by_status(self, tmp_path, monkeypatch):
        root = write_playbook(
            tmp_path / "playbooks",
            control="## example-feature-01-risk-01-control-01\n\n### Description\n\nA placeholder.\n",
        )
        monkeypatch.setenv("IOS_PLAYBOOK_DIR", str(root))
        monkeypatch.setattr(settings, "ENV_FILE", tmp_path / "absent.env")
        monkeypatch.setattr(source, "RISK_CONFIG_FILES", {})
        monkeypatch.setattr(catalogue, "CONTROL_OVERRIDE_FILES", {})
        catalogue.clear_cache()

        codes = [warning["code"] for warning in playbook_route.playbook_status("ios")["warnings"]]
        assert "control_without_steps" in codes

    def test_the_risk_list_prefers_the_markdown_demonstration(self, tmp_path, monkeypatch):
        from mobile_playbook.api.services import catalog as catalog_service
        from tests.test_playbook_catalogue import TestHeadingRiskDemonstration

        root = write_playbook(
            tmp_path / "playbooks", risk=TestHeadingRiskDemonstration.HEADING_RISK
        )
        monkeypatch.setenv("IOS_PLAYBOOK_DIR", str(root))
        monkeypatch.setattr(settings, "ENV_FILE", tmp_path / "absent.env")
        monkeypatch.setattr(source, "RISK_CONFIG_FILES", {})
        monkeypatch.setattr(catalogue, "CONTROL_OVERRIDE_FILES", {})
        catalogue.clear_cache()
        monkeypatch.setattr(
            catalog_service, "list_ios_risks", lambda: [{"risk_id": RISK_ID, "name": "Example risk"}]
        )
        monkeypatch.setattr(catalog_service.config_editor, "get_risk_metadata", lambda *_: {})
        monkeypatch.setattr(
            catalog_service.config_editor,
            "get_risk_demonstration",
            lambda *_: [{"id": "configured", "type": "steps", "items": []}],
        )

        risk = catalog_service.list_platform_risks("ios")[0]
        steps = [block for block in risk["demonstration"] if block["type"] == "steps"]

        assert all(block["id"] != "configured" for block in risk["demonstration"])
        assert [item["title"] for item in steps[0]["items"]] == [
            "Prepare the example environment",
            "Perform the example test",
        ]
        assert steps[0]["items"][1]["images"][0]["exists"] is True

    def test_the_configured_demonstration_is_used_when_markdown_has_none(self, tmp_path, monkeypatch):
        from mobile_playbook.api.services import catalog as catalog_service
        from tests.test_playbook_catalogue import RISK

        root = write_playbook(
            tmp_path / "playbooks", risk=RISK.replace("### Demonstration", "### Ignored")
        )
        monkeypatch.setenv("IOS_PLAYBOOK_DIR", str(root))
        monkeypatch.setattr(settings, "ENV_FILE", tmp_path / "absent.env")
        monkeypatch.setattr(source, "RISK_CONFIG_FILES", {})
        monkeypatch.setattr(catalogue, "CONTROL_OVERRIDE_FILES", {})
        catalogue.clear_cache()
        monkeypatch.setattr(
            catalog_service, "list_ios_risks", lambda: [{"risk_id": RISK_ID, "name": "Example risk"}]
        )
        monkeypatch.setattr(catalog_service.config_editor, "get_risk_metadata", lambda *_: {})
        monkeypatch.setattr(
            catalog_service.config_editor,
            "get_risk_demonstration",
            lambda *_: [{"id": "configured", "type": "steps", "items": []}],
        )

        risk = catalog_service.list_platform_risks("ios")[0]
        assert [block["id"] for block in risk["demonstration"]] == ["configured"]
