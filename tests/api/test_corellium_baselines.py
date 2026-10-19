from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI

from mobile_playbook.api.routes import config as config_routes
from mobile_playbook.api.routes import runs as runs_routes
from mobile_playbook.api.run_registry import JobRegistry
from mobile_playbook.api.services import app_install, provisioning
from mobile_playbook.api.services import runs as runs_service
from mobile_playbook.common.storage_paths import reports_root
from mobile_playbook.platforms.corellium.client import CorelliumClient, CorelliumError
from mobile_playbook.reporting.run_events import read_events
from tests.asgi_helpers import call
from tests.platforms.android.baseline_fakes import (
    PACKAGE,
    PHONE_SERIAL,
    PLAIN,
    ROOT,
    ROOTED_SERIAL,
    ROOTED_VM,
    STATIC,
    STOCK_SERIAL,
    STOCK_VM,
    BaselineBench,
)

MUTATIONS = {"restore", "start", "stop", "unpause"}


def bench_for(tmp_path, monkeypatch, *risks, roles=("non_rooted", "rooted"), physical_roles=()) -> BaselineBench:
    bench = BaselineBench(tmp_path, monkeypatch, risks=risks, roles=roles, physical_roles=physical_roles)
    monkeypatch.setattr(provisioning, "get_android_risk", bench.risks.get)
    return bench


def api() -> FastAPI:
    app = FastAPI()
    app.include_router(config_routes.router)
    app.include_router(runs_routes.router)
    return app


def readiness() -> dict:
    status, body = asyncio.run(call(api(), "GET", "/config/android/apps/one/provisioning"))
    assert status == 200, body
    return body


@pytest.fixture
def registry(monkeypatch):
    registry = JobRegistry(persist_path=None)
    for module in (runs_service, app_install, provisioning):
        monkeypatch.setattr(module, "registry", registry)
    return registry


def test_readiness_of_off_vms_is_runnable_and_never_starts_restores_installs_or_connects(tmp_path, monkeypatch, registry):
    bench = bench_for(tmp_path, monkeypatch, PLAIN, ROOT)

    body = readiness()

    assert body["runnable"] is True and body["device_ready"] is True and body["blocker_code"] is None
    assert body["configuration_ready"] is True
    assert {action for action, _ in bench.api.data["calls"]} <= {"list", "snapshots", "get", "snapshot"}
    assert not {action for action, _ in bench.api.data["calls"]} & MUTATIONS
    assert bench.adb.commands == []
    assert bench.store.pending() == []


def test_readiness_only_checks_the_roles_the_apps_risks_need(tmp_path, monkeypatch, registry):
    bench = bench_for(tmp_path, monkeypatch, PLAIN, STATIC)
    del bench.api.data["instances"][ROOTED_VM]

    body = readiness()

    assert body["runnable"] is True
    assert ("snapshots", ROOTED_VM) not in bench.api.data["calls"]


def test_an_unavailable_rooted_vm_blocks_an_app_that_needs_root_and_says_which_device(tmp_path, monkeypatch, registry):
    bench = bench_for(tmp_path, monkeypatch, PLAIN, ROOT)
    del bench.api.data["instances"][ROOTED_VM]

    body = readiness()

    assert (body["runnable"], body["blocker_code"], body["retryable"]) == (False, "no_device", True)
    assert body["detail"] == "Waiting for a compatible rooted test device to become available."


def test_a_missing_apk_is_a_missing_build_that_the_worker_never_tries_to_install(tmp_path, monkeypatch, registry):
    bench = bench_for(tmp_path, monkeypatch, PLAIN)
    bench.apk.unlink()

    body = readiness()

    assert (body["blocker_code"], body["retryable"]) == ("app_build_missing", True)
    assert body["configuration_ready"] is False


def test_a_vm_with_pending_ownership_reads_as_busy(tmp_path, monkeypatch, registry):
    bench = bench_for(tmp_path, monkeypatch, PLAIN)
    from tests.platforms.corellium.helpers import device

    claims = bench.store.claim_all("another-run", (device(STOCK_VM, "non_rooted"),))
    try:
        body = readiness()
    finally:
        claims.detach()

    assert (body["runnable"], body["blocker_code"], body["retryable"]) == (False, "platform_busy", True)


def test_an_unreachable_corellium_api_is_a_temporary_unreachable_device(tmp_path, monkeypatch, registry):
    bench_for(tmp_path, monkeypatch, PLAIN)

    def unreachable(cls, env_path=None):
        raise CorelliumError("Corellium API not reachable", transient=True)

    monkeypatch.setattr(CorelliumClient, "from_env", classmethod(unreachable))

    body = readiness()

    assert (body["blocker_code"], body["retryable"]) == ("device_unreachable", True)


def test_an_api_run_uses_the_same_lifecycle_and_shuts_down_what_it_prepared(tmp_path, monkeypatch, registry):
    bench = bench_for(tmp_path, monkeypatch, PLAIN, ROOT)
    monkeypatch.setattr(runs_service, "trigger_dashboard_sync", lambda *args: None)
    monkeypatch.setattr(runs_service, "REPORTS_ROOT", reports_root())

    class Inline:
        def __init__(self, target, args, daemon):
            self.target, self.args, self.name = target, args, "inline"

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(runs_service.threading, "Thread", Inline)

    status, body = asyncio.run(call(api(), "POST", "/runs", {"platform": "android", "config_path": str(bench.config_path)}))

    assert status == 202, body
    record = registry.get(body["run_id"])
    assert record.status == "completed", record.error
    assert bench.risks[PLAIN].used == [STOCK_SERIAL] and bench.risks[ROOT].used == [ROOTED_SERIAL]
    for vm in (STOCK_VM, ROOTED_VM):
        assert bench.actions(vm, "restore", "start", "stop") == ["restore", "start", "stop"]
    found, _ = read_events(reports_root() / record.run_timestamp)
    assert [event["role"] for event in found if event["type"] == "app_installed"] == ["non_rooted", "rooted"]
    assert not registry.is_platform_busy("android")


def test_a_standalone_install_defaults_to_the_non_rooted_vm_and_shuts_it_down(tmp_path, monkeypatch, registry):
    bench = bench_for(tmp_path, monkeypatch, PLAIN, ROOT)

    status, body = asyncio.run(call(api(), "POST", "/config/android/apps/one/install"))

    assert status == 200, body
    assert (body["status"], body["version_name"], body["version_code"]) == ("installed", "3.2.1", "321")
    assert bench.actions(STOCK_VM, "restore", "start", "stop") == ["restore", "start", "stop"]
    assert bench.actions(ROOTED_VM) == []
    assert PACKAGE in bench.adb.devices[STOCK_SERIAL].packages
    assert bench.store.pending() == [] and not registry.is_platform_busy("android")


def test_a_standalone_install_can_choose_the_rooted_vm(tmp_path, monkeypatch, registry):
    bench = bench_for(tmp_path, monkeypatch, PLAIN, ROOT)

    status, body = asyncio.run(call(api(), "POST", "/config/android/apps/one/install?role=rooted"))

    assert status == 200, body
    assert bench.actions(ROOTED_VM, "restore", "start", "stop") == ["restore", "start", "stop"]
    assert bench.actions(STOCK_VM) == []


def test_a_failed_standalone_install_still_shuts_the_vm_down(tmp_path, monkeypatch, registry):
    bench = bench_for(tmp_path, monkeypatch, PLAIN)
    bench.report_version("9.9.9", "999")

    status, body = asyncio.run(call(api(), "POST", "/config/android/apps/one/install"))

    assert status == 200, body
    assert (body["status"], body["blocker_code"], body["retryable"]) == ("failed", "app_install_failed", True)
    assert bench.instance_state(STOCK_VM) == "off" and bench.store.pending() == []


def test_a_standalone_install_without_its_apk_never_touches_a_vm(tmp_path, monkeypatch, registry):
    bench = bench_for(tmp_path, monkeypatch, PLAIN)
    bench.apk.unlink()

    status, body = asyncio.run(call(api(), "POST", "/config/android/apps/one/install"))

    assert status == 200, body
    assert (body["status"], body["blocker_code"]) == ("failed", "app_unavailable")
    assert bench.api.data["calls"] == []


def test_choosing_a_role_without_baseline_roles_is_rejected(config_root, registry):
    status, body = asyncio.run(call(api(), "POST", "/config/android/apps/one/install?role=rooted"))

    assert status == 422
    assert body["detail"]["blocker_code"] == "invalid_run_request"


def test_an_unknown_role_is_rejected_at_the_http_boundary(tmp_path, monkeypatch, registry):
    bench_for(tmp_path, monkeypatch, PLAIN)

    status, _ = asyncio.run(call(api(), "POST", "/config/android/apps/one/install?role=admin"))

    assert status == 422


def phone_bench(tmp_path, monkeypatch, *risks):
    return bench_for(tmp_path, monkeypatch, *(risks or (PLAIN, ROOT)), physical_roles=("non_rooted",))


def readiness_for(app_risk, bench) -> dict:
    apps = bench.config_path.parent / "split/android/apps.yaml"
    text = apps.read_text()
    for risk in (PLAIN, ROOT):
        if risk != app_risk:
            text = text.replace(f"      {risk}:\n        enabled: true\n", f"      {risk}:\n        enabled: false\n")
    apps.write_text(text)
    return readiness()


def test_an_app_tested_only_on_the_phone_is_ready_without_contacting_corellium(tmp_path, monkeypatch, registry):
    bench = phone_bench(tmp_path, monkeypatch)

    def no_credentials(cls, env_path=None):
        raise CorelliumError("Corellium credentials are not configured")

    monkeypatch.setattr(CorelliumClient, "from_env", classmethod(no_credentials))

    body = readiness_for(PLAIN, bench)

    assert (body["runnable"], body["device_ready"], body["blocker_code"]) == (True, True, None)
    assert bench.api.data["calls"] == []
    assert {tuple(args) for _, args in bench.adb.commands} <= {("version",), ("devices",), ("shell", "getprop", "ro.build.type")}


@pytest.mark.parametrize(("state", "detail"), [
    ("absent", "Waiting for the non-rooted test phone to be connected and to approve USB debugging."),
    ("unauthorized", "Waiting for the non-rooted test phone to allow USB debugging from the automation host: unlock it "
                     "and tap Allow on its debugging prompt, ticking Always allow."),
])
def test_a_phone_that_is_missing_or_unapproved_blocks_only_apps_tested_on_it(tmp_path, monkeypatch, registry, state, detail):
    bench = phone_bench(tmp_path, monkeypatch)
    bench.phone.state = state

    body = readiness()

    assert (body["runnable"], body["blocker_code"], body["retryable"]) == (False, "no_device", True)
    assert body["detail"] == detail
    assert readiness_for(ROOT, bench)["runnable"] is True


def test_a_phone_in_use_by_another_process_reads_as_busy(tmp_path, monkeypatch, registry, phone_held_by_another_process):
    bench = phone_bench(tmp_path, monkeypatch)

    body = readiness()

    assert (body["runnable"], body["blocker_code"]) == (False, "platform_busy")
    assert readiness_for(ROOT, bench)["runnable"] is True


def test_a_standalone_install_on_the_phone_keeps_the_app_and_never_touches_a_vm(tmp_path, monkeypatch, registry):
    from mobile_playbook.platforms.android.device.phone_lock import phone_lock_holder

    bench = phone_bench(tmp_path, monkeypatch)
    bench.phone.packages[PACKAGE] = ("3.0.0", "300")

    status, body = asyncio.run(call(api(), "POST", "/config/android/apps/one/install"))

    assert status == 200, body
    assert (body["status"], body["version_name"], body["version_code"]) == ("installed", "3.2.1", "321")
    assert bench.phone.packages[PACKAGE] == ("3.2.1", "321")
    assert bench.api.data["calls"] == []
    assert phone_lock_holder() is None and not registry.is_platform_busy("android")


def test_a_standalone_install_on_a_phone_in_use_is_refused_as_busy(tmp_path, monkeypatch, registry, phone_held_by_another_process):
    bench = phone_bench(tmp_path, monkeypatch)

    status, body = asyncio.run(call(api(), "POST", "/config/android/apps/one/install"))

    assert status == 409
    assert body["detail"] == {"blocker_code": "platform_busy", "message": "Another run is already using the test device."}
    assert PACKAGE not in bench.phone.packages and not registry.is_platform_busy("android")


def test_a_standalone_install_can_still_choose_the_rooted_vm_beside_a_phone(tmp_path, monkeypatch, registry):
    bench = phone_bench(tmp_path, monkeypatch)

    status, body = asyncio.run(call(api(), "POST", "/config/android/apps/one/install?role=rooted"))

    assert status == 200, body
    assert bench.actions(ROOTED_VM, "restore", "start", "stop") == ["restore", "start", "stop"]
    assert not [args for serial, args in bench.adb.commands if serial == PHONE_SERIAL]
