from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from mobile_playbook.core.config_files import merge_dicts
from mobile_playbook.platforms.ios.control_server import CommandControlServer
from mobile_playbook.platforms.ios.models import BehaviorResult, RiskRunResult
from mobile_playbook.platforms.ios.risks.feature5_keyboard_base import Feature5KeyboardRiskBase


class Feature04Risk01(Feature5KeyboardRiskBase):
    risk_id = "ios-feature-04-risk-01"
    feature_id = "feature-04"
    name = "Custom keyboard keystroke collection"
    requires_ipa_artifact = False

    def __init__(self, server_factory=CommandControlServer):
        self.server_factory = server_factory

    def run(self, app_config, global_config, device_client, report_writer):
        result = self._base_result(report_writer.run_timestamp, app_config)
        report_dir = report_writer.test_report_dir(app_config.id, self.risk_id, "collection_server")
        risk_config = merge_dicts(global_config.keystroke_collection, app_config.risks.get(self.risk_id) or {})
        collection = risk_config.get("collection") or risk_config.get("control") or {}
        keyboard_config = risk_config.get("keyboard_app") or {}
        installed_target_by_risk = False
        installed_keyboard_by_risk = False
        server = None
        try:
            print(f"ios-feature-04-risk-01[{app_config.id}]: installing/verifying keyboard app")
            keyboard_setup = self._install_or_verify_keyboard_app(keyboard_config, global_config, device_client)
            result.launch_result = {"keyboard_app": keyboard_setup}
            if keyboard_setup.get("status") not in {"INSTALLED", "INSTALLED_APP_VERIFIED", "SKIPPED"}:
                result.final_status = "INSTALL_FAILED" if keyboard_setup.get("status") == "INSTALL_FAILED" else "ARTIFACT_REQUIRED"
                result.errors.extend(keyboard_setup.get("errors") or [])
                return result
            installed_keyboard_by_risk = bool(keyboard_setup.get("installed_by_risk"))

            server = self._start_server(collection)
            device_reachable_base_url = self._device_reachable_base_url(server.base_url, collection)
            result.launch_result["collection_server"] = {
                "base_url": server.base_url,
                "device_reachable_base_url": device_reachable_base_url,
                "phone_base_url": device_reachable_base_url,
                "bind_host": collection.get("bind_host", "0.0.0.0"),
                "port": server.port,
                "token": server.state.token,
                "events_endpoint": f"{server.base_url}/events",
                "event_note": "Keyboard collection evidence is expected through POST /events with the pairing token.",
            }

            keyboard_bundle_id = keyboard_config.get("bundle_id")
            if keyboard_bundle_id and bool(keyboard_config.get("launch", True)):
                try:
                    result.launch_result["keyboard_app_launch"] = device_client.launch_app(keyboard_bundle_id)
                    result.launch_result["keyboard_app_permission_alerts"] = self._handle_permission_alerts(device_client, global_config)
                except Exception as exc:
                    result.final_status = "LAUNCH_FAILED"
                    result.errors.append(f"Could not launch keyboard host app {keyboard_bundle_id}: {exc}")
                    return result

            try:
                setup_result = self._configure_keyboard_server_url(device_client, keyboard_config, device_reachable_base_url)
                if setup_result:
                    result.launch_result["keyboard_server_setup"] = setup_result
            except Exception as exc:
                result.final_status = "FAILED"
                result.errors.append(f"Could not configure keyboard server URL: {exc}")
                return result

            setup_wait = float(collection.get("keyboard_setup_wait_seconds", 0))
            if setup_wait > 0:
                print(
                    "Add the custom keyboard in iOS Settings and enable Full Access now. "
                    f"Waiting {setup_wait:g} seconds before continuing."
                )
                time.sleep(setup_wait)

            pair_timeout = float(collection.get("pair_timeout_seconds", 60))
            print(f"ios-feature-04-risk-01[{app_config.id}]: waiting for /pair for up to {pair_timeout:g}s")
            if not server.wait_for_pair(pair_timeout):
                result.final_status = "PAIRING_TIMEOUT"
                result.errors.append(f"The keyboard app did not call /pair within {pair_timeout:g} seconds")
                return result

            acquisition = self._prepare_app(app_config, global_config, device_client, report_writer.run_timestamp)
            result.artifact_result = acquisition
            if acquisition.status not in {"ACQUIRED", "INSTALLED_APP_VERIFIED"}:
                result.final_status = self._artifact_status_to_final(acquisition.status)
                result.errors.extend(acquisition.errors)
                return result

            if acquisition.ipa_path is not None:
                install = device_client.install_app(acquisition.ipa_path, global_config.runner.app_install_timeout_ms)
                result.install_result = install
                installed_target_by_risk = install.status == "INSTALLED"
                if install.status != "INSTALLED":
                    result.final_status = "INSTALL_FAILED"
                    result.errors.extend(install.errors)
                    return result

            bundle_id = app_config.bundle_id
            try:
                result.launch_result["app_launch"] = device_client.launch_app(bundle_id)
                app_alerts = self._handle_permission_alerts(device_client, global_config)
                time.sleep(float(global_config.runner.launch_wait_seconds))
                app_alerts.extend(self._handle_permission_alerts(device_client, global_config))
                result.launch_result["app_permission_alerts"] = app_alerts
            except Exception as exc:
                result.final_status = "LAUNCH_FAILED"
                result.errors.append(str(exc))
                return result

            try:
                focus_result = self._focus_text_field_with_navigation(device_client, report_dir, collection, global_config)
                result.launch_result["text_field_focus"] = focus_result["focus"]
                result.launch_result["target_app_navigation"] = focus_result["navigation"]
                field_block = self._focused_field_custom_keyboard_blocker(focus_result["focus"])
                if field_block:
                    result.final_status = "CUSTOM_KEYBOARD_NOT_AVAILABLE"
                    result.verdict = "Reduced Risk"
                    result.errors.append(field_block)
                    self._capture_target_debug(device_client, report_dir, suffix="-custom-keyboard-unavailable")
                    return result
                keyboard_selection = self._select_custom_keyboard(device_client, collection, keyboard_config)
                result.launch_result["keyboard_selection"] = keyboard_selection
                if not self._keyboard_selection_allows_test(keyboard_selection):
                    result.launch_result["keyboard_selection_warning"] = self._keyboard_selection_error(keyboard_selection)
            except Exception as exc:
                result.final_status = "BEHAVIOR_FAILED"
                result.errors.append(f"Could not focus a text field in {app_config.name}: {exc}")
                self._capture_target_debug(device_client, report_dir)
                return result

            probe_text = self._probe_text(collection)
            type_result = self._type_probe_text(device_client, probe_text, collection)
            result.launch_result["probe_input"] = type_result

            evidence_source = str(collection.get("evidence_source") or "local_app_ui")
            evidence_timeout = float(collection.get("evidence_timeout_seconds") or collection.get("event_timeout_seconds", 30))
            if evidence_source == "server_events":
                behavior = self._verify_collection_event(server, report_dir, collection, probe_text, evidence_timeout)
            elif evidence_source == "local_app_ui":
                behavior = self._verify_local_collection_log(
                    device_client,
                    report_dir,
                    collection,
                    keyboard_config,
                    global_config,
                    probe_text,
                    evidence_timeout,
                )
            else:
                behavior = BehaviorResult(
                    status="FAIL",
                    errors=[f"Unsupported ios-feature-04-risk-01 collection.evidence_source: {evidence_source}"],
                    metadata={"evidence_source": evidence_source},
                )
            result.behavior_result = behavior
            if behavior.status != "PASS":
                result.final_status = "KEYSTROKE_COLLECTION_NOT_OBSERVED"
                result.verdict = "Reduced Risk"
                result.errors.extend(behavior.errors)
                self._capture_target_debug(device_client, report_dir, suffix="-collection-not-observed")
                return result

            result.final_status = "RISK_EXISTS"
            result.verdict = "At Risk"
            return result
        except Exception as exc:
            result.final_status = "FAILED"
            result.errors.append(str(exc))
            return result
        finally:
            if server is not None:
                snapshot = server.snapshot()
                result.launch_result = result.launch_result or {}
                result.launch_result["collection_server_snapshot"] = snapshot
                server.stop()
            result.cleanup_result = self._cleanup(
                app_config,
                global_config,
                device_client,
                installed_target_by_risk,
                keyboard_config,
                installed_keyboard_by_risk,
            )
            if result.cleanup_result.status == "CLEANUP_FAILED" and result.final_status == "RISK_EXISTS":
                result.final_status = "CLEANUP_FAILED"
                result.errors.extend(result.cleanup_result.errors)
            result.timestamp_end = datetime.now(timezone.utc).isoformat()
            report_writer.write_result(result, report_dir)

    def _base_result(self, run_timestamp: str, app_config) -> RiskRunResult:
        return RiskRunResult(
            run_timestamp=run_timestamp,
            timestamp_start=datetime.now(timezone.utc).isoformat(),
            timestamp_end=None,
            app_id=app_config.id,
            app_name=app_config.name,
            original_bundle_id=app_config.bundle_id,
            test_bundle_id=app_config.test_bundle_id,
            risk_id=self.risk_id,
            feature_id=self.feature_id,
            test_case_id="collection_server",
            test_case_type="keystroke_collection",
            artifact_source=app_config.artifact.get("source", ""),
        )

    def _probe_text(self, collection: dict) -> str:
        return str(collection.get("probe_text") or collection.get("expected_collected_text") or "hello123")

    def _type_probe_text(self, device_client, probe_text: str, collection: dict) -> dict:
        typer = getattr(device_client, "type_text", None)
        if not typer:
            raise RuntimeError("device client does not support typing probe text")
        input_config = collection.get("input") or {}
        return typer(probe_text, input_config)

    def _verify_collection_event(
        self,
        server,
        report_dir: Path,
        collection: dict,
        probe_text: str,
        timeout_seconds: float,
    ) -> BehaviorResult:
        expected = str(collection.get("expected_collected_text") or probe_text)
        deadline = time.monotonic() + timeout_seconds
        snapshot = server.snapshot()
        while time.monotonic() < deadline:
            snapshot = server.snapshot()
            matched = self._find_event_containing(snapshot.get("events") or [], expected)
            if matched is not None:
                evidence_path = report_dir / "collection_events.json"
                evidence_path.write_text(json.dumps(snapshot.get("events") or [], indent=2, sort_keys=True))
                return BehaviorResult(
                    status="PASS",
                    page_source_path=evidence_path,
                    metadata={"evidence_source": "server_events", "expected_collected_text": expected, "matched_event": matched},
                )
            time.sleep(0.5)
        evidence_path = report_dir / "collection_events.json"
        evidence_path.write_text(json.dumps(snapshot.get("events") or [], indent=2, sort_keys=True))
        return BehaviorResult(
            status="FAIL",
            page_source_path=evidence_path,
            errors=[f"Expected collected text was not observed from the keyboard event stream: {expected!r}"],
            metadata={
                "evidence_source": "server_events",
                "expected_collected_text": expected,
                "events_count": len(snapshot.get("events") or []),
                "requests_count": len(snapshot.get("requests") or []),
            },
        )

    def _verify_local_collection_log(
        self,
        device_client,
        report_dir: Path,
        collection: dict,
        keyboard_config: dict,
        global_config,
        probe_text: str,
        timeout_seconds: float,
    ) -> BehaviorResult:
        expected = str(collection.get("expected_collected_text") or probe_text)
        local_log = collection.get("local_log") or {}
        bundle_id = local_log.get("bundle_id") or keyboard_config.get("bundle_id")
        if not bundle_id:
            return BehaviorResult(
                status="FAIL",
                errors=["ios-feature-04-risk-01 local_app_ui evidence requires keyboard_app.bundle_id or collection.local_log.bundle_id"],
                metadata={"evidence_source": "local_app_ui", "expected_collected_text": expected},
            )

        launch_result = device_client.launch_app(bundle_id)
        self._handle_permission_alerts(device_client, global_config)
        wait_after_launch = float(local_log.get("wait_after_launch_seconds", 1))
        if wait_after_launch > 0:
            time.sleep(wait_after_launch)
        refresh_id = local_log.get("refresh_button_accessibility_id")
        if refresh_id:
            try:
                device_client.tap_by_accessibility_id(refresh_id)
            except Exception:
                pass

        page_source_path = report_dir / "keyboard_local_log_page_source.xml"
        screenshot_path = report_dir / "keyboard_local_log.png"
        last_source = ""
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            last_source = device_client.page_source()
            match = self._local_log_match(last_source, expected, collection)
            if match["matched"]:
                page_source_path.write_text(last_source)
                try:
                    device_client.screenshot(screenshot_path)
                except Exception:
                    pass
                return BehaviorResult(
                    status="PASS",
                    screenshot_path=screenshot_path if screenshot_path.exists() else None,
                    page_source_path=page_source_path,
                    metadata={
                        "evidence_source": "local_app_ui",
                        "expected_collected_text": expected,
                        "match": match,
                        "keyboard_app_launch": launch_result,
                    },
                )
            time.sleep(0.5)
        page_source_path.write_text(last_source)
        try:
            device_client.screenshot(screenshot_path)
        except Exception:
            pass
        return BehaviorResult(
            status="FAIL",
            screenshot_path=screenshot_path if screenshot_path.exists() else None,
            page_source_path=page_source_path,
            errors=[f"Expected collected text was not found in the keyboard host app local log UI: {expected!r}"],
            metadata={
                "evidence_source": "local_app_ui",
                "expected_collected_text": expected,
                "keyboard_app_launch": launch_result,
            },
        )

    def _find_event_containing(self, events: list[dict], expected: str) -> dict | None:
        for event in events:
            if expected and expected in json.dumps(event, sort_keys=True):
                return event
        return None

    def _local_log_match(self, source: str, expected: str, collection: dict) -> dict:
        local_log = collection.get("local_log") or {}
        if expected and expected in source:
            return {"matched": True, "mode": "substring", "expected": expected}
        expected_items = local_log.get("expected_items")
        if expected_items is None:
            expected_items = [char for char in expected if char]
        expected_items = [str(item) for item in expected_items if str(item)]
        if expected_items and self._contains_items_in_order(source, expected_items):
            return {"matched": True, "mode": "ordered_items", "expected_items": expected_items}
        return {"matched": False, "mode": "none", "expected": expected, "expected_items": expected_items}

    def _contains_items_in_order(self, source: str, items: list[str]) -> bool:
        cursor = 0
        lowered = source.lower()
        for item in items:
            found = lowered.find(item.lower(), cursor)
            if found < 0:
                return False
            cursor = found + len(item)
        return True
