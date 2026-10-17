import pytest

from mobile_playbook.platforms.android.device import adb as adb_module
from tests.platforms.android.adb_fakes import FakeAdbHost


@pytest.fixture
def adb_host(monkeypatch) -> FakeAdbHost:
    host = FakeAdbHost()
    monkeypatch.setattr(adb_module.subprocess, "run", host.run)
    return host
