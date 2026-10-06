from __future__ import annotations

import pytest

from mobile_playbook.orchestration.selection import validate_risk_selection


def test_validate_risk_selection_passes_when_none_selected():
    validate_risk_selection({"ios-feature-01-risk-01"}, None)  # no filter applied, nothing to check


def test_validate_risk_selection_passes_for_known_ids():
    validate_risk_selection({"ios-feature-01-risk-01", "ios-feature-04-risk-01"}, {"ios-feature-01-risk-01"})


def test_validate_risk_selection_raises_for_unknown_id_with_available_ids_listed():
    with pytest.raises(ValueError, match="Unknown risk ID"):
        validate_risk_selection({"ios-feature-01-risk-01", "ios-feature-04-risk-01"}, {"feature4-risk1"})


def test_validate_risk_selection_error_lists_available_ids():
    with pytest.raises(ValueError) as exc_info:
        validate_risk_selection({"ios-feature-01-risk-01", "ios-feature-04-risk-01"}, {"feature4-risk1"})
    assert "ios-feature-01-risk-01" in str(exc_info.value)
    assert "ios-feature-04-risk-01" in str(exc_info.value)


def _android_app(app_id: str, name: str, package_name: str):
    from types import SimpleNamespace

    return SimpleNamespace(id=app_id, name=name, package_name=package_name, bundle_id="")


def test_an_exact_app_id_selects_only_that_app_not_a_lookalike_package():
    from mobile_playbook.orchestration.selection import resolve_app_selection

    first = _android_app("parking", "Parking", "sg.parking.streetsmart")
    second = _android_app("sg_parking_streetsmart", "Parking", "sg.parking.streetsmart")

    assert resolve_app_selection([first, second], "sg_parking_streetsmart") == [second]
    assert resolve_app_selection([first, second], "parking") == [first]


def test_a_value_that_is_no_app_id_still_matches_loosely():
    from mobile_playbook.orchestration.selection import resolve_app_selection

    first = _android_app("parking", "Parking", "sg.parking.streetsmart")
    second = _android_app("lifesg", "LifeSG", "sg.gov.app.mol")

    assert resolve_app_selection([first, second], "sg.parking.streetsmart") == [first]
    assert resolve_app_selection([first, second], "Parking, LIFESG,parking") == [first, second]
    assert resolve_app_selection([first, second], None) is None


def test_narrowing_leaves_the_config_alone_when_nothing_matches_or_nothing_is_selected():
    from types import SimpleNamespace

    from mobile_playbook.orchestration.selection import narrow_to_app_selection

    apps = [_android_app("parking", "Parking", "sg.parking.streetsmart")]
    config = SimpleNamespace(apps=list(apps))

    narrow_to_app_selection(config, None)
    narrow_to_app_selection(config, "unknown")

    assert config.apps == apps
