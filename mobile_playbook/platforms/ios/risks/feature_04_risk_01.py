from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from mobile_playbook.core.config_files import merge_dicts
from mobile_playbook.platforms.ios import keyboard_setup
from mobile_playbook.platforms.ios.control_server import CommandControlServer
from mobile_playbook.platforms.ios.models import BehaviorResult, RiskRunResult
from mobile_playbook.platforms.ios.risks.feature_04_keyboard_base import Feature04KeyboardRiskBase

logger = logging.getLogger(__name__)


class Feature04Risk01(Feature04KeyboardRiskBase):
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
        result.launch_result = {}
        keyboard_bundle_id = keyboard_config.get("bundle_id")
        preexisting = bool(keyboard_bundle_id and device_client.is_installed(keyboard_bundle_id))
        result.launch_result["starting_state"] = {
            "keyboard_preinstalled": preexisting,
            "require_clean_state": bool(keyboard_config.get("require_clean_state", True)),
        }
        if preexisting and bool(keyboard_config.get("require_clean_state", True)):
            outcome = device_client.remove_app_verified(keyboard_bundle_id)
            result.launch_result["starting_state"]["reset"] = outcome
            if not outcome["verified"]:
                result.final_status = "DIRTY_STARTING_STATE"
                result.verdict = "Inconclusive"
                result.errors.append(
                    f"{keyboard_bundle_id} was left installed by an earlier run and could not be removed"
                )
                return result
        installed_keyboard_by_risk = False
        server = None
        try:
            logger.info("ios-feature-04-risk-01[%s]: installing/verifying keyboard app", app_config.id)
            keyboard_app_setup = self._install_or_verify_keyboard_app(keyboard_config, global_config, device_client)
            result.launch_result["keyboard_app"] = keyboard_app_setup
            if keyboard_app_setup.get("status") not in {"INSTALLED", "INSTALLED_APP_VERIFIED", "SKIPPED"}:
                result.final_status = "INSTALL_FAILED" if keyboard_app_setup.get("status") == "INSTALL_FAILED" else "ARTIFACT_REQUIRED"
                result.errors.extend(keyboard_app_setup.get("errors") or [])
                return result
            installed_keyboard_by_risk = bool(keyboard_app_setup.get("installed_by_risk"))

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

            result.launch_result["keyboard_network_alerts"] = self._handle_permission_alerts(
                device_client,
                global_config,
                {"wait_seconds": float(collection.get("network_alert_wait_seconds", 10))},
            )

            try:
                setup_config = dict(collection.get("keyboard_setup") or {})
                setup_config.setdefault(
                    "keyboard_extension_bundle_id", keyboard_config.get("keyboard_extension_bundle_id")
                )
                result.launch_result["keyboard_setup"] = keyboard_setup.prepare_custom_keyboard(
                    device_client, setup_config, report_dir
                )
            except keyboard_setup.KeyboardSetupError as exc:
                result.launch_result["keyboard_setup"] = exc.state
                result.final_status = "CUSTOM_KEYBOARD_NOT_AVAILABLE"
                result.verdict = "Inconclusive"
                result.errors.append(str(exc))
                return result

            setup_wait = float(collection.get("keyboard_setup_wait_seconds", 0))
            if setup_wait > 0:
                time.sleep(setup_wait)

            activation = self._activate_keyboard_in_host_app(
                device_client, global_config, collection, keyboard_config
            )
            result.launch_result["keyboard_activation"] = activation
            if activation["status"] not in {"ACTIVATED", "SKIPPED"}:
                result.final_status = "CUSTOM_KEYBOARD_NOT_AVAILABLE"
                result.verdict = "Inconclusive"
                result.errors.append(activation.get("error", "Could not activate the custom keyboard"))
                self._capture_target_debug(device_client, report_dir, suffix="-keyboard-activation")
                return result

            evidence_source = str(collection.get("evidence_source") or "local_app_ui")
            if evidence_source == "server_events":
                pair_timeout = float(collection.get("pair_timeout_seconds", 60))
                logger.info("ios-feature-04-risk-01[%s]: waiting for /pair for up to %gs", app_config.id, pair_timeout)
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
            probe_evidence = self._capture_probe_evidence(device_client, report_dir, probe_text)
            result.launch_result["probe_evidence"] = probe_evidence

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
            if probe_evidence.get("screenshot"):
                behavior.metadata["keyboard_log_screenshot"] = (
                    str(behavior.screenshot_path) if behavior.screenshot_path else None
                )
                behavior.screenshot_path = Path(probe_evidence["screenshot"])
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
                result.launch_result = result.launch_result or {}
                try:
                    result.launch_result["collection_server_snapshot"] = server.snapshot()
                except Exception as exc:
                    result.errors.append(f"Could not snapshot the collection server: {exc}")
                try:
                    server.stop()
                except Exception as exc:
                    result.errors.append(f"Could not stop the collection server: {exc}")
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

    def _activate_keyboard_in_host_app(self, device_client, global_config, collection: dict, keyboard_config: dict) -> dict:
        """Focus the keyboard host app's own field so iOS loads the extension before /pair."""
        bundle_id = keyboard_config.get("bundle_id")
        field_id = (keyboard_config.get("server_setup") or {}).get("server_url_input_accessibility_id")
        if not bundle_id or not field_id:
            return {
                "status": "SKIPPED",
                "reason": "keyboard_app.bundle_id and server_setup.server_url_input_accessibility_id are required to activate the keyboard",
            }
        state: dict = {"bundle_id": bundle_id, "accessibility_id": field_id}
        try:
            state["launch"] = device_client.launch_app(bundle_id)
            state["alerts"] = self._handle_permission_alerts(device_client, global_config)
            state["focus"] = device_client.tap_text_field({"accessibility_id": field_id})
        except Exception as exc:
            state["status"] = "FOCUS_FAILED"
            state["error"] = f"Could not focus the keyboard host app field {field_id}: {exc}"
            return state
        settle = float(collection.get("keyboard_activation_settle_seconds", 1))
        if settle > 0:
            time.sleep(settle)
        selection = self._select_custom_keyboard(device_client, collection, keyboard_config)
        state["selection"] = selection
        if not self._keyboard_selection_allows_test(selection):
            state["status"] = "SELECTION_FAILED"
            state["error"] = self._keyboard_selection_error(selection)
            return state
        state["status"] = "ACTIVATED"
        return state

    def _probe_text(self, collection: dict) -> str:
        return str(collection.get("probe_text") or collection.get("expected_collected_text") or "hello123")

    def _type_probe_text(self, device_client, probe_text: str, collection: dict) -> dict:
        typer = getattr(device_client, "type_text", None)
        if not typer:
            raise RuntimeError("device client does not support typing probe text")
        input_config = collection.get("input") or {}
        return typer(probe_text, input_config)

    def _capture_probe_evidence(self, device_client, report_dir: Path, probe_text: str) -> dict:
        """The app under test with the probe text in it, captured before evidence checks switch apps."""
        screenshot_path = report_dir / "target_screen.png"
        page_source_path = report_dir / "target_page_source.xml"
        evidence: dict = {"probe_text": probe_text}
        try:
            source = device_client.page_source()
            page_source_path.write_text(source)
            evidence["page_source"] = str(page_source_path)
            evidence["probe_text_visible"] = probe_text.lower() in source.lower()
        except Exception as exc:
            evidence["page_source_error"] = str(exc)
        try:
            device_client.screenshot(screenshot_path)
            evidence["screenshot"] = str(screenshot_path)
        except Exception as exc:
            evidence["screenshot_error"] = str(exc)
        return evidence

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
