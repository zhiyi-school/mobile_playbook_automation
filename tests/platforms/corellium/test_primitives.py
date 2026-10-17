import io
import json
import urllib.error

import pytest

from mobile_playbook.platforms.corellium import client as corellium
from mobile_playbook.platforms.corellium.client import (
    CorelliumClient,
    CorelliumError,
    CorelliumInstance,
    bounded_call,
    polling_deadline,
    retry_read,
)


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_snapshot_and_input_wire_contract_uses_existing_authentication(monkeypatch):
    requests = []
    snapshot = {"id": "s", "instance": "i", "status": {"created": True, "task": "none"}}
    def request(req, timeout):
        requests.append(req)
        if req.full_url.endswith("/snapshots"):
            return Response(json.dumps([snapshot]).encode())
        if req.full_url.endswith("/snapshots/s"):
            return Response(json.dumps(snapshot).encode())
        if "screenshot" in req.full_url:
            return Response(b"\x89PNG\r\n\x1a\nsynthetic")
        return Response(b"")
    monkeypatch.setattr(corellium.urllib.request, "urlopen", request)
    client = CorelliumClient("https://synthetic.example", "private-token")
    assert client.list_snapshots("i") == [snapshot]
    assert client.get_snapshot("i", "s") == snapshot
    client.restore_snapshot("i", "s")
    assert client.screenshot("i").startswith(b"\x89PNG")
    client.touch("i", 100, 300)
    assert requests[2].full_url.endswith("/snapshots/s/restore") and requests[2].method == "POST"
    assert json.loads(requests[-2].data) == [{"buttons": ["finger"], "position": [[100, 300]], "wait": 0}]
    assert json.loads(requests[-1].data) == [{"buttons": [], "wait": 100}]
    assert all(request.get_header("Authorization") == "Bearer private-token" for request in requests)


def test_touch_releases_even_if_press_response_is_lost(monkeypatch):
    client = CorelliumClient("https://synthetic.example", "private-token")
    calls = []
    def request(method, path, body):
        calls.append(body)
        if len(calls) == 1:
            raise CorelliumError("unreachable", transient=True)
    monkeypatch.setattr(client, "_request", request)
    with pytest.raises(CorelliumError):
        client.touch("i", 20, 30)
    assert len(calls) == 2 and calls[1][0]["buttons"] == []


def test_touch_reports_press_failure_when_release_also_fails(monkeypatch):
    client = CorelliumClient("https://synthetic.example", "private-token")
    def request(method, path, body):
        raise CorelliumError("press failed" if body[0]["buttons"] else "release failed")
    monkeypatch.setattr(client, "_request", request)
    with pytest.raises(CorelliumError, match="press failed"):
        client.touch("i", 20, 30)


def test_instance_json_reads_task_state_and_device_model():
    instance = CorelliumInstance.from_json({"id": "i", "taskState": "restoring", "model": "ranchu-model",
                                            "flavorName": "Generic Android"})
    assert (instance.task_state, instance.model) == ("restoring", "ranchu-model")


@pytest.mark.parametrize("status,transient", [(403, False), (404, False), (429, True), (503, True), (504, True)])
def test_http_failures_have_explicit_retry_classification(monkeypatch, status, transient):
    def request(req, timeout):
        raise urllib.error.HTTPError(req.full_url, status, "failure", {}, io.BytesIO(b"{}"))
    monkeypatch.setattr(corellium.urllib.request, "urlopen", request)
    with pytest.raises(CorelliumError) as error:
        CorelliumClient("https://synthetic.example", "secret").get_instance("i")
    assert error.value.transient is transient


def test_read_retry_uses_remaining_deadline_without_altering_shared_timeout(monkeypatch):
    client = CorelliumClient("https://synthetic.example", "secret", timeout=30)
    timeouts = []
    def request(req, timeout):
        timeouts.append(timeout)
        if len(timeouts) == 1:
            raise urllib.error.URLError("temporary outage")
        return Response(b"[]")
    monkeypatch.setattr(corellium.urllib.request, "urlopen", request)
    assert retry_read(client, lambda api: api.list_snapshots("i"), polling_deadline(1, 0.001), 0.001) == []
    assert len(timeouts) == 2 and 0 < timeouts[1] <= timeouts[0] <= 1
    assert client.timeout == 30


def test_expired_deadline_performs_no_request(monkeypatch):
    client = CorelliumClient("https://synthetic.example", "secret")
    def request(*args, **kwargs):
        pytest.fail("Expired operation must not contact the API")
    monkeypatch.setattr(corellium.urllib.request, "urlopen", request)
    with pytest.raises(CorelliumError, match="deadline"):
        bounded_call(client, lambda api: api.list_snapshots("i"), 0)


def test_outage_at_deadline_keeps_transient_classification(monkeypatch):
    client = CorelliumClient("https://synthetic.example", "secret")
    def request(req, timeout):
        raise urllib.error.URLError("outage")
    monkeypatch.setattr(corellium.urllib.request, "urlopen", request)
    with pytest.raises(CorelliumError) as error:
        retry_read(client, lambda api: api.list_snapshots("i"), polling_deadline(0.005, 0.01), 0.01)
    assert error.value.transient


def test_invalid_image_format_and_touch_coordinates_rejected_before_requests():
    client = CorelliumClient("https://synthetic.example", "secret")
    with pytest.raises(ValueError):
        client.screenshot("i", format="svg")
    for x, y in ((-1, 2), (True, 2), (1.5, 3)):
        with pytest.raises(ValueError):
            client.touch("i", x, y)
