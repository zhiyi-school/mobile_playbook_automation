from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException

from mobile_playbook.api.routes import config as config_routes
from mobile_playbook.api.routes import runs as runs_routes
from mobile_playbook.api.run_registry import JobRegistry
from mobile_playbook.api.services import runs as runs_service
from tests.conftest import make_ipa


@pytest.fixture
def intake(tmp_path):
    directory = tmp_path / "intake"
    make_ipa(directory / "Care_4.3.4.ipa", bundle_id="sg.example.care", display_name="Care", version="4.3.4")
    make_ipa(directory / "Care_3.9.9.ipa", bundle_id="sg.example.care", display_name="Care", version="3.9.9")
    return directory


def _app(intake, source="local_ipa", **artifact):
    return SimpleNamespace(
        id="care",
        name="Care",
        bundle_id="sg.example.care",
        package_name="",
        artifact={
            "source": source,
            "expected_bundle_id": "sg.example.care",
            "intake_dir": str(intake),
            "ipa": str(intake / "Care_4.3.4.ipa"),
            **artifact,
        },
    )


async def _call(app: FastAPI, method: str, path: str, body: dict | None = None):
    sent: list[dict] = []
    payload = json.dumps(body).encode() if body is not None else b""
    received = False

    async def receive():
        nonlocal received
        if received:
            return {"type": "http.disconnect"}
        received = True
        return {"type": "http.request", "body": payload, "more_body": False}

    async def send(message):
        sent.append(message)

    headers = [(b"content-type", b"application/json")] if body is not None else []
    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": headers,
            "client": ("test", 1),
            "server": ("testserver", 80),
            "root_path": "",
        },
        receive,
        send,
    )
    start = next(message for message in sent if message["type"] == "http.response.start")
    raw = b"".join(message.get("body", b"") for message in sent if message["type"] == "http.response.body")
    return start["status"], json.loads(raw) if raw else None


class TestPinningARun:
    def test_a_local_ipa_app_is_pointed_at_the_chosen_build_for_this_run(self, intake):
        app = _app(intake)
        config = SimpleNamespace(apps=[app])

        runs_service.pin_build_version(config, "ios", {"care"}, " 3.9.9 ")

        assert app.artifact["build_version"] == "3.9.9"
        assert app.artifact["ipa"] == str(intake / "Care_3.9.9.ipa")

    def test_an_intake_ipa_app_records_the_chosen_version(self, intake):
        app = _app(intake, source="intake_ipa")

        runs_service.pin_build_version(SimpleNamespace(apps=[app]), "ios", {"care"}, "4.3.4")

        assert app.artifact["build_version"] == "4.3.4"
        assert app.artifact["ipa"] == str(intake / "Care_4.3.4.ipa")

    def test_a_version_not_in_intake_names_a_blocker_and_the_available_versions(self, intake):
        with pytest.raises(HTTPException) as exc:
            runs_service.pin_build_version(SimpleNamespace(apps=[_app(intake)]), "ios", {"care"}, "5.0")

        assert exc.value.status_code == 422
        assert exc.value.detail["blocker_code"] == "app_build_missing"
        assert "Available versions: 3.9.9, 4.3.4." in exc.value.detail["message"]

    @pytest.mark.parametrize(
        "platform, selected, version, source",
        [
            ("android", {"care"}, "4.3.4", "local_ipa"),
            ("ios", None, "4.3.4", "local_ipa"),
            ("ios", {"care"}, "  ", "local_ipa"),
            ("ios", {"care"}, "4.3.4", "installed_app_reference"),
        ],
    )
    def test_a_version_that_cannot_apply_is_refused(self, intake, platform, selected, version, source):
        other = SimpleNamespace(id="other", name="Other", bundle_id="sg.other", package_name="", artifact={})
        config = SimpleNamespace(apps=[_app(intake, source=source), other])

        with pytest.raises(HTTPException) as exc:
            runs_service.pin_build_version(config, platform, selected, version)

        assert exc.value.status_code == 422
        assert isinstance(exc.value.detail, str)


@pytest.fixture
def runs_app(monkeypatch, intake, tmp_path):
    registry = JobRegistry(persist_path=None)
    monkeypatch.setattr(runs_service, "registry", registry)
    monkeypatch.setattr(
        runs_service, "load_config_or_400", lambda platform, config_path: SimpleNamespace(apps=[_app(intake)])
    )
    monkeypatch.setattr(runs_service, "validate_risk_selection", lambda risks, selected: None)
    started: list[object] = []
    ran = threading.Event()
    monkeypatch.setattr(runs_service, "execute_run", lambda *args: (started.append(args), ran.set()))
    monkeypatch.setattr(runs_service, "reserve_run_timestamp", lambda out_dir: "2026-01-01_00-00-00")
    monkeypatch.setattr(runs_service, "api_out_dir", lambda out_dir: tmp_path)
    app = FastAPI()
    app.include_router(runs_routes.router)
    return app, registry, started, ran


class TestRunsEndpoint:
    def test_a_pinned_run_starts_with_the_chosen_build(self, runs_app, intake):
        app, _, started, ran = runs_app

        status, body = asyncio.run(
            _call(app, "POST", "/runs", {"platform": "ios", "config_path": "configs/ios.yaml", "apps": "care", "build_version": "3.9.9"})
        )

        assert status == 202, body
        assert ran.wait(timeout=5)
        config = started[0][2]
        assert config.apps[0].artifact["ipa"] == str(intake / "Care_3.9.9.ipa")

    def test_a_missing_build_is_refused_with_its_blocker_and_frees_the_device(self, runs_app):
        app, registry, started, _ = runs_app

        status, body = asyncio.run(
            _call(app, "POST", "/runs", {"platform": "ios", "config_path": "configs/ios.yaml", "apps": "care", "build_version": "9.9"})
        )

        assert status == 422
        assert body["detail"]["blocker_code"] == "app_build_missing"
        assert started == []
        assert registry.try_claim_platform("ios")


@pytest.fixture
def builds_app(monkeypatch, intake):
    from mobile_playbook.api import config_editing

    apps = {
        "care": {"id": "care", "name": "Care", "bundle_id": "sg.example.care", "artifact": _app(intake).artifact},
        "device": {"id": "device", "name": "Device", "artifact": {"source": "installed_app_reference"}},
    }
    monkeypatch.setattr(config_editing, "effective_ios_app", lambda app_id: apps.get(app_id))
    app = FastAPI()
    app.include_router(config_routes.router)
    return app


class TestBuildsEndpoint:
    def test_lists_the_apps_builds_highest_version_first_with_the_default(self, builds_app):
        status, body = asyncio.run(_call(builds_app, "GET", "/config/ios/apps/care/builds"))

        assert status == 200
        assert [build["version"] for build in body["builds"]] == ["4.3.4", "3.9.9"]
        assert body["builds"][0]["file"] == "Care_4.3.4.ipa"
        assert body["default_version"] == "4.3.4"
        assert body["ambiguous"] is False

    def test_an_app_installed_from_the_device_has_no_builds(self, builds_app):
        status, body = asyncio.run(_call(builds_app, "GET", "/config/ios/apps/device/builds"))

        assert status == 200
        assert body == {"builds": [], "default_version": None, "ambiguous": False}

    def test_an_unknown_app_is_not_found(self, builds_app):
        assert asyncio.run(_call(builds_app, "GET", "/config/ios/apps/missing/builds"))[0] == 404

    def test_android_has_no_selectable_builds(self, builds_app):
        assert asyncio.run(_call(builds_app, "GET", "/config/android/apps/care/builds"))[0] == 422
