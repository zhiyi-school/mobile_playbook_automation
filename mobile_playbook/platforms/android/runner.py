from __future__ import annotations

from mobile_playbook.orchestration.artifact_intake import app_matches_selector
from mobile_playbook.platforms.android.adb import AdbClient
from mobile_playbook.platforms.android.device_client import AndroidDeviceClient
from mobile_playbook.platforms.android.permissions import grant_all
from mobile_playbook.platforms.android.preflight import check_android_preflight
from mobile_playbook.platforms.android.risks import get_risk


class AndroidPlatformRunner:
    platform = "android"

    def requires_device(self, config, selected_tests: set[str] | None, selected_apps: set[str] | None = None) -> bool:
        for app in config.apps:
            if not app_matches_selector(app, selected_apps):
                continue
            for risk_id in self.enabled_test_ids(app, selected_tests):
                risk = get_risk(risk_id)
                if risk is not None and getattr(risk, "requires_device", True):
                    return True
        return False

    def connect_device(self, config):
        adb = AdbClient(config.device.adb_path, config.device.adb_serial)
        return AndroidDeviceClient(config, adb).connect()

    def close_device(self, device_client) -> None:
        device_client.quit()

    def iter_enabled_tests(self, config, selected_tests: set[str] | None, selected_apps: set[str] | None):
        for app in config.apps:
            if not app_matches_selector(app, selected_apps):
                continue
            for risk_id in self.enabled_test_ids(app, selected_tests):
                yield app, risk_id

    def run_test(self, app, test_id: str, config, device_client, report_writer) -> None:
        risk = get_risk(test_id)
        if risk is None:
            return
        preflight = check_android_preflight(config, device_client.adb, getattr(risk, "requires", []))
        if not preflight.ok:
            raise RuntimeError("; ".join(preflight.errors))
        if config.runner.auto_grant_permissions:
            grant_all(device_client.adb, app.package_name)
        risk.run(app, config, device_client, report_writer)

    def enabled_test_ids(self, app, selected_tests: set[str] | None):
        for risk_id, risk_config in app.risks.items():
            if selected_tests and risk_id not in selected_tests:
                continue
            if risk_config.get("enabled", False):
                yield risk_id

    def dry_run_lines(self, config, selected_tests: set[str] | None, selected_apps: set[str] | None = None) -> list[str]:
        lines = ["Dry run: no Android device, Appium session, APK install, or repackaging files will be touched."]
        for app in config.apps:
            if not app_matches_selector(app, selected_apps):
                continue
            lines.append(f"App: {app.id} ({app.package_name})")
            for risk_id in self.enabled_test_ids(app, selected_tests):
                lines.append(f"  planned: {risk_id}")
        return lines
