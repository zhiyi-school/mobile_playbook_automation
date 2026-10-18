from types import SimpleNamespace

import pytest

from mobile_playbook.common.storage_paths import android_intake_dir
from mobile_playbook.platforms.android import play_services
from mobile_playbook.platforms.android.device.adb import AdbClient
from mobile_playbook.platforms.android.play_services import (
    app_needs_play_services,
    device_has_play_services,
    play_services_problem,
)
from tests.platforms.android.manifest_fakes import manifest_tree

PACKAGE = "com.example.one"
SERIAL = "10.11.1.3:5001"


def app(requires_play_services=None):
    return SimpleNamespace(id="one", name="One", package_name=PACKAGE, requires_play_services=requires_play_services)


@pytest.fixture
def intake_apk(apk_metadata):
    folder = android_intake_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "One_3.2.1.apk"
    path.write_bytes(b"apk")
    apk_metadata[path.name] = {"package_name": PACKAGE, "version_code": "321", "version_name": "3.2.1"}
    return path


@pytest.fixture
def manifests(monkeypatch):
    reads = []
    state = {"play_services": True}

    def read(path):
        reads.append(path)
        return manifest_tree(play_services=state["play_services"], package=PACKAGE)

    monkeypatch.setattr(play_services, "manifest_tree", read)
    return SimpleNamespace(reads=reads, state=state)


@pytest.mark.parametrize("setting", [True, False])
def test_the_app_setting_wins_without_reading_any_apk(monkeypatch, setting):
    monkeypatch.setattr(play_services, "find_intake_apk", lambda package: pytest.fail("the setting decides"))

    assert app_needs_play_services(app(setting)) is setting


@pytest.mark.parametrize("declared", [True, False])
def test_the_intake_apk_manifest_decides_when_the_app_sets_nothing(intake_apk, manifests, declared):
    manifests.state["play_services"] = declared

    assert app_needs_play_services(app()) is declared
    assert manifests.reads == [intake_apk]


def test_the_manifest_is_read_once_per_version_of_the_apk_file(intake_apk, manifests):
    app_needs_play_services(app())
    app_needs_play_services(app())
    assert len(manifests.reads) == 1

    intake_apk.write_bytes(b"a rebuilt apk")
    app_needs_play_services(app())
    assert len(manifests.reads) == 2


def test_the_need_is_unknown_without_an_intake_apk(apk_metadata, manifests):
    assert app_needs_play_services(app()) is None
    assert manifests.reads == []


def test_the_need_is_unknown_when_no_sdk_tool_can_read_the_manifest(intake_apk, monkeypatch):
    def unreadable(path):
        raise RuntimeError("APK inspection needs aapt, aapt2 or apkanalyzer on PATH")

    monkeypatch.setattr(play_services, "manifest_tree", unreadable)

    assert app_needs_play_services(app()) is None


@pytest.mark.parametrize("installed,expected", [(True, True), (False, False)])
def test_the_device_reports_whether_play_services_are_installed_and_enabled(adb_host, installed, expected):
    adb_host.add(SERIAL, state="device", play_services=installed)

    assert device_has_play_services(AdbClient("adb", SERIAL)) is expected
    assert adb_host.sent(SERIAL, "shell", "pm", "list", "packages", "-e", "com.google.android.gms") == 1


def test_a_device_adb_cannot_query_is_unknown_rather_than_missing_play_services(adb_host):
    assert device_has_play_services(AdbClient("adb", SERIAL)) is None


def test_a_similarly_named_package_is_not_mistaken_for_play_services(adb_host, monkeypatch):
    adb_host.add(SERIAL, state="device")
    answer = adb_host._answer
    monkeypatch.setattr(adb_host, "_answer", lambda serial, args: (
        (0, "package:com.google.android.gms.policy_sidecar_aps\n", "") if args[-1] == "com.google.android.gms"
        else answer(serial, args)))

    assert device_has_play_services(AdbClient("adb", SERIAL)) is False


def test_the_problem_names_the_device_role_serial_and_app(adb_host):
    adb_host.add(SERIAL, state="device", play_services=False)

    problem = play_services_problem(app(True), AdbClient("adb", SERIAL), "non_rooted")

    assert "non_rooted device (10.11.1.3:5001)" in problem
    assert "One (com.example.one) needs them" in problem
    assert "requires_play_services: false" in problem


@pytest.mark.parametrize("needs,installed", [(False, False), (None, False), (True, True)])
def test_nothing_blocks_unless_the_app_needs_play_services_and_the_device_lacks_them(adb_host, apk_metadata, needs,
                                                                                   installed):
    adb_host.add(SERIAL, state="device", play_services=installed)

    assert play_services_problem(app(needs), AdbClient("adb", SERIAL), "non_rooted") is None


def test_an_app_that_needs_nothing_never_queries_the_device(adb_host):
    adb_host.add(SERIAL, state="device", play_services=False)

    play_services_problem(app(False), AdbClient("adb", SERIAL))

    assert adb_host.commands == []
