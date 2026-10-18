import hashlib
import json

import pytest

from mobile_playbook.platforms.android import play_services
from mobile_playbook.platforms.android.device import adb as adb_module
from tests.platforms.android.adb_fakes import FakeAdbHost


@pytest.fixture(autouse=True)
def fresh_play_services_cache():
    play_services._apk_declares_play_services.cache_clear()
    yield
    play_services._apk_declares_play_services.cache_clear()


@pytest.fixture
def adb_host(monkeypatch) -> FakeAdbHost:
    host = FakeAdbHost()
    monkeypatch.setattr(adb_module.subprocess, "run", host.run)
    return host


@pytest.fixture
def write_apk_set(apk_metadata):
    def write(folder, *, package="com.example.app", version_code="7", files=None):
        folder.mkdir(parents=True, exist_ok=True)
        records = []
        for name, data in (files or {"base.apk": b"base", "split_config.en.apk": b"split"}).items():
            (folder / name).write_bytes(data)
            apk_metadata[name] = {"package_name": package, "version_code": version_code,
                                  "version_name": "1.2.3", "display_name": "Example"}
            records.append({"name": name, "sha256": hashlib.sha256(data).hexdigest()})
        (folder / "apk-set.json").write_text(json.dumps({"schema_version": 1, "package_name": package,
                                                       "version_code": version_code, "files": records}))
        return [folder / "base.apk", *(folder / name for name in sorted(files or {"split_config.en.apk": b""}) if name != "base.apk")]
    return write
