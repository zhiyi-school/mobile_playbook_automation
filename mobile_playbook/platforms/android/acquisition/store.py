"""
Resolves the install method named by device.app_store and names the store apps that tamper checks treat as a redirect.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from mobile_playbook.platforms.android.acquisition import apk, aurora_store, play_store
from mobile_playbook.platforms.android.acquisition.play_store import InstallOutcome


@dataclass(frozen=True)
class AppStore:
    key: str
    name: str
    install: Callable[..., InstallOutcome]
    failure: Callable[..., InstallOutcome]


APP_STORES: dict[str, AppStore] = {
    "apk": AppStore(key="apk", name=apk.STORE_NAME, install=apk.install_from_apk, failure=apk.failure),
    "play_store": AppStore(
        key="play_store",
        name=play_store.STORE_NAME,
        install=play_store.install_from_play_store,
        failure=play_store.failure,
    ),
    "aurora_store": AppStore(
        key="aurora_store",
        name=aurora_store.STORE_NAME,
        install=aurora_store.install_from_aurora_store,
        failure=aurora_store.failure,
    ),
}
DEFAULT_APP_STORE = "play_store"

STORE_APPS: dict[str, str] = {
    play_store.PLAY_STORE_PACKAGE: play_store.STORE_NAME,
    aurora_store.AURORA_STORE_PACKAGE: aurora_store.STORE_NAME,
}


# Return the install method an Android config selects; the config loader has already rejected unknown names.
def app_store_for(config) -> AppStore:
    return APP_STORES[config.device.app_store]
