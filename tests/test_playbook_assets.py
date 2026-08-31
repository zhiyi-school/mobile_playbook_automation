from __future__ import annotations

from pathlib import Path

import pytest

from mobile_playbook.api import config_editor, playbook_assets


@pytest.fixture
def playbook(tmp_path, monkeypatch):
    """A playbook dir with one image, wired up via risks.yaml."""
    root = tmp_path / "playbooks"
    (root / "attachments").mkdir(parents=True)
    (root / "attachments" / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (root / "notes.md").write_text("secret notes")

    demonstrations = tmp_path / "risks.yaml"
    demonstrations.write_text(f"playbook_dir: {root}\n")
    monkeypatch.setitem(config_editor.RISK_FILES, "ios", demonstrations)
    return root


def _steps(images: list) -> list:
    return [{"id": "steps", "type": "steps", "items": [{"id": "step_1", "images": images}]}]


def test_resolves_an_image_relative_to_the_playbook_dir(playbook):
    assert playbook_assets.resolve_image("ios", "attachments/shot.png") == (
        playbook / "attachments" / "shot.png"
    ).resolve()


def test_refuses_to_serve_outside_the_playbook_dir(playbook, tmp_path):
    (tmp_path / "outside.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    assert playbook_assets.resolve_image("ios", "../outside.png") is None


def test_refuses_non_image_files_inside_the_playbook_dir(playbook):
    assert playbook_assets.resolve_image("ios", "notes.md") is None


def test_missing_image_resolves_to_none(playbook):
    assert playbook_assets.resolve_image("ios", "attachments/absent.png") is None


def test_no_playbook_dir_configured_resolves_to_none(tmp_path, monkeypatch):
    demonstrations = tmp_path / "risks.yaml"
    demonstrations.write_text("ios-feature-01-risk-01:\n  demonstration: []\n")
    monkeypatch.setitem(config_editor.RISK_FILES, "ios", demonstrations)
    assert playbook_assets.playbook_dir("ios") is None
    assert playbook_assets.resolve_image("ios", "attachments/shot.png") is None


def test_decorate_marks_a_present_image_and_builds_its_url(playbook):
    decorated = playbook_assets.decorate_demonstration(
        "ios", _steps([{"path": "attachments/shot.png", "caption": "A shot"}])
    )
    image = decorated[0]["items"][0]["images"][0]
    assert image["url"] == "/platforms/ios/playbook/images/attachments/shot.png"
    assert image["exists"] is True
    assert image["caption"] == "A shot"


def test_decorate_marks_a_moved_playbooks_image_as_missing(playbook):
    decorated = playbook_assets.decorate_demonstration(
        "ios", _steps([{"path": "attachments/gone.png"}])
    )
    assert decorated[0]["items"][0]["images"][0]["exists"] is False


def test_strip_derived_removes_only_the_read_only_image_fields():
    stripped = playbook_assets.strip_derived(
        _steps([{"path": "a.png", "caption": "c", "url": "/x", "exists": True}])
    )
    assert stripped[0]["items"][0]["images"][0] == {"path": "a.png", "caption": "c"}


def test_strip_derived_leaves_table_rows_alone():
    demonstration = [{"id": "t", "type": "table", "rows": [{"Configuration": "url", "Detail": "https://x"}]}]
    assert playbook_assets.strip_derived(demonstration) == demonstration
