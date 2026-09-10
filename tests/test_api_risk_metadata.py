from __future__ import annotations

import pytest
from fastapi import HTTPException

from mobile_playbook.api import config_editor as ce
from mobile_playbook.api.routes.catalog import platform_risks
from mobile_playbook.playbook import catalogue
from tests.test_api_config_editor import config_root  # noqa: F401 — reused fixture

RISK_ID = "ios-feature-01-risk-01"


@pytest.fixture(autouse=True)
def empty_playbook(tmp_path, monkeypatch):
    """No Markdown demonstrations, so these tests exercise the configuration fallback."""
    root = tmp_path / "empty-playbook"
    root.mkdir()
    monkeypatch.setenv("IOS_PLAYBOOK_DIR", str(root))
    catalogue.clear_cache()
    yield root
    catalogue.clear_cache()


@pytest.fixture
def risks_file(config_root):  # noqa: F811
    path = config_root / "configs/split/ios/risks.yaml"
    path.write_text(
        f"{RISK_ID}:\n"
        "  name: From the playbook\n"
        "  description: Because the iOS platform provides X, your app is at risk of Y.\n"
        "  tactic: Discovery\n"
        "  demonstration:\n"
        "    - id: steps\n"
        "      type: steps\n"
        "      items:\n"
        "        - id: step_1\n"
        "          text: Do the thing.\n"
        "          images: []\n"
        "          commands: []\n"
    )
    return path


def _risk(risk_id: str) -> dict:
    return next(risk for risk in platform_risks("ios") if risk["risk_id"] == risk_id)


def test_yaml_owns_the_displayed_metadata(risks_file):
    risk = _risk(RISK_ID)

    assert risk["name"] == "From the playbook"
    assert risk["description"].startswith("Because the iOS platform provides X")
    assert risk["tactic"] == "Discovery"
    assert len(risk["demonstration"]) == 1


def test_a_risk_class_carries_no_displayed_text(risks_file):
    """Nothing but the YAML should be able to put prose in front of a user."""
    unlisted = _risk("ios-feature-02-risk-01")

    assert unlisted["description"] == ""
    assert unlisted["tactic"] is None
    assert unlisted["demonstration"] == []


PLAYBOOK_RISK = """## example-feature-01-risk-01

### Title

From the playbook document

### Description

An authored description. (MITRE ATT&CK: ***Collection*** - TA0035).

### Demonstration

#### 01. Do the authored thing

Follow the authored instruction.
"""


@pytest.fixture
def authored_playbook(empty_playbook, monkeypatch):
    (empty_playbook / "example-feature-01-risk-01.md").write_text(PLAYBOOK_RISK)
    catalogue.clear_cache()
    return empty_playbook


def test_the_playbook_document_overrides_the_configured_metadata(risks_file, authored_playbook):
    risk = _risk(RISK_ID)

    assert risk["name"] == "From the playbook document"
    assert risk["description"].startswith("An authored description")
    assert risk["tactic"] == "Collection"
    assert risk["tactic_id"] == "TA0035"


def test_the_configuration_still_answers_for_a_risk_the_playbook_does_not_cover(
    risks_file, authored_playbook
):
    risk = _risk(RISK_ID)
    unlisted = _risk("ios-feature-02-risk-01")

    assert risk["name"] == "From the playbook document"
    assert unlisted["name"] != "From the playbook document"
    assert unlisted["tactic"] is None
    assert unlisted["tactic_id"] is None


def test_a_playbook_risk_with_no_tactic_keeps_the_configured_one(risks_file, authored_playbook):
    (authored_playbook / "example-feature-01-risk-01.md").write_text(
        PLAYBOOK_RISK.replace(" (MITRE ATT&CK: ***Collection*** - TA0035)", "")
    )
    catalogue.clear_cache()

    assert _risk(RISK_ID)["tactic"] == "Discovery"


def test_behavioural_flags_stay_with_the_class(risks_file):
    assert _risk("ios-feature-99-risk-01")["automation_available"] is False
    assert _risk(RISK_ID)["automation_available"] is True


def test_put_metadata_updates_only_the_named_fields(risks_file):
    stored = ce.put_risk_metadata("ios", RISK_ID, {"description": "A new description."})

    assert stored["description"] == "A new description."
    assert stored["name"] == "From the playbook"
    assert _risk(RISK_ID)["demonstration"][0]["items"][0]["text"] == "Do the thing."


def test_put_metadata_rejects_fields_it_does_not_own(risks_file):
    with pytest.raises(HTTPException) as exc:
        ce.put_risk_metadata("ios", RISK_ID, {"automation_available": False})

    assert exc.value.status_code == 422


def test_put_demonstration_leaves_the_metadata_intact(risks_file):
    ce.put_risk_demonstration("ios", RISK_ID, [{"id": "t", "type": "table", "rows": []}])

    assert ce.get_risk_metadata("ios", RISK_ID)["description"].startswith("Because the iOS platform")
    assert ce.get_risk_demonstration("ios", RISK_ID)[0]["id"] == "t"


def test_unknown_risk_id_is_a_404(risks_file):
    with pytest.raises(HTTPException) as exc:
        ce.get_risk_metadata("ios", "ios-feature-00-risk-00")

    assert exc.value.status_code == 404


def test_a_missing_risks_file_does_not_empty_the_catalogue(config_root):  # noqa: F811
    (config_root / "configs/split/ios/risks.yaml").unlink(missing_ok=True)

    risks = platform_risks("ios")

    assert [r["risk_id"] for r in risks]
    assert all(r["demonstration"] == [] for r in risks)
