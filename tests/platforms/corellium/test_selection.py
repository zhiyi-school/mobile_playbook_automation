from dataclasses import replace

import pytest

from mobile_playbook.platforms.corellium.client import CorelliumError, CorelliumProject
from mobile_playbook.platforms.corellium.selection import RoleSelection, resolve_roles, validate_snapshot
from tests.platforms.corellium.helpers import FakeAPI, device


def test_roles_resolve_independently_with_same_snapshot_names():
    stock, root = device(), device("root", "rooted")
    api = FakeAPI(stock, root)
    inventory = {item.instance.id: replace(item.baseline, snapshot_id="", snapshot_name="automation-ready-v1") for item in (stock, root)}
    selected = resolve_roles(api, {"non_rooted": RoleSelection("automation"), "rooted": RoleSelection("project")}, inventory)
    assert [(item.role, item.instance.id, item.baseline_id) for item in selected] == [
        ("non_rooted", "stock", "baseline-stock"), ("rooted", "root", "baseline-root")]
    assert all(action not in {"start", "restore", "stop"} for action, _ in api.data["calls"])


def test_automatic_selection_requires_registration_and_valid_baseline_preferring_on():
    unregistered, off, on, invalid = (device("unregistered", state="on"), device("off"),
                                      device("on", state="on"), device("invalid", state="on"))
    api = FakeAPI(unregistered, off, on, invalid)
    api.data["snapshots"]["invalid"] = []
    inventory = {item.instance.id: item.baseline for item in (off, on, invalid)}
    assert resolve_roles(api, {"non_rooted": RoleSelection("project")}, inventory)[0].instance.id == "on"
    api.data["snapshots"]["on"] = []
    assert resolve_roles(api, {"non_rooted": RoleSelection("project")}, inventory)[0].instance.id == "off"


def test_pin_takes_precedence_over_running_candidates_and_os_filters_apply():
    off, on = device("dedicated"), device("running", state="on")
    api = FakeAPI(off, on)
    inventory = {item.instance.id: item.baseline for item in (off, on)}
    selected = resolve_roles(api, {"non_rooted": RoleSelection("automation", "dedicated", "16")}, inventory)
    assert selected[0].instance.id == "dedicated"
    with pytest.raises(CorelliumError, match="No eligible"):
        resolve_roles(api, {"non_rooted": RoleSelection("project", os_version="15")}, inventory)


@pytest.mark.parametrize("field,value", [("type", "ios"), ("os", "15.0.0"), ("osbuild", "r2 userdebug"),
                                           ("model", "Other"), ("rooted", True), ("instance", "foreign"),
                                           ("status", {"created": False}), ("status", {"created": True, "task": "creating"})])
def test_snapshot_compatibility_rejects_wrong_or_unavailable_metadata(field, value):
    selected = device()
    api = FakeAPI(selected)
    snapshot = {**api.data["snapshots"]["stock"][0], field: value}
    with pytest.raises(CorelliumError):
        validate_snapshot(snapshot, selected.instance, selected.baseline)


@pytest.mark.parametrize("change", [{"rooted": True}, {"os_version": "15"}, {"osbuild": "other"},
                                      {"snapshot_name": "also-specified"}, {"snapshot_id": ""}, {"rooted": "false"}])
def test_manifest_is_explicit_and_matches_instance(change):
    selected = device()
    api = FakeAPI(selected)
    with pytest.raises(CorelliumError):
        resolve_roles(api, {"non_rooted": RoleSelection("project")}, {"stock": replace(selected.baseline, **change)})


def test_snapshot_names_must_be_unique_and_ids_never_fall_back_to_names():
    selected = device()
    api = FakeAPI(selected)
    api.data["snapshots"]["stock"].append({**api.data["snapshots"]["stock"][0], "id": "other"})
    with pytest.raises(CorelliumError, match="uniquely"):
        resolve_roles(api, {"non_rooted": RoleSelection("project")},
                      {"stock": replace(selected.baseline, snapshot_id="", snapshot_name="automation-ready-v1")})
    with pytest.raises(CorelliumError, match="uniquely"):
        resolve_roles(api, {"non_rooted": RoleSelection("project")},
                      {"stock": replace(selected.baseline, snapshot_id="automation-ready-v1")})


def test_ambiguous_instance_selector_rejected_before_root_filtering():
    stock, root = device(), device("root", "rooted")
    api = FakeAPI(stock, root)
    api.data["instances"]["root"] = replace(root.instance, name="stock")
    with pytest.raises(CorelliumError, match="ambiguous"):
        resolve_roles(api, {"non_rooted": RoleSelection("project", "stock")}, {"stock": stock.baseline})


def test_ambiguous_projects_are_rejected(monkeypatch):
    api = FakeAPI(device())
    monkeypatch.setattr(api, "list_projects", lambda: [CorelliumProject("a", "duplicate"), CorelliumProject("b", "duplicate")])
    with pytest.raises(CorelliumError, match="Project selector.*ambiguous"):
        resolve_roles(api, {"non_rooted": RoleSelection("duplicate")}, {})


def test_missing_rooted_device_does_not_block_nonrooted_or_substitute():
    selected = device()
    api = FakeAPI(selected)
    inventory = {"stock": selected.baseline}
    assert len(resolve_roles(api, {"non_rooted": RoleSelection("project")}, inventory)) == 1
    with pytest.raises(CorelliumError, match="rooted"):
        resolve_roles(api, {"rooted": RoleSelection("project")}, inventory)
    with pytest.raises(CorelliumError, match="non_rooted"):
        resolve_roles(FakeAPI(device("root", "rooted")), {"non_rooted": RoleSelection("project")}, inventory)


@pytest.mark.parametrize("requests", [{"typo": RoleSelection("project")}, {"non_rooted": RoleSelection("")},
                                        {"non_rooted": {"project": "project"}}, {"rooted": RoleSelection("project", instance=4)}])
def test_malformed_roles_rejected(requests):
    with pytest.raises(CorelliumError):
        resolve_roles(FakeAPI(device()), requests, {})


def test_static_plan_performs_no_api_calls():
    api = FakeAPI()
    assert resolve_roles(api, {}, {}) == ()
    assert api.data["calls"] == []


def test_an_outage_while_checking_a_candidate_fails_instead_of_switching_devices(monkeypatch):
    preferred, fallback = device("preferred", state="on"), device("fallback")
    api = FakeAPI(preferred, fallback)
    listed = api.list_snapshots

    def list_snapshots(instance_id):
        if instance_id == "preferred":
            api.call("snapshots", instance_id)
            raise CorelliumError("HTTP 503", transient=True)
        return listed(instance_id)

    monkeypatch.setattr(api, "list_snapshots", list_snapshots)
    inventory = {item.instance.id: item.baseline for item in (preferred, fallback)}
    with pytest.raises(CorelliumError, match="503") as error:
        resolve_roles(api, {"non_rooted": RoleSelection("project")}, inventory, timeout=0.05, poll=0.001)
    assert error.value.transient
    assert api.data["calls"].count(("snapshots", "preferred")) > 1
    assert ("snapshots", "fallback") not in api.data["calls"]
