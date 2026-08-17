from __future__ import annotations

from mobile_playbook.platforms.ios.artifacts.installed_app_reference import InstalledAppReferenceProvider
from mobile_playbook.platforms.ios.artifacts.local_ipa import LocalIpaProvider
from tests.conftest import MockDevice


def test_local_ipa_acquisition(global_config, tmp_path):
    result = LocalIpaProvider().acquire(global_config.apps[0], global_config, None, "run1", tmp_path / "acquired")
    assert result.status == "ACQUIRED"
    assert result.ipa_path.exists()
    assert result.bundle_id == "com.example.app"


def test_installed_app_reference_acquisition_with_mocked_appium(global_config, tmp_path):
    app = global_config.apps[0]
    app.artifact = {"source": "installed_app_reference"}
    device = MockDevice()
    device.installed.add(app.bundle_id)
    result = InstalledAppReferenceProvider().acquire(app, global_config, device, "run1", tmp_path)
    assert result.status == "INSTALLED_APP_VERIFIED"
    assert result.ipa_path is None
