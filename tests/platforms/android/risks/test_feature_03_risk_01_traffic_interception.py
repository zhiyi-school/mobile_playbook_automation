from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import pytest

from mobile_playbook.common.storage_paths import android_work_dir
from mobile_playbook.platforms.android.device.scripted_steps import DEFAULT_CANARIES, StepsOutcome
from mobile_playbook.platforms.android.device_roles import NON_ROOTED, risk_role
from mobile_playbook.platforms.android.risks import feature_03_risk_01_traffic_interception as risk_module
from mobile_playbook.platforms.android.risks.feature_03_risk_01_traffic_interception import AndroidTrafficInterceptionRisk
from mobile_playbook.platforms.android.risks.registry import get_risk
from mobile_playbook.platforms.android.traffic_interception.ca_certificate import DEVICE_CA_PATH
from mobile_playbook.platforms.android.traffic_interception.device_proxy import ProxyState, TrafficInterceptionSetupError
from mobile_playbook.platforms.android.traffic_interception.proxy_warning import proxy_warning_keywords
from mobile_playbook.platforms.traffic_interception.burp_content import CONTENT_FILE_NAME

RISK_ID = "android-feature-03-risk-01"
PACKAGE = "com.example.app"
RUN_TIMESTAMP = "2026-11-02_00-00-00"
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJjYW5hcnkifQ.c2lnbmF0dXJlLWNhbmFyeQ"
EMAIL = DEFAULT_CANARIES["email"]
PASSWORD = DEFAULT_CANARIES["password"]
LOGIN_STEPS = [{"type": {"id": "email", "value": "{canary.email}"}}, {"type": {"id": "password", "value": "{canary.password}"}}, {"tap": {"id": "sign_in"}}]
PROXY = ProxyState(previous=None, applied="192.0.2.5:8081")


def record(host="api.example.com", body=b"", *, scheme="https", listener="0.0.0.0:8081", method="POST", path="/login", headers=(), response=b'{"ok":true}', status=200):
    """One exchange as the Burp extension writes it to the content file."""
    return {
        "schema_version": 1,
        "listener": listener,
        "client_ip": "192.0.2.20",
        "scheme": scheme,
        "host": host,
        "port": 443 if scheme == "https" else 80,
        "method": method,
        "url": f"{scheme}://{host}{path}",
        "request_headers": [f"{method} {path} HTTP/1.1", f"Host: {host}", *headers],
        "request_body_base64": base64.b64encode(body).decode(),
        "request_body_length": len(body),
        "request_body_truncated": False,
        "status_code": status,
        "response_headers": [f"HTTP/1.1 {status} OK", "Content-Type: application/json"],
        "response_body_base64": base64.b64encode(response).decode(),
        "response_body_length": len(response),
        "response_body_truncated": False,
    }


class FakeReportWriter:
    run_timestamp = RUN_TIMESTAMP

    def __init__(self, root):
        self.root = root
        self.results = []

    def test_report_dir(self, app_id, risk_id, test_case_id, platform):
        return self.root

    def write_result(self, result, report_dir):
        self.results.append(result)


class FakeDriver:
    current_package = PACKAGE

    def __init__(self, bench):
        self.bench = bench

    @property
    def page_source(self):
        return self.bench.page_source

    def activate_app(self, package):
        self.bench.emit(self.bench.on_launch)

    def terminate_app(self, package):
        pass

    def quit(self):
        self.bench.quit = True

    def get_screenshot_as_file(self, path):
        return True


class Bench:
    """The phone, the app and Burp's extension: opening the app or finishing its steps makes the extension record the
    app's exchanges, as Burp would while the content capture window is armed."""

    def __init__(self, tmp_path, monkeypatch):
        self.content_dir = tmp_path / "burp"
        self.report_dir = tmp_path / "report"
        self.report_dir.mkdir()
        self.on_launch: list[dict] = []
        self.on_steps: list[dict] = []
        self.page_source = '<hierarchy><node text="Welcome" resource-id="com.example.app:id/proxy_banner"/></hierarchy>'
        self.adb = SimpleNamespace(run=self._adb, calls=[])
        self.driver = FakeDriver(self)
        self.device = SimpleNamespace(adb=self.adb, make_driver=lambda: self.driver)
        self.quit = False
        self.steps_outcome = None
        self.restore_error = None
        self.setup_error = None
        self.ca_error = None
        self.restored: list[ProxyState] = []
        self.ca_installs = 0
        self.armed_at_launch = None
        monkeypatch.setattr(risk_module, "installed_base_apk_sha256", lambda adb, package: "ab" * 32)
        monkeypatch.setattr(risk_module, "resolve_lan_host", lambda configured, label: "192.0.2.5")
        monkeypatch.setattr(risk_module, "set_device_proxy", lambda adb, host, port: ProxyState(None, f"{host}:{port}"))
        monkeypatch.setattr(risk_module, "restore_device_proxy", self._restore)
        monkeypatch.setattr(risk_module, "fetch_burp_ca", lambda proxy_url: b"\x30" + bytes(400))
        monkeypatch.setattr(risk_module, "ensure_user_ca", self._ensure_ca)
        monkeypatch.setattr(risk_module, "check_interception", self._check)
        monkeypatch.setattr(risk_module, "run_steps", self._run_steps)

    def _adb(self, args, timeout=None):
        self.adb.calls.append(list(args))
        return 0, "", ""

    def emit(self, records):
        self.armed_at_launch = (self.content_dir / "content.armed").exists()
        with (self.content_dir / CONTENT_FILE_NAME).open("a") as handle:
            handle.writelines(json.dumps(item) + "\n" for item in records)

    def _restore(self, adb, state):
        self.restored.append(state)
        if self.restore_error:
            raise self.restore_error
        return {"status": "RESTORED", "proxy": ":0"}

    def _ensure_ca(self, driver, adb, ca, display_name, evidence_dir, *, device_path, timeout, outcome):
        self.ca_installs += 1
        outcome.pushed = True
        outcome.evidence.append({"kind": "ca_install_warning", "path": str(evidence_dir / "ca_install_warning.png"), "label": "warning"})
        if self.ca_error:
            raise self.ca_error
        outcome.action = "installed"
        return outcome

    def _check(self, adb, capture_path, *, url, browser_package, timeout):
        if self.setup_error:
            raise self.setup_error
        return {"status": "VERIFIED", "host": "example.com"}

    def _run_steps(self, driver, steps, canaries, timeout):
        self.emit(self.on_steps)
        return self.steps_outcome or StepsOutcome(configured=len(steps), completed=len(steps), canaries_typed=["email", "password"])

    def run(self, steps=None, **settings):
        shared = {"content_dir": str(self.content_dir), "launch_wait": 0, "dwell_seconds": 0, "traffic_wait_seconds": 0, "expected_hosts": ["example.com"], **settings}
        app_risk = {"enabled": True, "steps": LOGIN_STEPS if steps is None else steps}
        app = SimpleNamespace(id="example", name="Example", package_name=PACKAGE, risks={RISK_ID: app_risk})
        config = SimpleNamespace(traffic_interception=shared, tools={"burp_proxy": "http://127.0.0.1:8081"})
        writer = FakeReportWriter(self.report_dir)
        AndroidTrafficInterceptionRisk().run(app, config, self.device, writer)
        return writer.results[-1]

    def file(self, name):
        return json.loads((self.report_dir / name).read_text())


@pytest.fixture
def bench(tmp_path, monkeypatch):
    return Bench(tmp_path, monkeypatch)


LOGIN = json.dumps({"email": EMAIL, "password": PASSWORD}).encode()


def test_the_risk_is_registered_for_the_non_rooted_role_with_burp_required():
    risk = get_risk(RISK_ID)

    assert isinstance(risk, AndroidTrafficInterceptionRisk)
    assert risk.requires == ["adb", "appium", "burp"]
    assert risk_role(risk) == NON_ROOTED


def test_a_login_read_through_burp_is_at_risk_with_its_canaries(bench):
    bench.on_steps = [record(body=LOGIN)]

    result = bench.run()

    assert (result.final_status, result.verdict) == ("SENSITIVE_DATA_INTERCEPTED", "At Risk")
    assert result.metadata["canaries_found"] == ["email", "password"]
    assert result.metadata["app_hosts"] == ["api.example.com"]
    exported = bench.file("intercepted_traffic.json")["exchanges"][0]
    assert PASSWORD in exported["request_body"] and EMAIL in exported["request_body"]
    flags = {flag["id"] for flag in bench.file("critical_findings.json")["flags"]}
    assert flags == {"TRAFFIC_INTERCEPTED", "SENSITIVE_DATA_IN_INTERCEPTED_TRAFFIC"}


def test_any_decrypted_traffic_of_the_app_is_at_risk_even_with_nothing_sensitive(bench):
    bench.on_launch = [record(body=b"", method="GET", path="/config", response=b'{"theme":"dark"}')]

    result = bench.run(steps=[])

    assert (result.final_status, result.verdict) == ("TRAFFIC_INTERCEPTED", "At Risk")
    assert result.metadata["finding_count"] == 0
    assert result.metadata["coverage"] == "launch_only"


def test_a_credential_header_is_found_and_masked_in_the_evidence(bench):
    bench.on_launch = [record(method="GET", path="/profile", headers=[f"Authorization: Bearer {JWT}"])]

    result = bench.run(steps=[])

    assert result.final_status == "SENSITIVE_DATA_INTERCEPTED"
    findings = bench.file("intercepted_traffic_findings.json")["findings"]
    assert any(item["match_type"] == "SENSITIVE_HEADER" and item["context"] == "Authorization" for item in findings)
    assert all(item["value"] is None for item in findings)
    assert JWT not in (bench.report_dir / "intercepted_traffic.json").read_text()


def test_reveal_values_keeps_values_in_the_evidence(bench):
    bench.on_launch = [record(method="GET", path="/profile", headers=[f"Authorization: Bearer {JWT}"])]

    bench.run(steps=[], reveal_values=True)

    assert JWT in (bench.report_dir / "intercepted_traffic.json").read_text()


def test_cleartext_traffic_raises_the_interception_flag_to_high(bench):
    bench.on_launch = [record(scheme="http", method="GET", path="/news")]

    result = bench.run(steps=[])

    assert result.verdict == "At Risk"
    assert result.metadata["cleartext_exchange_count"] == 1
    flag = next(flag for flag in bench.file("critical_findings.json")["flags"] if flag["id"] == "TRAFFIC_INTERCEPTED")
    assert flag["severity"] == "HIGH"


def test_traffic_carrying_a_canary_is_the_apps_whatever_its_host(bench):
    bench.on_steps = [record(host="auth.vendor.test", body=LOGIN)]

    result = bench.run()

    assert result.verdict == "At Risk"
    assert result.metadata["app_hosts"] == ["auth.vendor.test"]


def test_traffic_only_to_other_hosts_cannot_be_told_apart_from_other_apps(bench):
    bench.on_launch = [record(host="clients4.google.com", method="GET", path="/generate_204")]

    result = bench.run(steps=[])

    assert (result.final_status, result.verdict) == ("APP_TRAFFIC_NOT_IDENTIFIED", "Inconclusive")
    assert "clients4.google.com" in result.errors[0]
    assert bench.file("intercepted_traffic.json") == {"exchanges": []}


def test_no_traffic_is_inconclusive_and_does_not_claim_pinning(bench):
    result = bench.run(steps=[])

    assert (result.final_status, result.verdict) == ("TRAFFIC_INTERCEPTION_NOT_OBSERVED", "Inconclusive")
    assert "does not show that it pins certificates" in result.errors[0]


def test_traffic_through_another_burp_listener_is_not_read(bench):
    bench.on_launch = [record(listener="0.0.0.0:8080", body=LOGIN)]

    result = bench.run(steps=[])

    assert result.final_status == "TRAFFIC_INTERCEPTION_NOT_OBSERVED"
    assert result.metadata["capture"]["other_listener_count"] == 1


def test_an_app_that_warns_of_the_proxy_without_sending_anything_is_reduced_risk(bench):
    bench.page_source = '<hierarchy><node text="A proxy was detected. This app will close." /></hierarchy>'

    result = bench.run()

    assert (result.final_status, result.verdict) == ("PROXY_DETECTED", "Reduced Risk")
    assert result.metadata["proxy_warning"] == ["proxy"]
    assert result.metadata["steps"]["completed"] == 0


def test_an_app_that_warns_of_the_proxy_but_still_sends_traffic_is_at_risk(bench):
    bench.page_source = '<hierarchy><node text="Proxy detected" /></hierarchy>'
    bench.on_launch = [record(method="GET", path="/config")]

    result = bench.run()

    assert result.verdict == "At Risk"


def test_failed_steps_with_no_traffic_leave_the_flow_untested(bench):
    bench.steps_outcome = StepsOutcome(configured=3, completed=1, failure={"index": 1, "step": LOGIN_STEPS[1], "error": "no element password"})

    result = bench.run()

    assert (result.final_status, result.verdict) == ("SCRIPTED_STEPS_INCOMPLETE", "Inconclusive")
    assert result.metadata["coverage"] == "scripted_partial"


def test_the_capture_is_armed_only_while_the_app_runs_and_its_raw_content_is_deleted(bench):
    bench.on_steps = [record(body=LOGIN)]

    bench.run()

    assert bench.armed_at_launch is True
    assert not (bench.content_dir / "content.armed").exists()
    assert not (bench.content_dir / CONTENT_FILE_NAME).exists()


def test_keep_raw_keeps_a_copy_of_the_capture_in_the_work_folder(bench):
    bench.on_steps = [record(body=LOGIN)]

    result = bench.run(keep_raw=True)

    raw = android_work_dir() / "traffic_interception" / "com_example_app" / f"{RUN_TIMESTAMP}_content.jsonl"
    assert result.metadata["raw_capture"] == str(raw)
    assert base64.b64encode(LOGIN).decode() in raw.read_text()
    assert not (bench.content_dir / CONTENT_FILE_NAME).exists()


def test_the_proxy_is_reset_and_the_pushed_ca_file_removed_after_the_run(bench):
    bench.on_steps = [record(body=LOGIN)]

    result = bench.run()

    assert bench.restored == [PROXY]
    assert result.metadata["device_proxy"]["restore"]["status"] == "RESTORED"
    assert ["shell", "rm", "-f", DEVICE_CA_PATH] in bench.adb.calls
    assert bench.quit is True


def test_a_setup_that_burp_cannot_decrypt_stops_before_the_app_and_still_resets_the_proxy(bench):
    bench.setup_error = TrafficInterceptionSetupError("INTERCEPTION_NOT_VERIFIED", "no decrypted HTTPS for example.com")
    bench.on_launch = [record(body=LOGIN)]

    result = bench.run()

    assert (result.final_status, result.verdict) == ("INTERCEPTION_NOT_VERIFIED", "Inconclusive")
    assert bench.armed_at_launch is None
    assert bench.restored == [PROXY]
    assert result.metadata["setup_failure"]["status"] == "INTERCEPTION_NOT_VERIFIED"


def test_a_failed_ca_install_keeps_its_screenshots_and_removes_the_pushed_file(bench):
    bench.ca_error = TrafficInterceptionSetupError("CA_INSTALL_UI_FAILED", "the warning did not appear")

    result = bench.run()

    assert (result.final_status, result.verdict) == ("CA_INSTALL_UI_FAILED", "Inconclusive")
    assert [item["kind"] for item in result.evidence] == ["ca_install_warning"]
    assert result.metadata["ca"]["pushed"] is True
    assert ["shell", "rm", "-f", DEVICE_CA_PATH] in bench.adb.calls
    assert bench.restored == [PROXY]


def test_a_proxy_that_cannot_be_reset_keeps_an_at_risk_verdict_but_says_so(bench):
    bench.on_steps = [record(body=LOGIN)]
    bench.restore_error = TrafficInterceptionSetupError("DEVICE_PROXY_RESTORE_FAILED", "Permission denial")

    result = bench.run()

    assert (result.final_status, result.verdict) == ("SENSITIVE_DATA_INTERCEPTED", "At Risk")
    assert "still points at Burp" in result.errors[-1]


def test_a_proxy_that_cannot_be_reset_otherwise_makes_the_run_inconclusive(bench):
    bench.restore_error = TrafficInterceptionSetupError("DEVICE_PROXY_RESTORE_FAILED", "Permission denial")

    result = bench.run(steps=[])

    assert (result.final_status, result.verdict) == ("DEVICE_PROXY_RESTORE_FAILED", "Inconclusive")


def test_a_capture_window_another_run_holds_is_not_taken_over(bench):
    bench.content_dir.mkdir()
    (bench.content_dir / "content.armed").write_text(json.dumps({"schema_version": 1, "listener_ports": [8081], "expires_at": 9e12}))

    result = bench.run()

    assert (result.final_status, result.verdict) == ("CONTENT_CAPTURE_BUSY", "Inconclusive")
    assert (bench.content_dir / "content.armed").exists()
    assert bench.restored == [PROXY]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ('<node text="Proxy detected. Turn it off to continue." />', ["proxy"]),
        ('<node content-desc="VPN connection active" />', ["vpn"]),
        ('<node text="Welcome" resource-id="com.example:id/proxy_banner" class="ProxyView" />', []),
        ('<node text="Your network is not secure" />', ["network is not secure"]),
    ],
)
def test_proxy_warnings_are_read_from_visible_text_only(source, expected):
    assert proxy_warning_keywords(f"<hierarchy>{source}</hierarchy>") == expected
