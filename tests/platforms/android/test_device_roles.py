from types import SimpleNamespace

import pytest

from mobile_playbook.platforms.android.device_roles import (
    RiskDeclarationError,
    declaration_errors,
    plan_risk_roles,
    required_roles,
    risk_role,
)
from mobile_playbook.platforms.android.risks import get_risk
from mobile_playbook.platforms.android.risks.registry import known_risks
from mobile_playbook.platforms.android.runner import AndroidPlatformRunner
from mobile_playbook.platforms.corellium.selection import ROLES


def declared(requires=(), requires_device=True, risk_id="synthetic-risk"):
    return SimpleNamespace(risk_id=risk_id, requires=list(requires) if isinstance(requires, tuple) else requires,
                           requires_device=requires_device)


RISKS = {
    "static": declared(requires_device=False),
    "plain": declared(("adb", "appium")),
    "rooted": declared(("adb", "root")),
}


def test_roles_are_the_shared_lifecycle_roles():
    assert {risk_role(RISKS["plain"]), risk_role(RISKS["rooted"])} == set(ROLES)


def test_a_risk_without_a_root_requirement_runs_on_the_non_rooted_device():
    assert risk_role(RISKS["plain"]) == "non_rooted"
    assert risk_role(declared()) == "non_rooted"


def test_a_risk_that_requires_root_runs_on_the_rooted_device():
    assert risk_role(RISKS["rooted"]) == "rooted"


def test_a_risk_without_a_device_has_no_role():
    assert risk_role(RISKS["static"]) is None


@pytest.mark.parametrize(
    ("risk", "message"),
    [
        (declared("root"), "requires must be a list"),
        (declared((1,)), "requires must be a list"),
        (declared(("root", "root")), "more than once"),
        (declared(("root",), requires_device=False), "must also require a device"),
        (declared(requires_device="yes"), "requires_device must be true or false"),
    ],
)
def test_malformed_declarations_are_rejected(risk, message):
    with pytest.raises(RiskDeclarationError, match=message):
        risk_role(risk)


def test_the_plan_keeps_the_risk_order_and_routes_each_risk_to_its_role():
    planned = [("a", "rooted"), ("a", "plain"), ("b", "static"), ("b", "unregistered"), ("b", "rooted")]

    plan = plan_risk_roles(planned, RISKS.get)

    assert [(item.app, item.risk_id, item.role) for item in plan] == [
        ("a", "rooted", "rooted"),
        ("a", "plain", "non_rooted"),
        ("b", "static", None),
        ("b", "unregistered", None),
        ("b", "rooted", "rooted"),
    ]
    assert required_roles(plan) == ("non_rooted", "rooted")


@pytest.mark.parametrize(
    ("planned", "roles"),
    [
        ([("a", "static")], ()),
        ([("a", "plain"), ("a", "static")], ("non_rooted",)),
        ([("a", "rooted")], ("rooted",)),
    ],
)
def test_only_the_roles_selected_risks_use_are_required(planned, roles):
    assert required_roles(plan_risk_roles(planned, RISKS.get)) == roles


def test_a_malformed_risk_fails_planning():
    with pytest.raises(RiskDeclarationError):
        plan_risk_roles([("a", "plain"), ("a", "bad")], {**RISKS, "bad": declared("adb")}.get)


def test_every_registered_android_risk_declares_its_device_needs_correctly():
    assert known_risks()
    for risk_id in known_risks():
        assert declaration_errors(get_risk(risk_id)) == [], risk_id


def test_the_runner_derives_roles_from_the_selected_apps_enabled_risks(monkeypatch):
    monkeypatch.setattr("mobile_playbook.platforms.android.runner.get_risk", RISKS.get)
    enabled = {"enabled": True}
    config = SimpleNamespace(apps=[
        SimpleNamespace(id="one", risks={"plain": enabled, "rooted": {"enabled": False}}),
        SimpleNamespace(id="two", risks={"static": enabled, "rooted": enabled}),
    ])
    runner = AndroidPlatformRunner()

    assert runner.required_device_roles(config, None, {"one"}) == ("non_rooted",)
    assert runner.required_device_roles(config, None, {"two"}) == ("rooted",)
    assert runner.required_device_roles(config, {"static"}, None) == ()
    assert runner.required_device_roles(config, None, None) == ("non_rooted", "rooted")
