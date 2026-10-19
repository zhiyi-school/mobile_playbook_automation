from types import SimpleNamespace

from mobile_playbook.orchestration.platform_runner import enabled_test_ids
from mobile_playbook.platforms.ios.risks import get_risk


def test_enabled_manual_only_risks_are_never_planned():
    app = SimpleNamespace(
        id="example-app",
        risks={
            "ios-feature-01-risk-01": {"enabled": True},
            "ios-feature-05-risk-01": {"enabled": True},
            "ios-feature-05-risk-02": {"enabled": True},
            "ios-feature-05-risk-03": {"enabled": True},
            "ios-feature-05-risk-04": {"enabled": True},
        },
    )

    assert list(enabled_test_ids(app, None, get_risk)) == ["ios-feature-01-risk-01"]


def test_selecting_a_manual_only_risk_plans_nothing():
    app = SimpleNamespace(id="example-app", risks={"ios-feature-05-risk-01": {"enabled": True}})

    assert list(enabled_test_ids(app, {"ios-feature-05-risk-01"}, get_risk)) == []
