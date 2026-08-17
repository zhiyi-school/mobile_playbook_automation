from __future__ import annotations

from pathlib import Path

from mobile_playbook.platforms.ios.artifacts.registry import get_provider
from mobile_playbook.orchestration.artifact_intake import app_matches_selector
from mobile_playbook.platforms.ios.device import AppiumDeviceClient
from mobile_playbook.platforms.ios.preflight import check_ios_preflight
from mobile_playbook.platforms.ios.risks import get_risk


class IosPlatformRunner:
    platform = "ios"

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
        preflight = check_ios_preflight(config)
        if not preflight.ok:
            raise RuntimeError("; ".join(preflight.errors))
        return AppiumDeviceClient(config.device).connect()

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
        risk.run(app, config, device_client, report_writer)

    def enabled_test_ids(self, app, selected_tests: set[str] | None):
        for risk_id, risk_config in app.risks.items():
            if selected_tests and risk_id not in selected_tests:
                continue
            if risk_config.get("enabled", False):
                yield risk_id

    def acquire_artifacts(self, config, selected_apps: set[str] | None, run_timestamp: str, out_dir: Path) -> list[dict]:
        client = None
        results = []
        try:
            if any(
                app_matches_selector(app, selected_apps)
                and (app.artifact.get("source") == "installed_app_reference" or app.artifact.get("require_original_app_installed"))
                for app in config.apps
            ):
                client = self.connect_device(config)
            for app in config.apps:
                if not app_matches_selector(app, selected_apps):
                    continue
                provider = get_provider(app.artifact.get("source", ""))
                if provider is None:
                    print(f"{app.id}: UNSUPPORTED_ARTIFACT_SOURCE")
                    continue
                result = provider.acquire(app, config, client, run_timestamp, out_dir)
                results.append(result.to_dict())
                print(f"{app.id}: {result.status} {result.ipa_path or ''}")
        finally:
            if client is not None:
                self.close_device(client)
        return results

    def dry_run_lines(self, config, selected_tests: set[str] | None, selected_apps: set[str] | None = None) -> list[str]:
        lines = ["Dry run: no files, devices, installs, uninstalls, or Appium session will be touched."]
        for app in config.apps:
            if not app_matches_selector(app, selected_apps):
                continue
            lines.append(f"App: {app.id} ({app.name})")
            lines.append(f"  artifact.source: {app.artifact.get('source')}")
            for risk_id in self.enabled_test_ids(app, selected_tests):
                risk_config = app.risks.get(risk_id) or {}
                if risk_id == "ios-feature5-risk1":
                    collection = risk_config.get("collection") or risk_config.get("control") or {}
                    lines.append("  planned: ios-feature5-risk1 / collection_server / keystroke_collection")
                    lines.append(f"    bind_host: {collection.get('bind_host', '0.0.0.0')}")
                    lines.append(f"    port: {collection.get('port', 8765)}")
                    lines.append(f"    pair_timeout_seconds: {collection.get('pair_timeout_seconds', 60)}")
                    lines.append(f"    evidence_source: {collection.get('evidence_source', 'local_app_ui')}")
                    lines.append(
                        "    evidence_timeout_seconds: "
                        f"{collection.get('evidence_timeout_seconds', collection.get('event_timeout_seconds', 30))}"
                    )
                    lines.append(f"    probe_text: {collection.get('probe_text', 'hello123')}")
                elif risk_id == "ios-feature1-risk1":
                    lines.append("  planned: ios-feature1-risk1 / ipa_static_analysis / package_inventory")
                else:
                    lines.append(f"  planned: {risk_id}")
        return lines
