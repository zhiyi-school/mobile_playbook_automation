from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from mobile_playbook.common.storage_paths import android_work_dir
from mobile_playbook.platforms.android.device_roles import NON_ROOTED, risk_role
from mobile_playbook.platforms.android.risks import feature_02_risk_02_runtime_logs as runtime_logs
from mobile_playbook.platforms.android.risks.feature_02_risk_02_runtime_logs import AndroidRuntimeLogsRisk, scan_log_lines
from mobile_playbook.platforms.android.risks.registry import get_risk

PACKAGE = "com.example.app"
UID = 10234
RUN_TIMESTAMP = "2026-10-21_00-00-00"
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJjYW5hcnkifQ.c2lnbmF0dXJlLWNhbmFyeQ"
EMAIL = "canary.logtest@example.com"
CARD = "4111 1111 1111 1111"

SENSITIVE_LOG = [
    f"10-21 12:00:00.100 {UID}  4321  4330 D Auth: session token {JWT}",
    f"10-21 12:00:00.200 {UID}  4321  4330 I Profile: signed in as {EMAIL}",
    f"10-21 12:00:00.300 {UID}  4321  4330 I Payment: charged card {CARD}",
    f"10-21 12:00:00.400 {UID}  4321  4330 D Auth: refreshed token {JWT}",
    f"10-21 12:00:00.500 10999  5555  5555 D Other: another app's {EMAIL.replace('canary', 'other')}",
]
PLAIN_LOG = [f"10-21 12:00:00.100 {UID}  4321  4330 I Main: onCreate", "10-21 12:00:00.200 10999  5555  5555 D Other: unrelated"]
OTHER_APP_LOG = ["10-21 12:00:00.200 10999  5555  5555 D Other: unrelated"]


class FakeReportWriter:
    run_timestamp = RUN_TIMESTAMP

    def __init__(self, root):
        self.root = root
        self.results = []

    def test_report_dir(self, app_id, risk_id, test_case_id, platform):
        return self.root

    def write_result(self, result, report_dir):
        self.results.append(result)


class FakeProcess:
    def __init__(self, ends_early=False):
        self.returncode = None
        self.ends_early = ends_early

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


class LogcatAdb:
    """A device over USB debugging whose logcat writes `log` once the app is launched."""

    adb_path = "adb"

    def __init__(self, log=(), *, uid_supported=True, installed=True, clear_fails=False, pids=(4321,), logcat_ends_early=False):
        self.log = list(log)
        self.uid_supported = uid_supported
        self.installed = installed
        self.clear_fails = clear_fails
        self.pids = list(pids)
        self.logcat_ends_early = logcat_ends_early
        self.calls: list[list[str]] = []
        self.capture_path = None
        self.process = None

    def run(self, args, timeout=None):
        self.calls.append(args)
        if args == ["shell", "pm", "path", PACKAGE]:
            return 0, "package:/data/app/x/base.apk\n", ""
        if args[:2] == ["shell", "sha256sum"]:
            return 0, "ab" * 32 + "  /data/app/x/base.apk\n", ""
        if args == ["shell", "settings", "get", "global", "adb_enabled"]:
            return 0, "1\n", ""
        if args == ["shell", "dumpsys", "package", PACKAGE]:
            return 0, (f"Packages:\n  Package [{PACKAGE}] (1a2b):\n    userId={UID}\n" if self.installed else ""), ""
        if args == ["logcat", "-c"]:
            return (1, "", "failed to clear the 'main' log") if self.clear_fails else (0, "", "")
        if args == ["shell", "ps", "-A"]:
            rows = "".join(f"u0_a234 {pid} 612 1 1 0 0 S {PACKAGE}\n" for pid in self.pids)
            return 0, "USER PID PPID VSZ RSS WCHAN ADDR S NAME\n" + rows + "root 1 0 1 1 0 0 S init\n", ""
        return 0, "", ""

    def start_to_file(self, args, destination):
        self.calls.append(args)
        if "uid" in args and not self.uid_supported:
            process = FakeProcess()
            process.returncode = 1
            return process
        self.capture_path = destination
        self.process = FakeProcess()
        return self.process

    def app_launched(self):
        if self.capture_path is not None:
            self.append(["--------- beginning of main", *self.log])
            if self.logcat_ends_early:
                self.process.returncode = 255

    def append(self, lines):
        with self.capture_path.open("a") as handle:
            handle.write("".join(f"{line}\n" for line in lines))


class FakeElement:
    """A field that, when typed into, writes the line `logs` formats with the typed text, as a leaky app would."""

    def __init__(self, adb, actions, logs=None):
        self.adb = adb
        self.actions = actions
        self.logs = logs

    def click(self):
        self.actions.append("click")

    def clear(self):
        pass

    def send_keys(self, text):
        self.actions.append(f"type:{text}")
        if self.logs:
            self.adb.append([self.logs.format(text=text)])


class FakeDriver:
    def __init__(self, adb, *, page_source="<hierarchy/>", foreground=PACKAGE, launch_error=None, fields=None):
        self.adb = adb
        self.page_source = page_source
        self.current_package = foreground
        self.launch_error = launch_error
        self.calls: list[str] = []
        self.fields = fields or {}

    def find_elements(self, by, target):
        if target not in self.fields:
            return []
        return [FakeElement(self.adb, self.calls, self.fields[target])]

    def activate_app(self, package):
        self.calls.append(f"activate:{package}")
        if self.launch_error:
            raise RuntimeError(self.launch_error)
        self.adb.app_launched()

    def terminate_app(self, package):
        self.calls.append(f"terminate:{package}")

    def quit(self):
        self.calls.append("quit")


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(runtime_logs.time, "sleep", lambda seconds: None)
    monkeypatch.setattr("mobile_playbook.platforms.android.device.logcat.time.sleep", lambda seconds: None)


def run_risk(tmp_path, adb, *, driver=None, config=None, app_risk_config=None):
    report_dir = tmp_path / "report"
    report_dir.mkdir()
    driver = driver or FakeDriver(adb)
    app_config = SimpleNamespace(id="example", name="Example", package_name=PACKAGE, risks={"android-feature-02-risk-02": app_risk_config or {}})
    global_config = SimpleNamespace(runtime_logs={"launch_wait": 0, "dwell_seconds": 0, "step_timeout_seconds": 0, **(config or {})})
    writer = FakeReportWriter(report_dir)
    device_client = SimpleNamespace(adb=adb, make_driver=lambda: driver)
    result = AndroidRuntimeLogsRisk().run(app_config, global_config, device_client, writer)
    assert writer.results == [result]
    return result


def report_file(tmp_path, name):
    return tmp_path / "report" / name


def raw_log_path():
    return android_work_dir() / "runtime_logs" / PACKAGE.replace(".", "_") / f"{RUN_TIMESTAMP}_logcat.txt"


def critical_flags(tmp_path):
    return json.loads(report_file(tmp_path, "critical_findings.json").read_text())["flags"]


def test_the_risk_is_registered_and_runs_on_the_non_rooted_device():
    risk = get_risk("android-feature-02-risk-02")

    assert isinstance(risk, AndroidRuntimeLogsRisk)
    assert risk_role(risk) == NON_ROOTED


def test_sensitive_data_in_the_apps_log_is_at_risk_with_values_masked(tmp_path):
    adb = LogcatAdb(SENSITIVE_LOG)

    result = run_risk(tmp_path, adb)

    assert (result.final_status, result.verdict, result.errors) == ("SENSITIVE_DATA_LOGGED", "At Risk", [])
    assert result.metadata["adb_enabled"] is True
    assert result.metadata["app_uid"] == UID
    assert result.metadata["uid_in_log"] is True
    assert result.metadata["coverage"] == "launch_only"
    assert result.metadata["log_buffer_cleared"] is True
    assert result.metadata["app_line_count"] == 4
    exported = report_file(tmp_path, f"logcat_{PACKAGE}.txt").read_text()
    assert len(exported.splitlines()) == 4
    assert "Other:" not in exported
    for value in (JWT, EMAIL, CARD):
        assert value not in exported
        assert value not in json.dumps(result.to_dict())
        assert value not in report_file(tmp_path, "runtime_log_findings.json").read_text()
        assert value not in report_file(tmp_path, "critical_findings.md").read_text()
    flag = critical_flags(tmp_path)[0]
    assert flag["id"] == "SENSITIVE_DATA_IN_RUNTIME_LOGS"
    assert flag["severity"] == "HIGH"
    assert any("JWT" in line and "(2 occurrence(s))" in line for line in flag["evidence"])
    assert {item["kind"] for item in result.evidence} == {"runtime_log", "runtime_log_findings", "critical_findings"}
    assert ["logcat", "-c"] in adb.calls
    assert adb.calls.index(["shell", "am", "force-stop", PACKAGE]) < adb.calls.index(["logcat", "-c"])
    assert not raw_log_path().exists()


def test_reveal_values_reports_the_values_in_full(tmp_path):
    result = run_risk(tmp_path, LogcatAdb(SENSITIVE_LOG), config={"reveal_values": True})

    assert result.final_status == "SENSITIVE_DATA_LOGGED"
    assert JWT in report_file(tmp_path, f"logcat_{PACKAGE}.txt").read_text()
    assert EMAIL in report_file(tmp_path, "runtime_log_findings.json").read_text()


def test_an_app_override_takes_precedence_over_the_shared_setting(tmp_path):
    run_risk(tmp_path, LogcatAdb(SENSITIVE_LOG), config={"reveal_values": False}, app_risk_config={"reveal_values": True})

    assert EMAIL in report_file(tmp_path, f"logcat_{PACKAGE}.txt").read_text()


def test_keep_raw_keeps_the_unfiltered_capture_out_of_the_report(tmp_path):
    result = run_risk(tmp_path, LogcatAdb(SENSITIVE_LOG), config={"keep_raw": True})

    assert result.metadata["raw_log"] == str(raw_log_path())
    assert "Other:" in raw_log_path().read_text()
    assert all("Other:" not in path.read_text() for path in report_file(tmp_path, "").iterdir())


def test_a_log_without_sensitive_data_is_reduced_risk(tmp_path):
    result = run_risk(tmp_path, LogcatAdb(PLAIN_LOG))

    assert (result.final_status, result.verdict) == ("NO_SENSITIVE_DATA_LOGGED", "Reduced Risk")
    assert report_file(tmp_path, f"logcat_{PACKAGE}.txt").read_text() == PLAIN_LOG[0] + "\n"
    assert critical_flags(tmp_path) == []


def test_an_app_that_ran_without_logging_is_reduced_risk(tmp_path):
    result = run_risk(tmp_path, LogcatAdb(OTHER_APP_LOG))

    assert (result.final_status, result.verdict) == ("NO_APP_LOGS", "Reduced Risk")
    assert result.metadata["app_pids"] == [4321]


def test_an_app_that_never_ran_is_inconclusive(tmp_path):
    result = run_risk(tmp_path, LogcatAdb(OTHER_APP_LOG, pids=()))

    assert (result.final_status, result.verdict) == ("FAILED", "Inconclusive")
    assert "did not start" in result.errors[0]


def test_a_usb_debugging_warning_is_reduced_risk_without_waiting(tmp_path, monkeypatch):
    dwelled = []
    monkeypatch.setattr(AndroidRuntimeLogsRisk, "_dwell", lambda self, *args: dwelled.append(args) or set())
    adb = LogcatAdb(PLAIN_LOG)
    driver = FakeDriver(adb, page_source='<Text text="USB Debugging Enabled. Please turn off USB debugging."/>')

    result = run_risk(tmp_path, adb, driver=driver)

    assert (result.final_status, result.verdict) == ("USB_DEBUGGING_BLOCKED", "Reduced Risk")
    assert "usb debugging" in result.metadata["usb_debugging_warning"]
    assert dwelled == []


def test_sensitive_data_logged_before_the_warning_is_still_at_risk(tmp_path):
    adb = LogcatAdb(SENSITIVE_LOG)
    driver = FakeDriver(adb, page_source='<Text text="USB debugging detected"/>')

    result = run_risk(tmp_path, adb, driver=driver)

    assert (result.final_status, result.verdict) == ("SENSITIVE_DATA_LOGGED", "At Risk")


def test_warning_text_in_another_app_is_not_a_block(tmp_path):
    adb = LogcatAdb(PLAIN_LOG)
    driver = FakeDriver(adb, page_source='<Text text="Developer options"/>', foreground="com.android.settings")

    result = run_risk(tmp_path, adb, driver=driver)

    assert result.final_status == "NO_SENSITIVE_DATA_LOGGED"
    assert result.metadata["usb_debugging_warning"] == []


def test_devices_without_uid_in_the_log_filter_by_the_apps_pids(tmp_path):
    log = ["10-21 12:00:00.100  4321  4330 I Profile: signed in as " + EMAIL, "10-21 12:00:00.200  5555  5555 D Other: unrelated"]
    adb = LogcatAdb(log, uid_supported=False)

    result = run_risk(tmp_path, adb, config={"dwell_seconds": 0.01})

    assert result.metadata["uid_in_log"] is False
    assert result.final_status == "SENSITIVE_DATA_LOGGED"
    assert result.metadata["app_line_count"] == 1


def test_a_buffer_that_could_not_be_cleared_is_recorded(tmp_path):
    result = run_risk(tmp_path, LogcatAdb(PLAIN_LOG, clear_fails=True))

    assert result.final_status == "NO_SENSITIVE_DATA_LOGGED"
    assert result.metadata["log_buffer_cleared"] is False
    assert result.metadata["log_buffer_clear_error"] == "failed to clear the 'main' log"


def test_an_app_that_is_not_installed_fails(tmp_path):
    adb = LogcatAdb(installed=False)

    result = run_risk(tmp_path, adb)

    assert (result.final_status, result.verdict) == ("FAILED", "Inconclusive")
    assert "is it installed" in result.errors[0]
    assert adb.process is None


def test_a_capture_that_ends_early_fails_and_leaves_nothing_behind(tmp_path):
    adb = LogcatAdb(SENSITIVE_LOG, logcat_ends_early=True)

    result = run_risk(tmp_path, adb)

    assert (result.final_status, result.verdict) == ("FAILED", "Inconclusive")
    assert "logcat stopped" in result.errors[0]
    assert not raw_log_path().exists()


def test_a_launch_failure_stops_the_capture_and_quits_the_driver(tmp_path):
    adb = LogcatAdb(PLAIN_LOG)
    driver = FakeDriver(adb, launch_error="activity not found")

    result = run_risk(tmp_path, adb, driver=driver)

    assert (result.final_status, result.errors) == ("FAILED", ["activity not found"])
    assert adb.process.returncode is not None
    assert driver.calls[-1] == "quit"
    assert not raw_log_path().exists()


def test_scan_log_lines_masks_a_longer_value_before_one_inside_it():
    line = f"D Auth: password={EMAIL}"

    findings, exported = scan_log_lines([line], "logcat.txt", reveal_values=False)

    assert {item["match_type"] for item in findings} >= {"EMAIL_ADDRESS"}
    assert EMAIL not in exported[0]


@pytest.mark.parametrize(
    ("message", "value"),
    [
        ("password=hunter22", "hunter22"),
        ("user_password: Canary-Pw-7Q4z", "Canary-Pw-7Q4z"),
        ("Authorization: Bearer opaqueTok3n123", "opaqueTok3n123"),
        ("pin=1234", "1234"),
        ('header token="abcdef12345"', "abcdef12345"),
    ],
)
def test_scan_log_lines_finds_secrets_logged_as_key_value_pairs(message, value):
    findings, exported = scan_log_lines([f"10-21 12:00:00.100 {UID}  4321  4330 D Login   : {message}"], "logcat.txt", reveal_values=False)

    assert value in {item["_raw_value"] for item in findings}
    assert value not in exported[0]


@pytest.mark.parametrize("message", ["session: started", "bypass=enabled", "token: null", "Token refresh scheduled", "execute start, token = android.os.BinderProxy@e00118d", "token=Handler@1a2b3c", "r=ActivityRecord{e332f0b token=android.os.BinderProxy@1ac1b53}", "state (token=android.os.BinderProxy@1ac1b53)"])
def test_scan_log_lines_ignores_ordinary_messages(message):
    assert scan_log_lines([f"10-21 12:00:00.100 {UID}  4321  4330 D Auth: {message}"], "logcat.txt", reveal_values=False)[0] == []


def test_scan_log_lines_does_not_read_the_tag_as_a_key():
    line = f"10-21 12:00:00.100 {UID}  4321  4330 I Token   : refreshed ok"

    assert scan_log_lines([line], "logcat.txt", reveal_values=False) == ([], [line])


SIGN_IN = [
    {"type": {"id": "email", "value": "{canary.email}"}},
    {"type": {"id": "password", "value": "{canary.password}"}},
    {"tap": {"id": "sign_in"}},
]


def scripted_driver(adb, email_log=None, password_log=None, **settings):
    return FakeDriver(adb, fields={"email": email_log, "password": password_log, "sign_in": None}, **settings)


def test_a_canary_typed_in_a_scripted_step_and_logged_is_at_risk_and_reported_in_full(tmp_path):
    adb = LogcatAdb(PLAIN_LOG)
    driver = scripted_driver(adb, email_log=f"10-21 12:00:01.000 {UID}  4321  4330 D Login: user {{text}}")

    result = run_risk(tmp_path, adb, driver=driver, app_risk_config={"steps": SIGN_IN})

    assert (result.final_status, result.verdict) == ("SENSITIVE_DATA_LOGGED", "At Risk")
    assert result.metadata["coverage"] == "scripted"
    assert result.metadata["steps"] == {"configured": 3, "completed": 3, "failure": None, "canaries_typed": ["email", "password"]}
    assert result.metadata["canaries_found"] == ["email"]
    assert "type:canary.logtest@example.com" in driver.calls
    assert EMAIL in report_file(tmp_path, f"logcat_{PACKAGE}.txt").read_text()
    findings = json.loads(report_file(tmp_path, "runtime_log_findings.json").read_text())["findings"]
    assert [(item["match_type"], item["context"], item["value"]) for item in findings] == [("CANARY_VALUE", "email canary, as typed", EMAIL)]
    assert "CANARY_VALUE (email canary, as typed)" in critical_flags(tmp_path)[0]["evidence"][0]


@pytest.mark.parametrize(
    ("logged", "form"),
    [
        ("Q2FuYXJ5LVB3LTdRNHo", "base64"),
        ("Canary-Pw-7Q4z".lower(), "lowercase"),
        ("CanaryPw7Q4z", "without separators"),
    ],
)
def test_a_canary_is_found_in_its_encoded_forms(tmp_path, logged, form):
    adb = LogcatAdb(PLAIN_LOG)
    driver = scripted_driver(adb, password_log=f"10-21 12:00:01.000 {UID}  4321  4330 D Login: hash input " + logged)

    result = run_risk(tmp_path, adb, driver=driver, app_risk_config={"steps": SIGN_IN})

    assert result.final_status == "SENSITIVE_DATA_LOGGED"
    contexts = [item["context"] for item in json.loads(report_file(tmp_path, "runtime_log_findings.json").read_text())["findings"]]
    assert f"password canary, {form}" in contexts


def test_a_shared_or_app_canary_replaces_the_default(tmp_path):
    adb = LogcatAdb(PLAIN_LOG)
    driver = scripted_driver(adb, email_log=f"10-21 12:00:01.000 {UID}  4321  4330 D Login: user {{text}}")

    run_risk(tmp_path, adb, driver=driver, config={"canaries": {"email": "shared@example.com"}}, app_risk_config={"steps": SIGN_IN, "canaries": {"password": "App-Pw-0000"}})

    assert "type:shared@example.com" in driver.calls
    assert "type:App-Pw-0000" in driver.calls


def test_scripted_steps_that_log_nothing_sensitive_are_reduced_risk(tmp_path):
    adb = LogcatAdb(PLAIN_LOG)

    result = run_risk(tmp_path, adb, driver=scripted_driver(adb), app_risk_config={"steps": SIGN_IN})

    assert (result.final_status, result.verdict) == ("NO_SENSITIVE_DATA_LOGGED", "Reduced Risk")
    assert result.metadata["canaries_found"] == []


def test_a_failed_step_with_a_clean_log_is_inconclusive(tmp_path):
    adb = LogcatAdb(PLAIN_LOG)
    driver = FakeDriver(adb, fields={"email": None})

    result = run_risk(tmp_path, adb, driver=driver, app_risk_config={"steps": SIGN_IN})

    assert (result.final_status, result.verdict) == ("SCRIPTED_STEPS_INCOMPLETE", "Inconclusive")
    assert result.metadata["coverage"] == "scripted_partial"
    assert result.metadata["steps"]["completed"] == 1
    assert result.metadata["steps"]["failure"]["index"] == 1
    assert result.errors == ["Scripted step 2 of 3 failed, so the flow it leads to was not tested: no element matched id 'password' within 0s"]


def test_a_failed_step_after_a_leak_is_still_at_risk(tmp_path):
    adb = LogcatAdb(PLAIN_LOG)
    driver = FakeDriver(adb, fields={"email": f"10-21 12:00:01.000 {UID}  4321  4330 D Login: user {{text}}"})

    result = run_risk(tmp_path, adb, driver=driver, app_risk_config={"steps": SIGN_IN})

    assert (result.final_status, result.verdict) == ("SENSITIVE_DATA_LOGGED", "At Risk")
    assert result.metadata["coverage"] == "scripted_partial"
    assert result.errors == []


def test_steps_are_not_run_when_the_app_warns_about_usb_debugging(tmp_path):
    adb = LogcatAdb(PLAIN_LOG)
    driver = scripted_driver(adb, page_source='<Text text="USB debugging detected"/>')

    result = run_risk(tmp_path, adb, driver=driver, app_risk_config={"steps": SIGN_IN})

    assert result.final_status == "USB_DEBUGGING_BLOCKED"
    assert result.metadata["steps"] == {"configured": 3, "completed": 0, "failure": None, "canaries_typed": []}
    assert not any(call.startswith("type:") for call in driver.calls)


def test_canaries_not_typed_are_not_searched_for(tmp_path):
    log = [f"10-21 12:00:00.100 {UID}  4321  4330 D Debug: sample card 4111111111111111"]
    adb = LogcatAdb(log)
    steps = [{"type": {"id": "email", "value": "{canary.email}"}}]

    run_risk(tmp_path, adb, driver=scripted_driver(adb), app_risk_config={"steps": steps})

    findings = json.loads(report_file(tmp_path, "runtime_log_findings.json").read_text())["findings"]
    assert [item["match_type"] for item in findings] == ["PAYMENT_CARD_NUMBER"]


@pytest.mark.parametrize(
    "line",
    [
        "10-21 10:34:07.325 10364 32439  7994 W unknown:ViewManagerPropertyUpdater: Could not find generated setter for class com.example.Manager",
        "10-21 10:34:07.325 10364 32439  7994 W unknown:ViewManagerPropertyUpdater:no space after the tag",
        "10-21 10:34:07.325 32439  7994 W unknown:ViewManagerPropertyUpdater: Could not find generated setter",
    ],
)
def test_scan_log_lines_never_reads_the_header_digits_as_a_card_number(line):
    assert scan_log_lines([line], "logcat.txt", reveal_values=False) == ([], [line])


def test_scan_log_lines_scans_the_message_after_a_tag_with_a_colon():
    line = f"10-21 10:34:07.325 {UID} 32439  7994 D unknown:Auth: signed in as {EMAIL}"

    findings, exported = scan_log_lines([line], "logcat.txt", reveal_values=False)

    assert [item["match_type"] for item in findings] == ["EMAIL_ADDRESS"]
    assert EMAIL not in exported[0]
