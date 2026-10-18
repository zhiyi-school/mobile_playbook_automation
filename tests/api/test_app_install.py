from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from fastapi import FastAPI

from mobile_playbook.api.routes import config as config_routes
from mobile_playbook.api.run_registry import JobRegistry
from mobile_playbook.api.services import app_install
from mobile_playbook.orchestration.appium_server import AppiumStartResult
from mobile_playbook.platforms.android.acquisition.play_store import InstallOutcome, failure
from mobile_playbook.platforms.android.acquisition import store
from mobile_playbook.platforms.android.device.app_version import InstalledVersion
from mobile_playbook.platforms.android.device.phone_lock import phone_lock_holder
from tests.asgi_helpers import call


@pytest.fixture
def installer(config_root, monkeypatch):
    registry = JobRegistry(persist_path=None)
    monkeypatch.setattr(app_install, "registry", registry)
    monkeypatch.setattr(
        app_install, "ensure_appium_running", lambda url, auto_start, log_path, environment: AppiumStartResult("ALREADY_RUNNING")
    )
    calls: list[dict] = []
    outcome = {"value": InstallOutcome(status="installed", installer="com.android.vending", seconds=12.0)}

    def fake_install(adb, make_driver, package, diagnostics_root, **kwargs):
        calls.append({"package": package, "diagnostics_root": diagnostics_root, "busy": registry.is_platform_busy("android"),
                      "phone": (phone_lock_holder() or {}).get("purpose")})
        if isinstance(outcome["value"], Exception):
            raise outcome["value"]
        return outcome["value"]

    for key, app_store in list(store.APP_STORES.items()):
        monkeypatch.setitem(store.APP_STORES, key, replace(app_store, install=fake_install))
    monkeypatch.setattr(store, "installed_version", lambda adb, package: InstalledVersion("3.2.1", "321"))
    app = FastAPI()
    app.include_router(config_routes.router)
    return app, registry, calls, outcome


def test_a_missing_app_is_installed_by_its_package_while_the_device_is_held(installer):
    app, registry, calls, _ = installer

    status, body = asyncio.run(call(app, "POST", "/config/android/apps/one/install"))

    assert status == 200, body
    assert body["status"] == "installed"
    assert body["installer"] == "com.android.vending"
    assert (body["version_name"], body["version_code"]) == ("3.2.1", "321")
    assert phone_lock_holder() is None
    assert calls == [
        {"package": "com.example.one", "diagnostics_root": calls[0]["diagnostics_root"], "busy": True,
         "phone": "Android install of one"}
    ]
    assert calls[0]["diagnostics_root"].parts[-2:] == ("play_store", "diagnostics")
    assert not registry.is_platform_busy("android")


def test_the_store_set_in_the_device_config_does_the_install(installer, config_root, monkeypatch):
    app, _, calls, _ = installer
    monkeypatch.setitem(store.APP_STORES, "other_store", replace(store.APP_STORES["play_store"], key="other_store"))
    entry = config_root / "configs/android.yaml"
    entry.write_text(entry.read_text().replace("device:\n", "device:\n  app_store: \"other_store\"\n", 1))

    status, body = asyncio.run(call(app, "POST", "/config/android/apps/one/install"))

    assert status == 200, body
    assert calls[-1]["diagnostics_root"].parts[-2:] == ("other_store", "diagnostics")


@pytest.mark.parametrize("name", ["apk", "play_store", "aurora_store"])
def test_each_install_method_can_be_set_through_the_api(installer, name):
    app, _, _, _ = installer

    status, body = asyncio.run(call(app, "PUT", "/config/android/device", {"app_store": name}))

    assert status == 200, body
    assert body["app_store"] == name


@pytest.mark.parametrize("name", ["app_gallery", "galaxy_store"])
def test_an_unknown_store_is_refused(installer, name):
    app, _, _, _ = installer

    status, _ = asyncio.run(call(app, "PUT", "/config/android/device", {"app_store": name}))

    assert status == 422


def test_a_paid_app_comes_back_as_a_blocker_that_needs_a_person(installer):
    app, _, _, outcome = installer
    outcome["value"] = failure("paid", diagnostics=None)

    status, body = asyncio.run(call(app, "POST", "/config/android/apps/one/install"))

    assert status == 200
    assert body["status"] == "failed"
    assert body["blocker_code"] == "app_paid"
    assert body["retryable"] is False
    assert (body["version_name"], body["version_code"]) == (None, None)


def test_diagnostics_paths_stay_on_the_host(installer, tmp_path):
    app, _, _, outcome = installer
    outcome["value"] = failure("unrecognised", diagnostics=tmp_path / "saved")

    _, body = asyncio.run(call(app, "POST", "/config/android/apps/one/install"))

    assert body["blocker_code"] == "store_screen_unrecognised"
    assert "diagnostics" not in body


def test_a_busy_device_is_refused_with_its_blocker(installer):
    app, registry, calls, _ = installer
    assert registry.try_claim_platform("android")

    status, body = asyncio.run(call(app, "POST", "/config/android/apps/one/install"))

    assert status == 409
    assert body["detail"]["blocker_code"] == "platform_busy"
    assert calls == []
    assert registry.is_platform_busy("android")


def test_the_device_is_released_even_when_the_install_raises(installer):
    app, registry, _, outcome = installer
    outcome["value"] = RuntimeError("adb went away")

    with pytest.raises(RuntimeError):
        asyncio.run(call(app, "POST", "/config/android/apps/one/install"))

    assert not registry.is_platform_busy("android")
    assert phone_lock_holder() is None


def test_a_phone_held_by_another_process_refuses_the_install(installer, phone_held_by_another_process):
    app, registry, calls, _ = installer

    status, body = asyncio.run(call(app, "POST", "/config/android/apps/one/install"))

    assert status == 409
    assert body["detail"] == {"blocker_code": "platform_busy", "message": "Another run is already using the test device."}
    assert calls == []
    assert not registry.is_platform_busy("android")


def test_unavailable_appium_is_a_retryable_failure(installer, monkeypatch):
    app, _, calls, _ = installer
    monkeypatch.setattr(
        app_install,
        "ensure_appium_running",
        lambda url, auto_start, log_path, environment: AppiumStartResult("FAILED", error="appium exited"),
    )

    _, body = asyncio.run(call(app, "POST", "/config/android/apps/one/install"))

    assert body["blocker_code"] == "app_install_failed"
    assert body["retryable"] is True
    assert "appium exited" in body["detail"]
    assert calls == []


@pytest.mark.parametrize(("path", "expected"), [("/config/ios/apps/one/install", 422), ("/config/android/apps/missing/install", 404)])
def test_only_configured_android_apps_are_installed(installer, path, expected):
    app, _, calls, _ = installer

    status, _ = asyncio.run(call(app, "POST", path))

    assert status == expected
    assert calls == []
