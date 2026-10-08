from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from mobile_playbook.platforms.android.acquisition import apk, aurora_store, play_store, store
from mobile_playbook.platforms.android.acquisition.store import APP_STORES, app_store_for
from mobile_playbook.platforms.android.config import ConfigError, parse_config, validate_config
from mobile_playbook.platforms.android.device.app_version import InstalledVersion

APPS = [{"id": "one", "name": "One", "package_name": "com.example.one"}]


@pytest.mark.parametrize(
    ("key", "module", "install"),
    [
        ("apk", apk, apk.install_from_apk),
        ("play_store", play_store, play_store.install_from_play_store),
        ("aurora_store", aurora_store, aurora_store.install_from_aurora_store),
    ],
)
def test_each_install_method_comes_from_its_module(key, module, install):
    selected = APP_STORES[key]

    assert selected.name == module.STORE_NAME
    assert selected.key == module.WORK_DIR_NAME
    assert selected.install is install
    assert selected.failure is module.failure


def test_device_app_store_selects_the_store(monkeypatch):
    monkeypatch.setitem(APP_STORES, "other_store", APP_STORES["play_store"])
    config = parse_config({"device": {"app_store": "other_store"}, "apps": APPS})

    assert config.device.app_store == "other_store"
    assert app_store_for(config) is APP_STORES["other_store"]


def test_the_play_store_is_used_when_no_store_is_set():
    config = parse_config({"device": {}, "apps": APPS})

    assert app_store_for(config) is APP_STORES["play_store"]


@pytest.mark.parametrize("name", ["app_gallery", "galaxy_store"])
def test_an_unknown_store_fails_validation(name):
    config = parse_config({"device": {"app_store": name}, "apps": APPS})

    with pytest.raises(ConfigError) as raised:
        validate_config(config, dry_run=True)

    assert "device.app_store must be one of: apk, play_store, aurora_store" in raised.value.errors


@pytest.mark.parametrize("status", ["installed", "already_installed"])
def test_the_version_on_the_device_is_added_once_the_app_is_there(monkeypatch, status):

    reads: list[str] = []

    def fake_version(adb, package):
        reads.append(package)
        return InstalledVersion(version_name="3.2.1", version_code="321")

    monkeypatch.setattr(store, "installed_version", fake_version)
    method = replace(APP_STORES["apk"], install=lambda *args: play_store.InstallOutcome(status=status, installer="adb"))

    outcome = method.install_and_read_version(object(), lambda: None, "com.example.one", Path("diagnostics"))

    assert reads == ["com.example.one"]
    assert (outcome.status, outcome.installer) == (status, "adb")
    assert (outcome.version_name, outcome.version_code) == ("3.2.1", "321")
    assert outcome.to_dict()["version_name"] == "3.2.1"


def test_a_failed_install_does_not_read_a_version(monkeypatch):
    monkeypatch.setattr(store, "installed_version", lambda adb, package: pytest.fail("read a version"))
    method = replace(APP_STORES["play_store"], install=lambda *args: play_store.failure("paid"))

    outcome = method.install_and_read_version(object(), lambda: None, "com.example.one", Path("diagnostics"))

    assert outcome.blocker_code == "app_paid"
    assert (outcome.version_name, outcome.version_code) == (None, None)


def test_an_installed_app_without_a_readable_version_keeps_its_outcome(monkeypatch):
    installed = play_store.InstallOutcome(status="installed", installer="com.android.vending", seconds=4.0)
    monkeypatch.setattr(store, "installed_version", lambda adb, package: None)
    method = replace(APP_STORES["play_store"], install=lambda *args: installed)

    assert method.install_and_read_version(object(), lambda: None, "com.example.one", Path("diagnostics")) == installed
