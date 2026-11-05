"""
A synthetic Corellium baseline bench: two automation VMs, their adb devices, an intake APK and a baseline config,
with a fake Play Store for the phone and helpers that run it and read what the run recorded.
"""

from __future__ import annotations

import functools
import json
import zipfile
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from mobile_playbook.orchestration.appium_server import AppiumStartResult
from mobile_playbook.orchestration.scan_runner import RunOptions, run_platform
from mobile_playbook.platforms.android import play_services
from mobile_playbook.platforms.android import runner as runner_module
from mobile_playbook.platforms.android.acquisition import intake_apk, play_store
from mobile_playbook.platforms.android.acquisition.play_store import InstallOutcome
from mobile_playbook.platforms.android.acquisition.store import APP_STORES
from mobile_playbook.platforms.android.device import adb as adb_module
from mobile_playbook.platforms.android.device import adb_connection, corellium_roles
from mobile_playbook.platforms.android.device.adb_connection import AdbConnectPolicy
from mobile_playbook.platforms.android.device.debugging_prompt import DebuggingAuthorizer
from mobile_playbook.platforms.android.config import load_config
from mobile_playbook.platforms.android.models import AndroidRiskRunResult
from mobile_playbook.platforms.android.results import normalize_android_result
from mobile_playbook.platforms.android.runner import AndroidPlatformRunner
from mobile_playbook.platforms.corellium import profile as profile_module
from mobile_playbook.platforms.corellium.claims import ClaimStore
from mobile_playbook.platforms.corellium.client import CorelliumClient
from mobile_playbook.platforms.corellium.lifecycle import LifecyclePolicy
from mobile_playbook.reporting.report_writer import ReportWriter
from mobile_playbook.reporting.run_events import read_events
from tests.platforms.android.adb_fakes import FakeAdbHost
from tests.platforms.android.manifest_fakes import manifest_tree
from tests.platforms.corellium.helpers import FakeAPI, device

STOCK_VM, ROOTED_VM = "stock-vm", "rooted-vm"
STOCK_SERIAL, ROOTED_SERIAL = "10.11.1.1:5001", "10.11.1.2:5001"
PHONE_SERIAL = "PHONE0001"
PLAIN, ROOT, STATIC = "android-feature-06-risk-01", "android-feature-01-risk-02", "android-feature-01-risk-01"
PACKAGE = "com.example.one"
APK_NAME = "One_3.2.1.apk"
FAST = LifecyclePolicy(restore_timeout=2, power_timeout=0.5, poll=0.001)
ADB_POLICY = AdbConnectPolicy(timeout=1, reconnect_pause=0.001, unauthorized_grace=0, poll=0.001)


class EvidenceRisk:
    """Writes a host evidence file and a PASSED result naming the serial it ran on."""

    test_case_id = "synthetic"
    test_case_type = "synthetic"

    def __init__(self, risk_id, requires=(), requires_device=True):
        self.risk_id = risk_id
        self.requires = list(requires)
        self.requires_device = requires_device
        self.used: list[str | None] = []
        self.fail_with: BaseException | None = None
        self.on_run = None

    def run(self, app, config, device_client, report_writer):
        serial = device_client.adb.serial if device_client is not None else None
        self.used.append(serial)
        if self.on_run is not None:
            self.on_run()
        if self.fail_with is not None:
            raise self.fail_with
        report_dir = report_writer.test_report_dir(app.id, self.risk_id, self.test_case_id, platform="android")
        evidence = report_dir / "evidence.txt"
        evidence.write_text(f"ran on {serial}\n")
        now = datetime.now().astimezone().isoformat()
        report_writer.write_result(AndroidRiskRunResult(
            run_timestamp=report_writer.run_timestamp, timestamp_start=now, timestamp_end=now, app_id=app.id,
            app_name=app.name, package_name=app.package_name, risk_id=self.risk_id, test_case_id=self.test_case_id,
            test_case_type=self.test_case_type, final_status="PASSED", verdict="Reduced Risk",
            evidence=[{"kind": "file", "path": str(evidence), "label": "evidence"}],
        ), report_dir)


def baseline_device_yaml(roles=("non_rooted", "rooted"), physical_roles=(), phone_serial=PHONE_SERIAL, phone_app_store=None) -> str:
    selectors = {
        "non_rooted": f"      non_rooted:\n        project: automation\n        instance: {STOCK_VM}\n        adb_address: \"{STOCK_SERIAL}\"\n",
        "rooted": f"      rooted:\n        project: automation\n        instance: {ROOTED_VM}\n        adb_address: \"{ROOTED_SERIAL}\"\n",
    }
    return (
        "device:\n"
        "  provider: corellium\n"
        "  app_store: apk\n"
        "  appium_server_url: \"http://127.0.0.1:4723\"\n"
        + (f"  adb_serial: \"{phone_serial}\"\n  physical_roles: [{', '.join(physical_roles)}]\n" if physical_roles else "")
        + (f"  phone_app_store: {phone_app_store}\n" if phone_app_store else "")
        + "  corellium:\n"
        "    auto_start: true\n"
        "    auto_stop: true\n"
        "    roles:\n"
        + "".join(selectors[role] for role in roles if role not in physical_roles)
        + "    baselines:\n"
        f"      {STOCK_VM}:\n        rooted: false\n        os_version: \"16.0.0\"\n        osbuild: \"r2 user\"\n"
        f"        snapshot_id: baseline-{STOCK_VM}\n"
        f"      {ROOTED_VM}:\n        rooted: true\n        os_version: \"16.0.0\"\n        osbuild: \"r2 userdebug\"\n"
        f"        snapshot_id: baseline-{ROOTED_VM}\n"
    )


def write_config(root: Path, risks, roles=("non_rooted", "rooted"), physical_roles=(), phone_app_store=None) -> Path:
    configs = root / "configs"
    (configs / "split/android").mkdir(parents=True, exist_ok=True)
    (configs / "split/android/apps.yaml").write_text(
        "apps:\n  - id: \"one\"\n    name: \"One\"\n    package_name: \"com.example.one\"\n    risks:\n"
        + "".join(f"      {risk}:\n        enabled: true\n" for risk in risks)
    )
    (configs / "android.yaml").write_text(
        baseline_device_yaml(roles, physical_roles, phone_app_store=phone_app_store) + "\nrunner:\n  auto_grant_permissions: false\n\ninclude:\n  apps: split/android/apps.yaml\n"
    )
    return configs / "android.yaml"


class BaselineBench:
    """Wires the fakes together; CLI and API code paths reach the same fakes through the default constructors."""

    def __init__(self, root: Path, monkeypatch, *, risks=(PLAIN, ROOT), roles=("non_rooted", "rooted"), physical_roles=(),
                 phone_app_store=None):
        self.root = root
        self.api = FakeAPI(device(STOCK_VM, "non_rooted"), device(ROOTED_VM, "rooted"))
        monkeypatch.setenv("CORELLIUM_STATE_DIR", str(root / "corellium-state"))
        self.store = ClaimStore(self.api.api_url)
        monkeypatch.setattr(CorelliumClient, "from_env", classmethod(lambda cls, env_path=None: self.api))
        monkeypatch.setattr(profile_module, "LifecyclePolicy", functools.partial(LifecyclePolicy, poll=0.001))
        monkeypatch.setattr(adb_connection, "AdbConnectPolicy", functools.partial(AdbConnectPolicy, poll=0.001))
        monkeypatch.setattr(corellium_roles, "DebuggingAuthorizer", functools.partial(
            DebuggingAuthorizer, prompt_timeout=0.02, approval_timeout=0.05, poll=0.001))

        self.adb = FakeAdbHost()
        monkeypatch.setattr(adb_module.subprocess, "run", self.adb.run)
        self.adb.add(STOCK_SERIAL, connects_to="device", build_type="user")
        self.adb.add(ROOTED_SERIAL, connects_to="device", build_type="userdebug")
        self.adb.apks[APK_NAME] = (PACKAGE, "3.2.1", "321")

        monkeypatch.setenv("INTAKE_DIR", str(root / "intake"))
        self.apk = root / "intake/android/apks" / APK_NAME
        self.apk.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(self.apk, "w") as archive:
            archive.writestr("AndroidManifest.xml", b"synthetic")
        metadata = {APK_NAME: {"package_name": PACKAGE, "version_name": "3.2.1", "version_code": "321"}}
        monkeypatch.setattr(intake_apk, "inspect_apk_metadata", lambda path: metadata[Path(path).name])
        monkeypatch.setattr(intake_apk, "_scan_cache", {})
        self.declares_play_services = False
        monkeypatch.setattr(play_services, "manifest_tree",
                            lambda path: manifest_tree(play_services=self.declares_play_services, package=PACKAGE))

        self.risks = {PLAIN: EvidenceRisk(PLAIN, ["adb"]), ROOT: EvidenceRisk(ROOT, ["adb", "root"]),
                      STATIC: EvidenceRisk(STATIC, requires_device=False)}
        monkeypatch.setattr(runner_module, "get_risk", self.risks.get)
        monkeypatch.setattr(runner_module, "check_android_preflight",
                            lambda config, adb, requires: SimpleNamespace(ok=True, errors=[], warnings=[]))
        monkeypatch.setattr(runner_module, "wake_and_unlock", lambda adb: None)
        monkeypatch.setattr(runner_module, "ensure_appium_running",
                            lambda *args, **kwargs: AppiumStartResult("ALREADY_RUNNING"))
        self.appium_reachable: list[bool] = []
        monkeypatch.setattr(runner_module, "tcp_reachable",
                            lambda url, timeout: self.appium_reachable.pop(0) if self.appium_reachable else True)
        self.config_path = write_config(root, risks, roles, physical_roles, phone_app_store)
        if physical_roles:
            self.phone = self.adb.add(PHONE_SERIAL, state="device", build_type="user")

    def runner(self, recognizer=None) -> AndroidPlatformRunner:
        return AndroidPlatformRunner(corellium_client=self.api, claim_store=self.store, lifecycle_policy=FAST,
                                     adb_policy=ADB_POLICY, recognizer=recognizer)

    def actions(self, instance_id: str, *names: str) -> list[str]:
        return [action for action, target in self.api.data["calls"] if target == instance_id and (not names or action in names)]

    def stuck_off(self, instance_id: str) -> None:
        self.api.data["stuck"].add(instance_id)

    def report_version(self, version: str, build: str) -> None:
        self.adb.apks[APK_NAME] = (PACKAGE, version, build)

    def instance_state(self, instance_id: str) -> str:
        return self.api.data["instances"][instance_id].state

    def turn_on(self, instance_id: str) -> None:
        self.api.data["instances"][instance_id] = replace(self.api.data["instances"][instance_id], state="on")


def run(bench, tmp_path, *risks):
    return run_platform(
        load_config(bench.config_path), bench.runner(),
        RunOptions(out_dir=tmp_path / "reports", selected_tests=set(risks) or None),
        lambda out, ts: ReportWriter(out, ts, result_adapter=normalize_android_result, platform="android"),
    )


def events(run_dir, kind=None):
    found, _ = read_events(run_dir)
    return [event for event in found if kind is None or event["type"] == kind]


def last_run_dir(tmp_path):
    return max((tmp_path / "reports").iterdir())


def report(run_dir, risk_id):
    return json.loads((run_dir / "android/one" / risk_id / "synthetic/report.json").read_text())


def phone_commands(bench, *prefix):
    return [args for serial, args in bench.adb.commands if serial == PHONE_SERIAL and args[:len(prefix)] == list(prefix)]


class FakePlayStore:
    """Installs the app on the bench's phone as the Play Store would, or fails with a store failure state."""

    def __init__(self, bench, version=("3.3.0", "330")):
        self.bench = bench
        self.version = version
        self.installs: list[str] = []
        self.fail_with: str | None = None

    def install(self, adb, make_driver, package, diagnostics_root):
        self.installs.append(package)
        if self.fail_with:
            return play_store.failure(self.fail_with)
        if package in self.bench.phone.packages:
            return InstallOutcome(status="already_installed", installer=self.bench.phone.installers.get(package))
        self.bench.phone.packages[package] = self.version
        self.bench.phone.installers[package] = play_store.PLAY_STORE_PACKAGE
        return InstallOutcome(status="installed", installer=play_store.PLAY_STORE_PACKAGE)


def use_fake_play_store(bench, monkeypatch) -> FakePlayStore:
    fake = FakePlayStore(bench)
    monkeypatch.setitem(APP_STORES, "play_store", replace(APP_STORES["play_store"], install=fake.install))
    return fake
