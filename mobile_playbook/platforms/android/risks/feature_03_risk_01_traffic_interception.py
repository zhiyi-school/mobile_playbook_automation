"""
Android risk check for whether an app's network traffic can be intercepted through an HTTP proxy, routing the device
through Burp with its CA installed as a user CA and reading what the app sends, as playbook feature-03 risk 01 does.
"""

from __future__ import annotations

import json
import logging
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from mobile_playbook.common.logging_setup import redacted
from mobile_playbook.common.network import resolve_lan_host
from mobile_playbook.common.storage_paths import android_work_dir, burp_content_dir, ios_capture_path, resolve_under_repository
from mobile_playbook.platforms.android.device.fingerprint import installed_base_apk_sha256
from mobile_playbook.platforms.android.device.scripted_steps import StepsOutcome, canary_variants, merged_canaries, run_steps
from mobile_playbook.platforms.android.models import AndroidRiskRunResult
from mobile_playbook.platforms.android.private_data.findings import write_critical_findings
from mobile_playbook.platforms.android.private_data.scan import pii_value_findings
from mobile_playbook.platforms.android.risks.base import AndroidRisk
from mobile_playbook.platforms.android.traffic_interception.ca_certificate import (
    DEVICE_CA_PATH,
    CaOutcome,
    ensure_user_ca,
    fetch_burp_ca,
    load_ca,
    remove_pushed_ca,
)
from mobile_playbook.platforms.android.traffic_interception.device_proxy import (
    TrafficInterceptionSetupError,
    restore_device_proxy,
    set_device_proxy,
)
from mobile_playbook.platforms.android.traffic_interception.interception_check import BROWSER_PACKAGE, CHECK_URL, check_interception
from mobile_playbook.platforms.android.traffic_interception.proxy_warning import proxy_warning_keywords
from mobile_playbook.platforms.static_analysis.findings import highest_severity, severity_rank
from mobile_playbook.platforms.static_analysis.sensitive_findings import (
    public_sensitive_findings,
    scan_text_content,
    sensitive_finding,
)
from mobile_playbook.platforms.traffic_interception.burp_content import (
    ContentCaptureBusy,
    InterceptedExchange,
    arm_content_capture,
    disarm_content_capture,
    discard_content,
    read_content,
)

logger = logging.getLogger(__name__)

MARKDOWN_TITLE = "Android Traffic Interception Critical Findings"
DEFAULT_CA_DISPLAY_NAME = "PortSwigger CA"
MAX_FINDINGS = 200
MAX_EXPORTED_BODY_CHARS = 4000
# Headers that carry credentials or sessions; their values are findings whatever they look like.
SENSITIVE_HEADERS = {"authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key", "x-auth-token"}


class AndroidTrafficInterceptionRisk(AndroidRisk):
    risk_id = "android-feature-03-risk-01"
    feature_id = "feature-03"
    name = "Android HTTP Proxy Traffic Interception Test"
    test_case_id = "traffic_interception"
    test_case_type = "burp_proxy_capture"
    requires = ["adb", "appium", "burp"]

    # Route the device through Burp and trust its CA, confirm Burp decrypts the device's HTTPS, then open the app, take
    # it through its scripted steps while Burp records the app's exchanges in full, and judge what was intercepted.
    def run(self, app_config, global_config, device_client, report_writer):
        started_at = datetime.now().astimezone()
        package = app_config.package_name
        report_dir = report_writer.test_report_dir(app_config.id, self.risk_id, self.test_case_id, platform="android")
        shared = getattr(global_config, "traffic_interception", None) or {}
        cfg = {**shared, **(app_config.risks.get(self.risk_id) or {})}
        result = AndroidRiskRunResult(
            run_timestamp=report_writer.run_timestamp,
            timestamp_start=started_at.isoformat(),
            timestamp_end=None,
            app_id=app_config.id,
            app_name=app_config.name,
            package_name=package,
            risk_id=self.risk_id,
            test_case_id=self.test_case_id,
            test_case_type=self.test_case_type,
        )
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("%s[%s]: setup package=%s report_dir=%s config=%s", self.risk_id, app_config.id, package, report_dir, redacted(cfg))
        adb = device_client.adb
        proxy_url = str(cfg.get("proxy_url") or (getattr(global_config, "tools", None) or {}).get("burp_proxy") or "")
        listener_port = int(cfg.get("proxy_port") or urlsplit(proxy_url).port or 8081)
        steps = (app_config.risks.get(self.risk_id) or {}).get("steps") or []
        canaries = merged_canaries(shared.get("canaries"), cfg.get("canaries"))
        device_path = str(cfg.get("ca_device_path") or DEVICE_CA_PATH)
        proxy_state = None
        ca_outcome = CaOutcome()
        window = None
        driver = None
        try:
            result.metadata["installed_base_apk_sha256"] = installed_base_apk_sha256(adb, package)
            result.metadata["coverage"] = "scripted" if steps else "launch_only"
            # Step 03: the device's traffic goes to Burp's listener on this host.
            host = resolve_lan_host(str(cfg.get("proxy_host") or "auto"), label="traffic_interception.proxy_host")
            proxy_state = set_device_proxy(adb, host, listener_port)
            result.metadata["device_proxy"] = proxy_state.to_dict()
            # Step 04: Burp's CA in DER format, exported from Burp or served by its listener.
            ca = load_ca(resolve_under_repository(cfg["ca_cert_path"])) if cfg.get("ca_cert_path") else _fetch_ca(proxy_url)
            # Steps 05 and 06: copy the CA to the device and install it as a user CA through Settings.
            driver = device_client.make_driver()
            try:
                ensure_user_ca(driver, adb, ca, str(cfg.get("ca_display_name") or DEFAULT_CA_DISPLAY_NAME), report_dir, device_path=device_path, timeout=float(cfg.get("settings_timeout_seconds", 20)), outcome=ca_outcome)
            finally:
                result.evidence.extend(ca_outcome.evidence)
                result.metadata["ca"] = ca_outcome.to_dict()
            result.metadata["interception_check"] = check_interception(
                adb,
                resolve_under_repository(cfg["capture_path"]) if cfg.get("capture_path") else ios_capture_path(),
                url=str(cfg.get("check_url") or CHECK_URL),
                browser_package=str(cfg.get("browser_package") or BROWSER_PACKAGE),
                timeout=float(cfg.get("check_timeout_seconds", 20)),
            )
            try:
                window = arm_content_capture(
                    resolve_under_repository(cfg["content_dir"]) if cfg.get("content_dir") else burp_content_dir(),
                    [listener_port],
                    max_body_bytes=int(cfg.get("max_body_bytes", 65536)),
                )
            except ContentCaptureBusy as exc:
                raise TrafficInterceptionSetupError("CONTENT_CAPTURE_BUSY", str(exc)) from exc
            # Step 07: open the app and take it through a sensitive flow while Burp records its exchanges.
            outcome = self._exercise(driver, adb, package, steps, canaries, cfg, report_dir, result)
            disarm_content_capture(window)
            read = read_content(window)
            result.metadata["capture"] = {
                "exchange_count": len(read.exchanges),
                "malformed_count": read.malformed_count,
                "other_listener_count": read.other_listener_count,
                "listener_port": listener_port,
            }
            # Step 08: inspect the intercepted requests and responses for sensitive data.
            typed = {name: canaries[name] for name in (outcome.canaries_typed if outcome else [])}
            self._record(read.exchanges, typed, outcome, cfg, app_config, report_dir, result)
        except TrafficInterceptionSetupError as exc:
            logger.debug("%s[%s]: setup failed status=%s: %s", self.risk_id, app_config.id, exc.status, exc, exc_info=True)
            result.final_status, result.verdict = exc.status, "Inconclusive"
            result.errors.append(str(exc))
            result.metadata["setup_failure"] = {"status": exc.status, "state": exc.state}
        except Exception as exc:
            logger.debug("%s[%s]: run failed: %s", self.risk_id, app_config.id, exc, exc_info=True)
            result.final_status, result.verdict = "FAILED", "Inconclusive"
            result.errors.append(str(exc))
        finally:
            self._clean_up(adb, driver, window, proxy_state, ca_outcome, device_path, package, bool(cfg.get("keep_raw", False)), report_writer.run_timestamp, result)
            result.timestamp_end = datetime.now().astimezone().isoformat()
            logger.debug("%s[%s]: writing result final_status=%s verdict=%s errors=%s", self.risk_id, app_config.id, result.final_status, result.verdict, result.errors)
            report_writer.write_result(result, report_dir)
        return result

    # Open the app, record whether it warns of a proxy, then run its scripted steps or leave it running for the dwell
    # time, wait for its last requests and close it; returns the steps' outcome, or None when there are none.
    def _exercise(self, driver, adb, package: str, steps: list, canaries: dict[str, str], cfg: dict, report_dir: Path, result: AndroidRiskRunResult) -> StepsOutcome | None:
        adb.run(["shell", "am", "force-stop", package])
        driver.activate_app(package)
        time.sleep(float(cfg.get("launch_wait", 5)))
        try:
            foreground = driver.current_package
        except Exception as exc:
            logger.debug("%s[%s]: reading current_package failed: %s", self.risk_id, package, exc, exc_info=True)
            foreground = None
        result.metadata["foreground_package"] = foreground
        warning: list[str] = []
        if foreground == package:
            try:
                warning = proxy_warning_keywords(driver.page_source)
            except Exception as exc:
                logger.debug("%s[%s]: reading page_source failed: %s", self.risk_id, package, exc, exc_info=True)
                result.metadata["ui_error"] = f"Could not read the app UI: {exc}"
            _save_screenshot(driver, report_dir / "app_launched.png", "app_launched", "The app after it opened with the device routed through Burp", result)
        result.metadata["proxy_warning"] = warning
        outcome = None
        if steps and not warning:
            outcome = run_steps(driver, steps, canaries, float(cfg.get("step_timeout_seconds", 10)))
            result.metadata["steps"] = outcome.to_dict()
            if outcome.failure is not None:
                result.metadata["coverage"] = "scripted_partial"
        elif steps:
            result.metadata["steps"] = StepsOutcome(configured=len(steps)).to_dict()
        else:
            time.sleep(float(cfg.get("dwell_seconds", 15)))
        # Requests the last step started may still be on their way through Burp.
        time.sleep(float(cfg.get("traffic_wait_seconds", 5)))
        try:
            driver.terminate_app(package)
        except Exception as exc:
            logger.debug("%s[%s]: terminate_app failed: %s", self.risk_id, package, exc, exc_info=True)
        return outcome

    # Keep the exchanges that belong to the app, scan them, write them masked with their findings, and decide the
    # verdict: any intercepted exchange of the app's is At Risk, whether or not it carries sensitive data.
    def _record(self, exchanges: list[InterceptedExchange], typed: dict[str, str], outcome: StepsOutcome | None, cfg: dict, app_config, report_dir: Path, result: AndroidRiskRunResult) -> None:
        reveal_values = bool(cfg.get("reveal_values", False))
        expected_hosts = [str(host).strip().lower().rstrip(".") for host in (cfg.get("expected_hosts") or []) if str(host).strip()]
        variants = canary_variants(typed)
        scanned = [scan_exchange(exchange, index, reveal_values, variants) for index, exchange in enumerate(exchanges, start=1)]
        app = [item for item in scanned if _from_app(item, expected_hosts)]
        findings = _distinct([finding for item in app for finding in item["findings"]])
        result.metadata["expected_hosts"] = expected_hosts
        result.metadata["app_exchange_count"] = len(app)
        result.metadata["app_hosts"] = sorted({item["exchange"].host for item in app})
        result.metadata["other_hosts"] = sorted({item["exchange"].host for item in scanned if item not in app})[:50]
        result.metadata["cleartext_exchange_count"] = sum(1 for item in app if item["exchange"].scheme == "http")
        result.metadata["finding_count"] = len(findings)
        result.metadata["canaries_found"] = sorted({item["context"].split(" ")[0] for item in findings if item["match_type"] == "CANARY_VALUE"})

        traffic_path = report_dir / "intercepted_traffic.json"
        traffic_path.write_text(json.dumps({"exchanges": [export_exchange(item, reveal_values) for item in app]}, indent=2, sort_keys=True))
        result.evidence.append({"kind": "intercepted_traffic", "path": str(traffic_path), "label": "The app's requests and responses intercepted through Burp" + ("" if reveal_values else ", with sensitive values masked")})
        findings_path = report_dir / "intercepted_traffic_findings.json"
        findings_path.write_text(json.dumps({"findings": public_sensitive_findings(findings)}, indent=2, sort_keys=True))
        result.evidence.append({"kind": "intercepted_traffic_findings", "path": str(findings_path), "label": "Sensitive values found in the app's intercepted traffic"})

        flags = []
        if app:
            result.verdict = "At Risk"
            result.final_status = "SENSITIVE_DATA_INTERCEPTED" if findings else "TRAFFIC_INTERCEPTED"
            hosts = ", ".join(result.metadata["app_hosts"][:5])
            flags.append(
                {
                    "id": "TRAFFIC_INTERCEPTED",
                    "severity": "HIGH" if result.metadata["cleartext_exchange_count"] else "MEDIUM",
                    "title": "The app's traffic can be read through an HTTP proxy whose CA the user installed",
                    "evidence": [f"{len(app)} exchange(s) with {hosts} read through Burp"] + [f"{item['exchange'].method} {item['exchange'].url}" for item in app[:10]],
                    "recommendation": "Pin the server's certificate or public key, for example with OkHttp's CertificatePinner or a <pin-set> in the network security configuration, so a user-installed CA is not trusted for the app's servers, and send nothing over cleartext HTTP.",
                }
            )
        if findings:
            flags.append(
                {
                    "id": "SENSITIVE_DATA_IN_INTERCEPTED_TRAFFIC",
                    "severity": highest_severity(findings),
                    "title": "Sensitive data is readable in the app's intercepted traffic",
                    "evidence": [_evidence_line(item) for item in findings[:15]],
                    "recommendation": "Assume traffic can be read on a device whose user trusts another CA. Pin certificates, and avoid sending secrets the server does not need, such as passwords on every request.",
                }
            )
        if not app:
            result.verdict = "Inconclusive"
            if result.metadata["proxy_warning"]:
                result.final_status, result.verdict = "PROXY_DETECTED", "Reduced Risk"
            elif outcome is not None and outcome.failure is not None:
                result.final_status = "SCRIPTED_STEPS_INCOMPLETE"
                result.errors.append(f"Scripted step {outcome.failure['index'] + 1} of {outcome.configured} failed, so the flow it leads to was not tested: {outcome.failure['error']}")
            elif scanned:
                result.final_status = "APP_TRAFFIC_NOT_IDENTIFIED"
                result.errors.append(
                    "Burp read traffic during the test, but none of it was to an expected host or carried a canary the steps typed, so it could not be "
                    "told apart from other apps'; set expected_hosts to the app's servers. Hosts seen: " + ", ".join(result.metadata["other_hosts"][:10])
                )
            else:
                result.final_status = "TRAFFIC_INTERCEPTION_NOT_OBSERVED"
                result.errors.append(
                    "Burp read no traffic while the app ran. The app may not trust user-installed CAs (the default for apps targeting Android 7 and later), "
                    "may pin certificates, may not use the system proxy, or may not have made requests; this does not show that it pins certificates."
                )
        logger.debug("%s[%s]: %s exchanges, %s from the app, %s findings -> %s", self.risk_id, app_config.id, len(exchanges), len(app), len(findings), result.final_status)
        write_critical_findings(flags, app_config, report_dir, result, MARKDOWN_TITLE)

    # Stop recording, delete or keep the raw capture, close the app's session, put back the device proxy and remove the
    # CA file this run copied; a proxy that could not be reset is reported without changing the verdict.
    def _clean_up(self, adb, driver, window, proxy_state, ca_outcome: CaOutcome, device_path: str, package: str, keep_raw: bool, run_timestamp: str, result: AndroidRiskRunResult) -> None:
        if window is not None:
            disarm_content_capture(window)
            if keep_raw and window.content_path.exists():
                raw = android_work_dir() / "traffic_interception" / package.replace(".", "_") / f"{run_timestamp}_content.jsonl"
                raw.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(window.content_path, raw)
                result.metadata["raw_capture"] = str(raw)
            # The content file holds raw request bodies such as passwords, so it does not outlive the run.
            discard_content(window)
        if driver is not None:
            try:
                driver.quit()
            except Exception as exc:
                logger.debug("%s[%s]: driver quit failed: %s", self.risk_id, package, exc, exc_info=True)
        if proxy_state is not None:
            try:
                result.metadata["device_proxy"]["restore"] = restore_device_proxy(adb, proxy_state)
            except TrafficInterceptionSetupError as exc:
                result.metadata["device_proxy"]["restore"] = {"status": exc.status, "error": str(exc)}
                result.errors.append(f"The device's HTTP proxy could not be reset and still points at Burp; run `adb shell settings put global http_proxy :0`. {exc}")
                if result.verdict != "At Risk":
                    result.final_status, result.verdict = exc.status, "Inconclusive"
        try:
            remove_pushed_ca(adb, ca_outcome, device_path)
        except Exception as exc:
            logger.debug("%s[%s]: removing the pushed CA failed: %s", self.risk_id, package, exc, exc_info=True)


# Scan one exchange's URL, headers and bodies for the canaries typed, in any of their encodings, credential headers,
# secrets and personal data, returning the exchange with its findings and its readable request and response text.
def scan_exchange(exchange: InterceptedExchange, index: int, reveal_values: bool, variants: dict[str, str]) -> dict[str, Any]:
    label = f"exchange {index} ({exchange.method} {exchange.host})"
    request_text = _body_text(exchange.request_body)
    response_text = _body_text(exchange.response_body)
    sections = {
        "url": exchange.url,
        "request headers": "\n".join(exchange.request_headers[1:]),
        "request body": request_text,
        "response headers": "\n".join(exchange.response_headers[1:]),
        "response body": response_text,
    }
    findings = []
    for part, text in sections.items():
        where = f"{label} {part}"
        canary = [sensitive_finding(where, "CANARY_VALUE", value, "HIGH", context=context, reveal_values=True) for value, context in variants.items() if value in text]
        patterns = scan_text_content(text.encode("utf-8"), where, reveal_values) + pii_value_findings(where, text, reveal_values)
        if part.endswith("headers"):
            patterns += _header_findings(where, text, reveal_values)
        # A pattern match on a canary repeats its canary finding, and would mask a value that is not real.
        findings += canary + [finding for finding in patterns if finding["_raw_value"] not in variants]
    return {"exchange": exchange, "findings": findings, "request_text": request_text, "response_text": response_text, "variants": variants}


# Describe an exchange for the evidence file, with every sensitive value found masked unless reveal_values is set and
# bodies cut to a readable length. Canaries are synthetic, so they are left in full.
def export_exchange(item: dict[str, Any], reveal_values: bool) -> dict[str, Any]:
    exchange: InterceptedExchange = item["exchange"]
    raws = sorted({finding["_raw_value"] for finding in item["findings"] if finding["_raw_value"] not in item["variants"]}, key=len, reverse=True)
    masks = {finding["_raw_value"]: finding["masked_value"] for finding in item["findings"]}

    def mask(text: str) -> str:
        if reveal_values:
            return text
        # Longest first, so a value inside a longer match is not masked on its own and leaves the rest readable.
        for raw in raws:
            text = text.replace(raw, masks[raw])
        return text

    return {
        "method": exchange.method,
        "url": mask(exchange.url),
        "scheme": exchange.scheme,
        "host": exchange.host,
        "status_code": exchange.status_code,
        "listener": exchange.listener,
        "request_headers": [mask(header) for header in exchange.request_headers],
        "request_body": mask(item["request_text"][:MAX_EXPORTED_BODY_CHARS]),
        "request_body_length": exchange.request_body_length,
        "request_body_truncated": exchange.request_body_truncated,
        "response_headers": [mask(header) for header in exchange.response_headers],
        "response_body": mask(item["response_text"][:MAX_EXPORTED_BODY_CHARS]),
        "response_body_length": exchange.response_body_length,
        "response_body_truncated": exchange.response_body_truncated,
        "response_body_encoding": exchange.response_body_encoding,
        "finding_count": len(item["findings"]),
    }


# Return whether a scanned exchange is the app's: to one of the expected hosts or their subdomains, or carrying a
# canary the steps typed into the app.
def _from_app(item: dict[str, Any], expected_hosts: list[str]) -> bool:
    host = item["exchange"].host
    if any(host == expected or host.endswith(f".{expected}") for expected in expected_hosts):
        return True
    return any(finding["match_type"] == "CANARY_VALUE" for finding in item["findings"])


# Return a body as text, or a note of its size when it is binary.
def _body_text(body: bytes) -> str:
    if b"\x00" in body[:4096]:
        return f"<{len(body)} binary bytes>"
    return body.decode("utf-8", errors="replace")


# Report the value of each credential or session header in a block of "Name: value" lines.
def _header_findings(where: str, text: str, reveal_values: bool) -> list[dict[str, Any]]:
    findings = []
    for line in text.splitlines():
        name, _, value = line.partition(":")
        if name.strip().lower() in SENSITIVE_HEADERS and value.strip():
            findings.append(sensitive_finding(where, "SENSITIVE_HEADER", value.strip(), "HIGH", context=name.strip(), reveal_values=reveal_values))
    return findings


# Return one finding per distinct value, counting its occurrences, most severe first.
def _distinct(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    distinct: dict[tuple[str, str], dict[str, Any]] = {}
    for finding in findings:
        key = (finding["match_type"], finding["_raw_value"])
        if key in distinct:
            distinct[key]["occurrences"] += 1
        else:
            distinct[key] = {**finding, "occurrences": 1}
    return sorted(distinct.values(), key=lambda item: severity_rank(item["severity"]), reverse=True)[:MAX_FINDINGS]


# Describe one finding for the critical findings report.
def _evidence_line(item: dict[str, Any]) -> str:
    kind = f"{item['match_type']} ({item['context']})" if item["context"] else item["match_type"]
    return f"{item['severity']} {kind} in {item['path']}: {item['reported_value']} ({item['occurrences']} occurrence(s))"


# Download Burp's CA through its listener, reporting a missing or unreachable listener as CA_UNAVAILABLE.
def _fetch_ca(proxy_url: str) -> bytes:
    if not proxy_url:
        raise TrafficInterceptionSetupError("CA_UNAVAILABLE", "tools.burp_proxy is not set, so Burp's CA cannot be downloaded; set it or traffic_interception.ca_cert_path")
    try:
        return fetch_burp_ca(proxy_url)
    except TrafficInterceptionSetupError:
        raise
    except Exception as exc:
        raise TrafficInterceptionSetupError("CA_UNAVAILABLE", f"downloading Burp's CA through {proxy_url} failed: {exc}") from exc


# Add a screenshot to the evidence when Appium saves it.
def _save_screenshot(driver, path: Path, kind: str, label: str, result: AndroidRiskRunResult) -> None:
    try:
        if driver.get_screenshot_as_file(str(path)):
            result.evidence.append({"kind": kind, "path": str(path), "label": label})
    except Exception as exc:
        logger.debug("android traffic interception: screenshot %s failed: %s", path.name, exc, exc_info=True)
