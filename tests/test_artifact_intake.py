from __future__ import annotations

import pytest

from mobile_playbook.orchestration.artifact_intake import validate_risk_selection


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
