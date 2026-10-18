from dataclasses import replace

import pytest

from mobile_playbook.platforms.corellium.client import CorelliumError, ensure_instance_state, wait_instance_state
from tests.platforms.corellium.helpers import FakeAPI, device


@pytest.fixture
def api():
    return FakeAPI(device("stock", "non_rooted", "on"))


@pytest.mark.parametrize("initial, target", [("on", "off"), ("off", "on")])
def test_a_power_change_that_outlasts_its_budget_reports_the_state_not_a_starved_read(api, initial, target):
    api.data["instances"]["stock"] = replace(api.data["instances"]["stock"], state=initial)
    api.data["stuck"].add("stock")
    api.data["latency"] = 0.05
    with pytest.raises(CorelliumError, match=rf"stock did not reach \['{target}'\].*last {initial}") as error:
        ensure_instance_state(api, "stock", target, timeout=0.2, poll=0.001)
    assert not error.value.transient
    assert ("stop" if target == "off" else "start", "stock") in api.data["calls"]


def test_a_state_wait_that_outlasts_its_budget_reports_the_state_not_a_starved_read(api):
    api.data["instances"]["stock"] = replace(api.data["instances"]["stock"], state="off", task_state="restoring")
    api.data["latency"] = 0.05
    with pytest.raises(CorelliumError, match=r"stock did not reach \['off'\].*task restoring") as error:
        wait_instance_state(api, "stock", {"off"}, timeout=0.2, poll=0.001)
    assert not error.value.transient


def test_a_stuck_vm_without_latency_reports_the_state_not_an_expired_deadline(api):
    api.data["stuck"].add("stock")
    with pytest.raises(CorelliumError, match=r"did not reach \['off'\]"):
        ensure_instance_state(api, "stock", "off", timeout=0.05, poll=0.001)


@pytest.mark.parametrize("wait", [
    lambda api: ensure_instance_state(api, "stock", "off", timeout=0.2, poll=0.001),
    lambda api: wait_instance_state(api, "stock", {"off"}, timeout=0.2, poll=0.001),
])
def test_an_outage_during_a_power_wait_still_surfaces_as_a_transient_error(api, monkeypatch, wait):
    api.timeout = 0.05
    api.data["stuck"].add("stock")
    original = api.get_instance
    def instance_then_outage(instance_id):
        if api.data["calls"].count(("get", instance_id)):
            api.call("get", instance_id)
            raise CorelliumError("Corellium API not reachable: connection refused", transient=True)
        return original(instance_id)
    monkeypatch.setattr(api, "get_instance", instance_then_outage)
    with pytest.raises(CorelliumError, match="connection refused") as error:
        wait(api)
    assert error.value.transient
    assert api.data["calls"].count(("get", "stock")) > 2
