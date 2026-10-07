from __future__ import annotations

import pytest

from mobile_playbook.platforms.android.acquisition import apk, aurora_store, play_store
from mobile_playbook.platforms.android.acquisition.store import APP_STORES, app_store_for
from mobile_playbook.platforms.android.config import ConfigError, parse_config, validate_config

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
