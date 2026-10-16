from __future__ import annotations

import io
import json
import urllib.error

import pytest

from mobile_playbook.platforms.corellium import client as corellium
from mobile_playbook.platforms.corellium.client import (
    CorelliumClient,
    CorelliumError,
    CorelliumInstance,
    CorelliumProject,
    api_base_url,
    ensure_instance_on,
    select_instance,
    settings_errors,
    stop_instance,
)


class FakeCorellium:
    """Instance states returned in order by get_instance, recording start and unpause calls."""

    def __init__(self, *states: str):
        self.states = list(states)
        self.calls: list[str] = []

    def get_instance(self, instance_id):
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        return CorelliumInstance(id=instance_id, name="pixel", state=state, service_ip="10.11.1.1")

    def start_instance(self, instance_id):
        self.calls.append("start")

    def unpause_instance(self, instance_id):
        self.calls.append("unpause")

    def stop_instance(self, instance_id):
        self.calls.append("stop")


class FakeResponse(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.mark.parametrize(
    "configured",
    ["https://corellium.example.com", "https://corellium.example.com/", "https://corellium.example.com/api", "https://corellium.example.com/api/v1/"],
)
def test_api_base_url_accepts_the_site_api_and_v1_roots(configured):
    assert api_base_url(configured) == "https://corellium.example.com/api"


def test_from_env_names_every_missing_variable(monkeypatch):
    monkeypatch.delenv(corellium.API_URL_ENV, raising=False)
    monkeypatch.delenv(corellium.API_TOKEN_ENV, raising=False)

    with pytest.raises(CorelliumError, match="CORELLIUM_API_URL and CORELLIUM_API_TOKEN"):
        CorelliumClient.from_env()


def test_get_instance_sends_the_bearer_token_and_parses_the_instance(monkeypatch):
    seen = {}

    def fake_urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["auth"] = request.get_header("Authorization")
        return FakeResponse(json.dumps({"id": "abc", "name": "pixel", "state": "on", "serviceIp": "10.11.1.1"}).encode())

    monkeypatch.setattr(corellium.urllib.request, "urlopen", fake_urlopen)

    instance = CorelliumClient("https://corellium.example.com", "secret-token").get_instance("abc")

    assert seen == {"url": "https://corellium.example.com/api/v1/instances/abc", "auth": "Bearer secret-token"}
    assert instance == CorelliumInstance(id="abc", name="pixel", state="on", service_ip="10.11.1.1")


def test_an_http_error_becomes_a_corellium_error_without_the_token(monkeypatch):
    def fake_urlopen(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, io.BytesIO(b'{"error":"no such instance"}'))

    monkeypatch.setattr(corellium.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(CorelliumError, match="HTTP 404") as raised:
        CorelliumClient("https://corellium.example.com", "secret-token").get_instance("missing")
    assert "secret-token" not in str(raised.value)


def test_an_instance_that_is_off_is_started_and_awaited():
    fake = FakeCorellium("off", "booting", "on")

    instance = ensure_instance_on(fake, "abc", timeout=5, poll=0)

    assert instance.state == "on"
    assert fake.calls == ["start"]


def test_a_paused_instance_is_resumed():
    fake = FakeCorellium("paused", "on")

    ensure_instance_on(fake, "abc", timeout=5, poll=0)

    assert fake.calls == ["unpause"]


def test_an_instance_that_is_already_on_is_left_alone():
    fake = FakeCorellium("on")

    assert ensure_instance_on(fake, "abc", timeout=5, poll=0).state == "on"
    assert fake.calls == []


@pytest.mark.parametrize("state", ["deleting", "error"])
def test_an_unusable_instance_is_refused(state):
    with pytest.raises(CorelliumError, match=state):
        ensure_instance_on(FakeCorellium(state), "abc", timeout=5, poll=0)


def test_an_instance_that_errors_while_starting_fails_fast():
    with pytest.raises(CorelliumError, match="error state"):
        ensure_instance_on(FakeCorellium("off", "booting", "error"), "abc", timeout=5, poll=0)


def test_an_instance_that_never_comes_on_times_out():
    with pytest.raises(CorelliumError, match="still booting"):
        ensure_instance_on(FakeCorellium("booting"), "abc", timeout=0, poll=0)


PROJECTS = [CorelliumProject(id="p1", name="Default Project"), CorelliumProject(id="p2", name="testing")]


def _instance(name, *, project="p2", type="android", os="16.0.0", rooted=False, state="off"):
    return CorelliumInstance(
        id=f"id-{name}",
        name=name,
        state=state,
        service_ip="10.11.1.1",
        project=project,
        type=type,
        os=os,
        # Corellium lists the jailbroken patch on every Android instance; the build variant says whether it is rooted.
        patches=("jailbroken",) if rooted or type == "android" else ("corelliumd",),
        osbuild="r2 userdebug" if rooted else "r2 user",
    )


def test_from_json_reads_the_project_build_and_reported_adb_port():
    instance = CorelliumInstance.from_json(
        {"id": "i", "name": "n", "state": "on", "project": "p1", "type": "android", "os": "16.0.0", "osbuild": "r2 user", "port-adb": "5001"}
    )

    assert (instance.project, instance.type, instance.os, instance.osbuild, instance.adb_port) == ("p1", "android", "16.0.0", "r2 user", 5001)
    assert CorelliumInstance.from_json({"port-adb": None}).adb_port is None


@pytest.mark.parametrize(
    ("type", "osbuild", "patches", "rooted"),
    [
        # Every Android instance carries the jailbroken patch; a user build is still stock.
        ("android", "r2 user", ["jailbroken"], False),
        ("android", "r2 userdebug", ["jailbroken"], True),
        ("android", "eng", [], True),
        ("android", "", ["jailbroken"], False),
        ("ios", "", ["jailbroken"], True),
        ("ios", "", ["corelliumd"], False),
    ],
)
def test_rooted_uses_the_android_build_variant_and_the_ios_jailbreak_patch(type, osbuild, patches, rooted):
    instance = CorelliumInstance.from_json({"type": type, "osbuild": osbuild, "patches": patches})

    assert instance.rooted is rooted


def test_select_instance_filters_by_project_platform_and_rooted():
    instances = [
        _instance("other-project", project="p1"),
        _instance("ios", type="ios"),
        _instance("rooted", rooted=True),
        _instance("stock"),
    ]

    chosen = select_instance(PROJECTS, instances, {"project": "testing", "rooted": False}, "android")

    assert chosen.name == "stock"


def test_select_instance_accepts_the_project_id():
    chosen = select_instance(PROJECTS, [_instance("stock", project="p1")], {"project": "p1", "rooted": False}, "android")

    assert chosen.name == "stock"


def test_select_instance_prefers_an_instance_that_is_already_on_then_by_name():
    instances = [_instance("a-off"), _instance("b-on", state="on"), _instance("c-on", state="on"), _instance("bad", state="error")]

    assert select_instance(PROJECTS, instances, {"project": "testing", "rooted": False}, "android").name == "b-on"


def test_select_instance_honours_os_version_and_a_pinned_instance():
    instances = [_instance("fifteen", os="15.0.0", state="on"), _instance("sixteen", os="16.0.0"), _instance("pinned", os="16.1.0")]
    settings = {"project": "testing", "rooted": False}

    assert select_instance(PROJECTS, instances, {**settings, "os_version": 16}, "android").name == "pinned"
    assert select_instance(PROJECTS, instances, {**settings, "os_version": "16.0"}, "android").name == "sixteen"
    assert select_instance(PROJECTS, instances, {**settings, "instance": "id-sixteen"}, "android").name == "sixteen"


def test_select_instance_explains_what_the_project_has_when_nothing_matches():
    instances = [_instance("non_root", rooted=True), _instance("phone", type="ios", os="27.0.1", state="on")]

    with pytest.raises(CorelliumError) as raised:
        select_instance(PROJECTS, instances, {"project": "testing", "rooted": False}, "android")

    message = str(raised.value)
    assert "no usable non-rooted android instance" in message
    assert "non_root (android 16.0.0, rooted, off)" in message
    assert "phone (ios 27.0.1, not rooted, on)" in message


def test_select_instance_refuses_an_unknown_or_ambiguous_project():
    with pytest.raises(CorelliumError, match="No Corellium project is named 'missing'.*Default Project, testing"):
        select_instance(PROJECTS, [], {"project": "missing", "rooted": False}, "android")
    twins = [CorelliumProject(id="a", name="dup"), CorelliumProject(id="b", name="dup")]
    with pytest.raises(CorelliumError, match="project id"):
        select_instance(twins, [], {"project": "dup", "rooted": False}, "android")


def test_stop_instance_stops_a_running_instance_and_skips_one_already_off():
    running = FakeCorellium("on")
    stop_instance(running, _instance("x", state="on"))
    off = FakeCorellium("off")
    stop_instance(off, _instance("x"))

    assert running.calls == ["stop"]
    assert off.calls == []


def test_stop_instance_logs_instead_of_raising_when_corellium_refuses(caplog):
    class Refusing(FakeCorellium):
        def stop_instance(self, instance_id):
            raise CorelliumError("HTTP 403")

    stop_instance(Refusing("on"), _instance("x", state="on"))

    assert "Could not power off" in caplog.text


@pytest.mark.parametrize(
    ("settings", "errors"),
    [
        ({"project": "testing", "rooted": False}, []),
        ({}, ["device.corellium.project is required", "device.corellium.rooted must be true or false"]),
        (
            {"project": "testing", "rooted": "no", "auto_start": "yes", "auto_stop": 1, "boot_timeout_seconds": 0},
            [
                "device.corellium.rooted must be true or false",
                "device.corellium.auto_start must be true or false",
                "device.corellium.auto_stop must be true or false",
                "device.corellium.boot_timeout_seconds must be a positive number",
            ],
        ),
        ("testing", ["device.corellium must be a mapping"]),
    ],
)
def test_settings_errors(settings, errors):
    assert settings_errors(settings) == errors
