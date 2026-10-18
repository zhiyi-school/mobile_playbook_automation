from types import SimpleNamespace

import pytest

from mobile_playbook.orchestration.scan_runner import RunOptions, run_platform
from mobile_playbook.platforms.android.device.adb import AdbClient
from mobile_playbook.platforms.android.device.adb_connection import AdbConnectPolicy
from mobile_playbook.platforms.android.device.client import AndroidDeviceClient
from mobile_playbook.platforms.android.device.debugging_prompt import DebuggingAuthorizer
from mobile_playbook.platforms.android.device.corellium import CorelliumConnection
from mobile_playbook.platforms.android.device.pool import AndroidDevicePool, RoleDevice
from mobile_playbook.platforms.android.runner import AndroidPlatformRunner
from mobile_playbook.platforms.corellium.client import CorelliumClient, CorelliumInstance
from mobile_playbook.platforms.corellium.lifecycle import CorelliumLifecycle
from mobile_playbook.reporting.report_writer import ReportWriter
from mobile_playbook.reporting.run_events import read_events
from tests.platforms.android.prompt_fakes import png, prompt_words

STOCK, ROOTED = "10.11.1.1:5001", "10.11.1.2:5001"
POLICY = AdbConnectPolicy(timeout=1, reconnect_pause=0.001, unauthorized_grace=0, poll=0.001)
APP = SimpleNamespace(id="example_app", name="Example", package_name="com.example.one")


class RecordingRisk:
    test_case_id = "synthetic"
    test_case_type = "synthetic"

    def __init__(self, risk_id, requires=(), requires_device=True):
        self.risk_id = risk_id
        self.requires = list(requires)
        self.requires_device = requires_device
        self.used = []

    def run(self, app, config, device_client, report_writer):
        self.used.append(getattr(getattr(device_client, "adb", None), "serial", device_client))


@pytest.fixture
def risks(monkeypatch):
    table = {
        "plain": RecordingRisk("plain", ["adb"]),
        "rooted": RecordingRisk("rooted", ["adb", "root"]),
        "static": RecordingRisk("static", requires_device=False),
        "malformed": RecordingRisk("malformed", requires_device="yes"),
    }
    monkeypatch.setattr("mobile_playbook.platforms.android.runner.get_risk", table.get)
    monkeypatch.setattr("mobile_playbook.platforms.android.runner.check_android_preflight",
                        lambda config, adb, requires: SimpleNamespace(ok=True, errors=[], warnings=[]))
    monkeypatch.setattr("mobile_playbook.platforms.android.runner.wake_and_unlock", lambda adb: None)
    return table


@pytest.fixture
def no_preparation(monkeypatch):
    def refuse(*args, **kwargs):
        pytest.fail("device recovery must not select, restore, start or stop a device")

    for name in ("prepare", "cleanup", "session", "recover"):
        monkeypatch.setattr(CorelliumLifecycle, name, refuse)
    for name in ("restore_snapshot", "start_instance", "stop_instance", "unpause_instance", "list_instances", "list_projects"):
        monkeypatch.setattr(CorelliumClient, name, refuse)
    for target in ("runner.open_device", "runner.connect_claimed", "device.corellium.find_instance",
                   "device.corellium.ensure_instance_on"):
        monkeypatch.setattr(f"mobile_playbook.platforms.android.{target}", refuse)


def config(keep_screen_on=False):
    return SimpleNamespace(
        device=SimpleNamespace(appium_server_url="http://127.0.0.1:4723", adb_path="adb", appium_auto_start={},
                               provider="corellium", corellium={"project": "automation", "rooted": False}),
        runner=SimpleNamespace(auto_grant_permissions=False, keep_screen_on=keep_screen_on),
    )


def make_pool(adb_host, *roles, authorizers=None):
    devices = {}
    for role, serial, rooted, build in (("non_rooted", STOCK, False, "user"), ("rooted", ROOTED, True, "userdebug")):
        adb_host.add(serial, state="device", build_type=build)
        if role in roles:
            devices[role] = RoleDevice(role, f"{role}-vm", serial, rooted,
                                       AndroidDeviceClient(None, AdbClient("adb", serial)), (authorizers or {}).get(role))
    return AndroidDevicePool(devices, adb_path="adb", policy=POLICY)


def run_risks(risk_ids, device_client):
    runner = AndroidPlatformRunner()
    failures = []
    runner._failure_result_template = lambda *args: SimpleNamespace()
    runner._record_failure = lambda result, writer, exc: failures.append(str(exc))
    for risk_id in risk_ids:
        runner.run_test(APP, risk_id, config(), device_client, None)
    return failures


def test_each_risk_runs_on_its_roles_device_in_plan_order(adb_host, risks):
    pool = make_pool(adb_host, "non_rooted", "rooted")

    assert run_risks(["rooted", "plain", "static", "rooted"], pool) == []

    assert risks["rooted"].used == [ROOTED, ROOTED]
    assert risks["plain"].used == [STOCK]
    assert risks["static"].used == [None]


def test_a_root_risk_fails_when_no_rooted_device_was_prepared(adb_host, risks):
    pool = make_pool(adb_host, "non_rooted")

    failures = run_risks(["rooted", "plain"], pool)

    assert len(failures) == 1 and "no prepared rooted device" in failures[0]
    assert risks["rooted"].used == []
    assert risks["plain"].used == [STOCK]


def test_a_non_rooted_risk_never_falls_back_to_the_rooted_device(adb_host, risks):
    pool = make_pool(adb_host, "rooted")

    failures = run_risks(["plain"], pool)

    assert "no prepared non_rooted device" in failures[0]
    assert risks["plain"].used == []


def test_a_single_physical_device_still_runs_every_risk(risks):
    phone = SimpleNamespace(adb=SimpleNamespace(serial="PHONE1"))

    assert run_risks(["rooted", "plain"], phone) == []

    assert risks["rooted"].used == ["PHONE1"]
    assert risks["plain"].used == ["PHONE1"]


def test_a_malformed_declaration_fails_the_risk_without_running_it(adb_host, risks):
    failures = run_risks(["malformed"], make_pool(adb_host, "non_rooted"))

    assert "requires_device must be true or false" in failures[0]
    assert risks["malformed"].used == []


def test_installed_versions_are_read_from_every_role_and_must_agree(adb_host):
    pool = make_pool(adb_host, "non_rooted", "rooted")
    runner = AndroidPlatformRunner()

    assert runner.installed_app_version(APP, pool) is None
    adb_host.devices[STOCK].packages[APP.package_name] = ("1.0", "10")
    assert runner.installed_app_version(APP, pool) == ("1.0", "10")
    adb_host.devices[ROOTED].packages[APP.package_name] = ("1.1", "11")
    with pytest.raises(RuntimeError, match="different installed versions"):
        runner.installed_app_version(APP, pool)


def appium(monkeypatch, reachable):
    started = []
    monkeypatch.setattr("mobile_playbook.platforms.android.runner.tcp_reachable", lambda url, timeout: reachable)
    monkeypatch.setattr("mobile_playbook.platforms.android.runner.ensure_appium_running",
                        lambda *args: started.append(args) or SimpleNamespace(status="STARTED", error=None, log_tail=None, log_path="appium.log"))
    return started


class PromptOnlyCorellium:
    def __init__(self, on_touch):
        self.on_touch = on_touch
        self.calls = []

    def screenshot(self, instance_id, format="png"):
        self.calls.append(("screenshot", instance_id))
        return png(1080, 2400)

    def touch(self, instance_id, x, y):
        self.calls.append(("touch", instance_id))
        self.on_touch()


def test_appium_recovery_reconnects_each_role_to_its_own_instance_without_preparing(adb_host, monkeypatch, tmp_path, no_preparation):
    started = appium(monkeypatch, reachable=False)
    corellium = PromptOnlyCorellium(lambda: setattr(adb_host.devices[ROOTED], "state", "device"))
    approve = DebuggingAuthorizer(corellium, "rooted-vm", ROOTED, diagnostics_dir=tmp_path, poll=0.001,
                                  recognizer=SimpleNamespace(words=lambda image: prompt_words()))
    pool = make_pool(adb_host, "non_rooted", "rooted", authorizers={"rooted": approve})
    adb_host.devices[STOCK].state = "offline"
    adb_host.devices[ROOTED].state = "unauthorized"

    recovered = AndroidPlatformRunner().ensure_device_healthy(config(), pool, tmp_path)

    assert recovered is pool
    assert recovered.identities() == {"non_rooted": {"instance_id": "non_rooted-vm", "serial": STOCK},
                                      "rooted": {"instance_id": "rooted-vm", "serial": ROOTED}}
    assert len(started) == 1
    assert adb_host.devices[STOCK].state == adb_host.devices[ROOTED].state == "device"
    assert corellium.calls == [("screenshot", "rooted-vm"), ("touch", "rooted-vm")]
    assert [event["type"] for event in read_events(tmp_path)[0]] == ["appium_recovery"]


def test_a_healthy_appium_leaves_the_pool_untouched(adb_host, monkeypatch, tmp_path, no_preparation):
    started = appium(monkeypatch, reachable=True)
    pool = make_pool(adb_host, "non_rooted")

    assert AndroidPlatformRunner().ensure_device_healthy(config(), pool, tmp_path) is pool
    assert started == []
    assert adb_host.commands == []


def test_single_instance_recovery_reconnects_the_same_instance(monkeypatch, tmp_path, no_preparation):
    appium(monkeypatch, reachable=False)
    instance = CorelliumInstance(id="vm", name="vm", state="on", service_ip="10.11.1.1", type="android")
    connection = CorelliumConnection(instance=instance, serial=STOCK, auto_stop=True)
    client = AndroidDeviceClient(config(), AdbClient("adb", STOCK), corellium=connection)
    reconnected = []
    monkeypatch.setattr("mobile_playbook.platforms.android.runner.reconnect_corellium",
                        lambda adb_path, used, settings: reconnected.append(used))
    monkeypatch.setattr(client, "release", lambda: pytest.fail("recovery must not release the instance"))

    assert AndroidPlatformRunner().ensure_device_healthy(config(), client, tmp_path) is client
    assert reconnected == [connection]


def test_role_devices_are_connected_with_diagnostics_in_the_run_folder(monkeypatch, tmp_path, adb_host):
    appium(monkeypatch, reachable=True)
    pool = make_pool(adb_host, "non_rooted", "rooted")
    seen = {}

    def connect(config, devices, prepared, client, **kwargs):
        seen.update(kwargs, devices=devices, prepared=prepared, client=client)
        return pool

    monkeypatch.setattr("mobile_playbook.platforms.android.runner.connect_claimed", connect)
    kept_on = []
    monkeypatch.setattr("mobile_playbook.platforms.android.runner.keep_screen_on_while_charging",
                        lambda adb: kept_on.append(adb.serial) or True)

    result = AndroidPlatformRunner().connect_role_devices(config(keep_screen_on=True), tmp_path, "devices", "prepared", "api")

    assert result is pool
    assert (seen["devices"], seen["prepared"], seen["client"], seen["diagnostics_dir"]) == ("devices", "prepared", "api", tmp_path)
    assert kept_on == [STOCK, ROOTED]


def test_a_mixed_run_routes_risks_in_order_through_the_shared_runner(adb_host, monkeypatch, tmp_path, risks, no_preparation):
    appium(monkeypatch, reachable=True)
    pool = make_pool(adb_host, "non_rooted", "rooted")
    adb_host.devices[STOCK].packages[APP.package_name] = ("1.0", "10")
    planned = [(APP, "plain"), (APP, "rooted"), (APP, "static"), (APP, "plain")]

    class PoolRunner(AndroidPlatformRunner):
        def requires_device(self, config, selected_tests, selected_apps=None):
            return True

        def connect_device(self, config, run_dir=None):
            return pool

        def iter_enabled_tests(self, config, selected_tests, selected_apps):
            yield from planned

    run_platform(config(), PoolRunner(), RunOptions(out_dir=tmp_path),
                 lambda out_dir, run_timestamp: ReportWriter(out_dir, run_timestamp))

    assert risks["plain"].used == [STOCK, STOCK]
    assert risks["rooted"].used == [ROOTED]
    assert risks["static"].used == [None]
    assert adb_host.sent(None, "disconnect", STOCK) == adb_host.sent(None, "disconnect", ROOTED) == 1
