"""
Android risk check for whether an app writes sensitive data to its runtime log, capturing the log over USB debugging
with `adb logcat` while the app runs, as the playbook does with Android Studio's Logcat window.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import time
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Any

from mobile_playbook.common.logging_setup import redacted
from mobile_playbook.common.storage_paths import android_work_dir
from mobile_playbook.platforms.android.device.debugging_warning import debugging_keywords
from mobile_playbook.platforms.android.device.fingerprint import installed_base_apk_sha256
from mobile_playbook.platforms.android.device.logcat import LINE_RE, LogcatCapture, LogcatError, app_pids, app_uid, clear_logcat, read_app_lines, start_logcat
from mobile_playbook.platforms.android.device.scripted_steps import StepsOutcome, merged_canaries, run_steps
from mobile_playbook.platforms.android.models import AndroidRiskRunResult
from mobile_playbook.platforms.android.private_data.findings import write_critical_findings
from mobile_playbook.platforms.android.private_data.scan import pii_value_findings
from mobile_playbook.platforms.android.risks.base import AndroidRisk
from mobile_playbook.platforms.static_analysis.findings import highest_severity, severity_rank
from mobile_playbook.platforms.static_analysis.sensitive_findings import (
    public_sensitive_findings,
    scan_text_content,
    sensitive_finding,
    sensitive_key_severity,
    value_looks_secret,
)

logger = logging.getLogger(__name__)

MARKDOWN_TITLE = "Android Runtime Log Critical Findings"
PID_POLL_SECONDS = 2.0
# Seconds logcat is given to write the app's last lines after the app is stopped.
FLUSH_SECONDS = 1.0
MAX_FINDINGS = 200
# Log messages usually print "password=value" or "token: value" without the quotes the shared scanner expects; the
# key names follow the playbook control's redaction patterns and the shared scanner's secret keys.
LOG_KEY_VALUE_RE = re.compile(
    r"(?i)(?<![a-z])(password|passwd|pwd|pass|passcode|pin|otp|secret|client[_-]?secret|api[_-]?key|access[_-]?token|refresh[_-]?token|auth[_-]?token|token|session[_-]?id|cookie|authorization)(?![a-z0-9])"
    r"\s*[=:]\s*(?:(?:bearer|basic)\s+)?(?![\"'])([^\s,;\"'&)}\]>]{4,})"
)
# Java's default toString(), a class name and hash code such as the framework's "token = android.os.BinderProxy@e00118d"
# in every app process, names an object rather than holding a secret.
OBJECT_REFERENCE_RE = re.compile(r"[A-Za-z_$][\w.$]*@[0-9a-fA-F]{1,8}")
# The threadtime header and the tag up to the first ": " ("... D Tag     : "), so neither the header's digits nor a tag
# such as "Token" are scanned; a tag may itself contain a colon, as "unknown:ViewManagerPropertyUpdater" does.
LINE_PREFIX_RE = re.compile(LINE_RE.pattern + r".*?: ")


class AndroidRuntimeLogsRisk(AndroidRisk):
    risk_id = "android-feature-02-risk-02"
    feature_id = "feature-02"
    name = "Android Runtime Log Capture Test"
    test_case_id = "runtime_log_capture"
    test_case_type = "adb_logcat_capture"
    requires = ["adb", "appium"]

    # Capture the device log while the app is opened and either taken through its scripted steps or left running,
    # keep the app's own lines, scan them for sensitive data and the canaries typed, and write the result.
    def run(self, app_config, global_config, device_client, report_writer):
        started_at = datetime.now().astimezone()
        package = app_config.package_name
        report_dir = report_writer.test_report_dir(app_config.id, self.risk_id, self.test_case_id, platform="android")
        cfg = {**(getattr(global_config, "runtime_logs", None) or {}), **(app_config.risks.get(self.risk_id) or {})}
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
        reveal_values = bool(cfg.get("reveal_values", False))
        keep_raw = bool(cfg.get("keep_raw", False))
        steps = (app_config.risks.get(self.risk_id) or {}).get("steps") or []
        canaries = merged_canaries((getattr(global_config, "runtime_logs", None) or {}).get("canaries"), cfg.get("canaries"))
        outcome: StepsOutcome | None = None
        raw_path = android_work_dir() / "runtime_logs" / package.replace(".", "_") / f"{report_writer.run_timestamp}_logcat.txt"
        capture: LogcatCapture | None = None
        driver = None
        try:
            result.metadata["installed_base_apk_sha256"] = installed_base_apk_sha256(adb, package)
            result.metadata["adb_enabled"] = _adb_enabled(adb)
            uid = app_uid(adb, package)
            if uid is None:
                raise LogcatError(f"dumpsys package reports no user ID for {package}; is it installed on the device?")
            result.metadata["app_uid"] = uid
            result.metadata["coverage"] = "scripted" if steps else "launch_only"
            # Stopped first, so the capture starts before the app does and holds what it logs on start-up.
            adb.run(["shell", "am", "force-stop", package])
            cleared = clear_logcat(adb)
            result.metadata["log_buffer_cleared"] = cleared is None
            if cleared is not None:
                result.metadata["log_buffer_clear_error"] = cleared
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            capture = start_logcat(adb, raw_path)
            result.metadata["uid_in_log"] = capture.with_uid
            driver = device_client.make_driver()
            pids = self._open_app(driver, adb, package, cfg, result)
            if result.metadata["usb_debugging_warning"]:
                if steps:
                    result.metadata["steps"] = StepsOutcome(configured=len(steps)).to_dict()
            elif steps:
                outcome = run_steps(driver, steps, canaries, float(cfg.get("step_timeout_seconds", 10)))
                result.metadata["steps"] = outcome.to_dict()
                if outcome.failure is not None:
                    result.metadata["coverage"] = "scripted_partial"
            else:
                pids |= self._dwell(adb, package, capture, float(cfg.get("dwell_seconds", 15)))
            _close_app(driver, package)
            pids |= app_pids(adb, package)
            time.sleep(FLUSH_SECONDS)
            if not capture.stop():
                raise LogcatError("logcat stopped before the app was closed; the device may have disconnected")
            result.metadata["app_pids"] = sorted(pids)
            lines = read_app_lines(raw_path, uid, pids)
            typed = {name: canaries[name] for name in (outcome.canaries_typed if outcome else [])}
            self._record(lines, bool(pids), reveal_values, typed, outcome, app_config, report_dir, result)
        except Exception as exc:
            logger.debug("%s[%s]: run failed: %s", self.risk_id, app_config.id, exc, exc_info=True)
            result.final_status = "FAILED"
            result.verdict = "Inconclusive"
            result.errors.append(str(exc))
        finally:
            if capture is not None and capture.running():
                capture.stop()
            if driver is not None:
                try:
                    driver.quit()
                except Exception as quit_exc:
                    logger.debug("%s[%s]: driver quit failed: %s", self.risk_id, app_config.id, quit_exc, exc_info=True)
            if raw_path.exists():
                if keep_raw:
                    result.metadata["raw_log"] = str(raw_path)
                else:
                    raw_path.unlink()
            result.timestamp_end = datetime.now().astimezone().isoformat()
            logger.debug("%s[%s]: writing result final_status=%s verdict=%s errors=%s", self.risk_id, app_config.id, result.final_status, result.verdict, result.errors)
            report_writer.write_result(result, report_dir)
        return result

    # Launch the app, wait for it to settle, record whether it warns that USB debugging is on, and return the PIDs
    # its processes have.
    def _open_app(self, driver, adb, package: str, cfg: dict, result: AndroidRiskRunResult) -> set[int]:
        driver.activate_app(package)
        time.sleep(float(cfg.get("launch_wait", 5)))
        pids = app_pids(adb, package)
        try:
            foreground = driver.current_package
        except Exception as exc:
            logger.debug("%s[%s]: reading current_package failed: %s", self.risk_id, package, exc, exc_info=True)
            foreground = None
        result.metadata["foreground_package"] = foreground
        warning: list[str] = []
        if foreground == package:
            try:
                warning = debugging_keywords(driver.page_source)
            except Exception as exc:
                logger.debug("%s[%s]: reading page_source failed: %s", self.risk_id, package, exc, exc_info=True)
                result.metadata["ui_error"] = f"Could not read the app UI: {exc}"
        result.metadata["usb_debugging_warning"] = warning
        logger.debug("%s[%s]: opened, foreground=%s pids=%s warning=%s", self.risk_id, package, foreground, sorted(pids), warning)
        return pids

    # Leave the app running for the dwell time, collecting its PIDs along the way when the log lines carry no UID.
    def _dwell(self, adb, package: str, capture: LogcatCapture, dwell_seconds: float) -> set[int]:
        pids: set[int] = set()
        deadline = time.monotonic() + dwell_seconds
        while (remaining := deadline - time.monotonic()) > 0:
            time.sleep(min(PID_POLL_SECONDS, remaining))
            if not capture.with_uid:
                pids |= app_pids(adb, package)
        return pids

    # Scan the app's log lines, write them masked with their findings, and decide the verdict; a canary found decides
    # it as any sensitive value does, and an unfinished script leaves an otherwise clean log inconclusive.
    def _record(self, lines: list[str], app_ran: bool, reveal_values: bool, typed: dict[str, str], outcome: StepsOutcome | None, app_config, report_dir: Path, result: AndroidRiskRunResult) -> None:
        findings, exported = scan_log_lines(lines, f"logcat_{app_config.package_name}.txt", reveal_values, typed)
        result.metadata["app_line_count"] = len(lines)
        result.metadata["finding_count"] = len(findings)
        result.metadata["canaries_found"] = sorted({item["context"].split(" ")[0] for item in findings if item["match_type"] == "CANARY_VALUE"})
        log_path = report_dir / f"logcat_{app_config.package_name}.txt"
        log_path.write_text("".join(f"{line}\n" for line in exported))
        result.evidence.append({"kind": "runtime_log", "path": str(log_path), "label": "The app's log lines captured over USB debugging" + ("" if reveal_values else ", with sensitive values masked")})
        findings_path = report_dir / "runtime_log_findings.json"
        findings_path.write_text(json.dumps({"findings": public_sensitive_findings(findings)}, indent=2, sort_keys=True))
        result.evidence.append({"kind": "runtime_log_findings", "path": str(findings_path), "label": "Sensitive values found in the app's log lines"})

        flags = []
        if findings:
            result.final_status, result.verdict = "SENSITIVE_DATA_LOGGED", "At Risk"
            flags.append(
                {
                    "id": "SENSITIVE_DATA_IN_RUNTIME_LOGS",
                    "severity": highest_severity(findings),
                    "title": "The app writes sensitive data to its runtime log, which can be read over USB debugging",
                    "evidence": [_evidence_line(item) for item in findings[:15]],
                    "recommendation": "Do not log credentials, tokens or personal data. Redact sensitive patterns before they reach Logcat, and strip debug logging from release builds, for example with R8 -assumenosideeffects rules for android.util.Log.",
                }
            )
        elif result.metadata["usb_debugging_warning"]:
            result.final_status, result.verdict = "USB_DEBUGGING_BLOCKED", "Reduced Risk"
        elif outcome is not None and outcome.failure is not None:
            result.final_status, result.verdict = "SCRIPTED_STEPS_INCOMPLETE", "Inconclusive"
            result.errors.append(f"Scripted step {outcome.failure['index'] + 1} of {outcome.configured} failed, so the flow it leads to was not tested: {outcome.failure['error']}")
        elif lines:
            result.final_status, result.verdict = "NO_SENSITIVE_DATA_LOGGED", "Reduced Risk"
        elif app_ran:
            result.final_status, result.verdict = "NO_APP_LOGS", "Reduced Risk"
        else:
            result.final_status, result.verdict = "FAILED", "Inconclusive"
            result.errors.append("The app did not start, so no log lines were captured from it")
        logger.debug("%s[%s]: %s app lines, %s findings -> %s", self.risk_id, app_config.id, len(lines), len(findings), result.final_status)
        write_critical_findings(flags, app_config, report_dir, result, MARKDOWN_TITLE)


# Scan each log line for the canaries typed, in any of their encodings, and for secrets and personal data, returning
# one finding per distinct value with its first line and number of occurrences, and the lines with every value found
# masked unless reveal_values is set. Canaries are synthetic, so they are reported and left in the lines in full.
def scan_log_lines(lines: list[str], log_name: str, reveal_values: bool, canaries: dict[str, str] | None = None) -> tuple[list[dict[str, Any]], list[str]]:
    variants = canary_variants(canaries or {})
    distinct: dict[tuple[str, str], dict[str, Any]] = {}
    exported = []
    for number, line in enumerate(lines, start=1):
        label = f"{log_name}:{number}"
        prefix = LINE_PREFIX_RE.match(line) or LINE_RE.match(line)
        message = line[prefix.end():] if prefix else line
        canary_findings = [sensitive_finding(label, "CANARY_VALUE", value, "HIGH", context=context, reveal_values=True) for value, context in variants.items() if value in message]
        pattern_findings = scan_text_content(message.encode("utf-8"), label, reveal_values) + pii_value_findings(label, message, reveal_values) + _log_key_value_findings(label, message, reveal_values)
        # A pattern match on a canary repeats its canary finding, and would mask a value that is not real.
        line_findings = canary_findings + [finding for finding in pattern_findings if finding["_raw_value"] not in variants]
        for finding in line_findings:
            key = (finding["match_type"], finding["_raw_value"])
            if key in distinct:
                distinct[key]["occurrences"] += 1
            else:
                distinct[key] = {**finding, "occurrences": 1}
        if not reveal_values:
            # Longest first, so a value inside a longer match is not masked on its own and leaves the rest readable.
            for raw in sorted({finding["_raw_value"] for finding in line_findings if finding["_raw_value"] not in variants}, key=len, reverse=True):
                line = line.replace(raw, next(item["masked_value"] for item in line_findings if item["_raw_value"] == raw))
        exported.append(line)
    findings = sorted(distinct.values(), key=lambda item: severity_rank(item["severity"]), reverse=True)[:MAX_FINDINGS]
    return findings, exported


# Describe one finding for the critical findings report.
def _evidence_line(item: dict[str, Any]) -> str:
    kind = f"{item['match_type']} ({item['context']})" if item["context"] else item["match_type"]
    return f"{item['severity']} {kind} at {item['path']}: {item['reported_value']} ({item['occurrences']} occurrence(s))"


# Return each form a canary may be logged in, as typed, without spaces or dashes, lowercased, URL-encoded and base64
# encoded, mapped to a description naming the canary and the form.
def canary_variants(canaries: dict[str, str]) -> dict[str, str]:
    variants: dict[str, str] = {}
    for name, value in canaries.items():
        forms = {
            "as typed": value,
            "without separators": re.sub(r"[\s-]", "", value),
            "lowercase": value.lower(),
            "URL-encoded": urllib.parse.quote(value, safe=""),
            "form-encoded": urllib.parse.quote_plus(value, safe=""),
            "base64": base64.b64encode(value.encode()).decode().rstrip("="),
            "URL-safe base64": base64.urlsafe_b64encode(value.encode()).decode().rstrip("="),
        }
        for form, text in forms.items():
            variants.setdefault(text, f"{name} canary, {form}")
    return variants


# Match unquoted "key=value" and "key: value" pairs whose key names a secret, as log messages usually write them.
def _log_key_value_findings(label: str, line: str, reveal_values: bool) -> list[dict[str, Any]]:
    findings = []
    for match in LOG_KEY_VALUE_RE.finditer(line):
        key, value = match.group(1), match.group(2)
        if OBJECT_REFERENCE_RE.fullmatch(value):
            continue
        if value_looks_secret(value) or (key.lower() in {"pin", "otp", "pass", "pwd", "passcode"} and value.lower() not in {"null", "none", "true", "false"}):
            findings.append(sensitive_finding(label, "SENSITIVE_KEY_VALUE", value, sensitive_key_severity(key), context=key, reveal_values=reveal_values))
    return findings


# Return whether the device reports USB debugging as on in Settings.Global.ADB_ENABLED, the setting the playbook's
# control reads, or None when it cannot be read.
def _adb_enabled(adb) -> bool | None:
    code, out, _ = adb.run(["shell", "settings", "get", "global", "adb_enabled"])
    value = (out or "").strip()
    return value == "1" if code == 0 and value in {"0", "1"} else None


# Stop the app, ignoring failures.
def _close_app(driver, package: str) -> None:
    try:
        driver.terminate_app(package)
    except Exception as exc:
        logger.debug("android runtime logs: terminate_app %s failed: %s", package, exc, exc_info=True)
