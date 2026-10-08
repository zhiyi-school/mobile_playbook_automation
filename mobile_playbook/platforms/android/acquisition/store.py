"""
Resolves the install method named by device.app_store and names the store apps that tamper checks treat as a redirect.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from mobile_playbook.platforms.android.acquisition import apk, aurora_store, play_store
from mobile_playbook.platforms.android.acquisition.play_store import InstallOutcome
from mobile_playbook.platforms.android.device.adb import AdbClient
from mobile_playbook.platforms.android.device.app_version import installed_version

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AppStore:
    key: str
    name: str
    install: Callable[..., InstallOutcome]
    failure: Callable[..., InstallOutcome]

    # Install the app with this method and, once it is on the device, add the version installed there to the outcome.
    def install_and_read_version(
        self, adb: AdbClient, make_driver: Callable[[], Any], package: str, diagnostics_root: Path
    ) -> InstallOutcome:
        outcome = self.install(adb, make_driver, package, diagnostics_root)
        if outcome.status not in ("installed", "already_installed"):
            return outcome
        version = installed_version(adb, package)
        if version is None:
            logger.debug("android install: %s is %s but reports no version.", package, outcome.status)
            return outcome
        return replace(outcome, version_name=version.version_name, version_code=version.version_code)


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
