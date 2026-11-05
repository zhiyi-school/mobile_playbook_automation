from __future__ import annotations

import base64
import hashlib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from mobile_playbook.platforms.android.device import settings_ui
from mobile_playbook.platforms.android.traffic_interception import ca_certificate
from mobile_playbook.platforms.android.traffic_interception.ca_certificate import (
    DEVICE_CA_PATH,
    SETTINGS_ACTION,
    TRUSTED_CREDENTIALS_USER_ACTION,
    CaOutcome,
    ensure_user_ca,
    fetch_burp_ca,
    load_ca,
    push_ca,
    remove_pushed_ca,
)
from mobile_playbook.platforms.android.traffic_interception.device_proxy import TrafficInterceptionSetupError
from tests.platforms.android.settings_ui_fakes import TAP_TARGETS, Element, SettingsDriver

# Shaped like a DER certificate (an ASN.1 SEQUENCE of Burp's usual size); its content is never parsed.
CA = b"\x30\x82\x03\xa0" + bytes(range(256)) * 3
DISPLAY_NAME = "PortSwigger CA"


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(ca_certificate, "POLL_SECONDS", 0)
    monkeypatch.setattr(settings_ui, "POLL_SECONDS", 0)
    TAP_TARGETS.clear()


class Phone:
    """Fake adb for one phone: its files, typed text and the Settings screen each intent opens."""

    def __init__(self, driver=None, files=None, push_fails=False):
        self.driver = driver
        self.files: dict[str, bytes] = dict(files or {})
        self.push_fails = push_fails
        self.commands: list[list[str]] = []
        self.typed: list[str] = []

    def run(self, args, timeout=None):
        self.commands.append(list(args))
        if args[:2] == ["shell", "sha256sum"]:
            if args[2] not in self.files:
                return 1, "", f"sha256sum: {args[2]}: No such file or directory"
            return 0, f"{hashlib.sha256(self.files[args[2]]).hexdigest()}  {args[2]}\n", ""
        if args[0] == "push":
            if self.push_fails:
                return 1, "", "adb: error: failed to copy"
            self.files[args[2]] = Path(args[1]).read_bytes()
            return 0, "1 file pushed", ""
        if args[:3] == ["shell", "rm", "-f"]:
            self.files.pop(args[3], None)
            return 0, "", ""
        if args[:4] == ["shell", "am", "start", "-a"]:
            self.driver.screen = {TRUSTED_CREDENTIALS_USER_ACTION: "user_list", SETTINGS_ACTION: "home"}[args[4]]
            return 0, "Starting: Intent", ""
        if args[:3] == ["shell", "input", "tap"]:
            target = TAP_TARGETS.get((int(args[3]), int(args[4])))
            if target is not None:
                target.tapped()
            return 0, "", ""
        if args[:3] == ["shell", "input", "text"]:
            self.typed.append(args[3])
            return 0, "", ""
        return 0, "", ""

    def pushes(self):
        return [command for command in self.commands if command[0] == "push"]


def one_ui(installed=False, picker_opens_in_download=False, warning_appears=True, done_installs=True, search_field="android.widget.AutoCompleteTextView"):
    """The One UI 8.5 screens met on the way to installing a CA certificate, as seen on a Galaxy A34, whose Settings
    search field is an AutoCompleteTextView; AOSP's is an EditText."""
    driver = SettingsDriver({}, "home")
    state = {"installed": installed}
    go = lambda screen: lambda element: setattr(driver, "screen", screen)  # noqa: E731

    def user_list():
        return [Element("System"), Element("User"), Element(DISPLAY_NAME) if state["installed"] else Element("No items")]

    def done(element):
        state["installed"] = done_installs
        driver.screen = "storage"

    driver.screens.update({
        "home": [Element("Connections"), Element(desc="Search", on_click=go("search"))],
        "search": [
            Element("developer", cls=search_field),
            Element("CA certificate", on_click=go("storage")),
            Element("More security settings > Install from device storage"),
        ],
        "storage": [
            Element("Install from device storage"),
            Element("CA certificate", on_click=go("warning" if warning_appears else "storage")),
            Element("VPN and app user certificate"),
        ],
        "warning": [Element("CA certificate"), Element("Your privacy is at risk"), Element("Install anyway", on_click=go("picker_download" if picker_opens_in_download else "picker_root"))],
        "picker_root": [Element("Select item"), Element("Download", on_click=go("picker_download"))],
        "picker_download": [Element("ca1.crt"), Element("ca.crt"), Element("Done", on_click=done)],
    })
    # The User tab's contents change once the CA is installed, so they are rebuilt each time it is shown.
    driver.screens = _Screens(driver.screens, {"user_list": user_list})
    return driver, state


class _Screens(dict):
    def __init__(self, static, dynamic):
        super().__init__(static)
        self.dynamic = dynamic

    def get(self, key, default=None):
        return self.dynamic[key]() if key in self.dynamic else super().get(key, default)


class _BurpProxy(BaseHTTPRequestHandler):
    body = CA
    requested: list[str] = []

    def do_GET(self):
        _BurpProxy.requested.append(self.path)
        self.send_response(200)
        self.send_header("Content-Type", "application/x-x509-ca-cert")
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *args):
        pass


@pytest.fixture
def burp_proxy():
    _BurpProxy.requested = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _BurpProxy)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()
    _BurpProxy.body = CA


def test_the_ca_is_downloaded_from_burp_through_its_proxy_listener(burp_proxy):
    assert fetch_burp_ca(burp_proxy) == CA
    assert _BurpProxy.requested == ["http://burp/cert"]


def test_a_page_that_is_not_a_certificate_is_refused(burp_proxy):
    _BurpProxy.body = b"<html>Burp Suite Professional</html>" * 10

    with pytest.raises(TrafficInterceptionSetupError, match="did not return a DER certificate") as error:
        fetch_burp_ca(burp_proxy)

    assert error.value.status == "CA_UNAVAILABLE"


def test_an_exported_ca_is_read_in_der_or_pem_format(tmp_path):
    der = tmp_path / "burp.der"
    der.write_bytes(CA)
    pem = tmp_path / "burp.pem"
    pem.write_text("-----BEGIN CERTIFICATE-----\n" + base64.encodebytes(CA).decode() + "-----END CERTIFICATE-----\n")

    assert load_ca(der) == CA
    assert load_ca(pem) == CA


def test_the_ca_is_pushed_where_the_playbook_puts_it(tmp_path):
    phone = Phone()

    assert push_ca(phone, CA, DEVICE_CA_PATH, tmp_path, CaOutcome()) is True

    assert phone.files == {DEVICE_CA_PATH: CA}
    assert phone.pushes()[0][2] == "/sdcard/Download/ca.crt"


def test_an_identical_file_already_there_is_left_in_place(tmp_path):
    phone = Phone(files={DEVICE_CA_PATH: CA})

    assert push_ca(phone, CA, DEVICE_CA_PATH, tmp_path, CaOutcome()) is False
    assert phone.pushes() == []


def test_a_different_file_already_there_is_never_overwritten(tmp_path):
    phone = Phone(files={DEVICE_CA_PATH: b"someone else's file"})

    with pytest.raises(TrafficInterceptionSetupError, match="already holds a different file") as error:
        push_ca(phone, CA, DEVICE_CA_PATH, tmp_path, CaOutcome())

    assert error.value.status == "CA_PATH_OCCUPIED"
    assert phone.files[DEVICE_CA_PATH] == b"someone else's file"
    assert phone.pushes() == []


def test_a_failed_push_is_reported(tmp_path):
    with pytest.raises(TrafficInterceptionSetupError, match="failed to copy") as error:
        push_ca(Phone(push_fails=True), CA, DEVICE_CA_PATH, tmp_path, CaOutcome())

    assert error.value.status == "CA_PUSH_FAILED"


def test_only_a_file_this_run_pushed_is_removed():
    phone = Phone(files={DEVICE_CA_PATH: CA})

    remove_pushed_ca(phone, CaOutcome(pushed=False))
    assert DEVICE_CA_PATH in phone.files

    remove_pushed_ca(phone, CaOutcome(pushed=True))
    assert DEVICE_CA_PATH not in phone.files


def test_a_ca_settings_already_lists_is_not_installed_again(tmp_path):
    driver, _ = one_ui(installed=True)
    phone = Phone(driver)

    outcome = ensure_user_ca(driver, phone, CA, DISPLAY_NAME, tmp_path, timeout=1)

    assert outcome.action == "already_installed"
    assert outcome.pushed is False
    assert phone.pushes() == []
    assert phone.typed == []


@pytest.mark.parametrize("in_download", [False, True])
def test_the_ca_is_installed_through_settings_search_as_the_playbook_does(tmp_path, in_download):
    driver, state = one_ui(picker_opens_in_download=in_download)
    phone = Phone(driver)

    outcome = ensure_user_ca(driver, phone, CA, DISPLAY_NAME, tmp_path, timeout=1)

    assert outcome.action == "installed"
    assert outcome.pushed is True
    assert state["installed"] is True
    assert phone.typed == ["CA%scertificate"]
    assert [item["kind"] for item in outcome.evidence] == ["ca_install_warning", "ca_user_certificates"]
    assert ["shell", "am", "force-stop", "com.android.settings"] in phone.commands


def test_a_tap_is_not_repeated_while_settings_is_still_changing_page(tmp_path):
    # On a Galaxy A34, Install from device storage shows Wi-Fi certificate where the search result was. Appium keeps
    # reporting the old page for a moment after the tap, while a second tap already lands on the new page.
    driver, state = one_ui()
    result = next(element for element in driver.screens["search"] if element.text == "CA certificate")
    wifi = Element("Wi-Fi certificate", on_click=lambda element: state.update(wifi_flow=True))
    reported_pages_left = {"count": 0}
    find_elements = driver.find_elements

    def opened_storage_slowly(element):
        if not reported_pages_left["count"]:
            TAP_TARGETS[(result.cx, result.cy)] = wifi
            reported_pages_left["count"] = 4

    def stale_find(by, value):
        if driver.screen == "search" and reported_pages_left["count"]:
            reported_pages_left["count"] -= 1
            if not reported_pages_left["count"]:
                driver.screen = "storage"
        return find_elements(by, value)

    result.on_click = opened_storage_slowly
    driver.find_elements = stale_find
    phone = Phone(driver)

    outcome = ensure_user_ca(driver, phone, CA, DISPLAY_NAME, tmp_path, timeout=1)

    assert "wifi_flow" not in state
    assert outcome.action == "installed"


def test_an_aosp_edittext_search_field_works_too(tmp_path):
    driver, state = one_ui(search_field="android.widget.EditText")

    outcome = ensure_user_ca(driver, Phone(driver), CA, DISPLAY_NAME, tmp_path, timeout=1)

    assert outcome.action == "installed"
    assert state["installed"] is True


def test_the_search_query_is_never_taken_for_its_result(tmp_path):
    # On the Galaxy A34 the search field itself reads "CA certificate" once typed; listed first here, it must be skipped.
    driver, state = one_ui()
    phone = Phone(driver)
    # The field is tapped once to focus it before typing; a tap after typing would take the query for a result.
    query = Element("CA certificate", cls="android.widget.AutoCompleteTextView", on_click=lambda element: phone.typed and state.update(query_tapped=True))
    driver.screens["search"].insert(0, query)

    ensure_user_ca(driver, phone, CA, DISPLAY_NAME, tmp_path, timeout=1)

    assert "query_tapped" not in state
    assert state["installed"] is True


def test_settings_reopened_on_an_old_search_is_cleared_before_typing(tmp_path):
    driver, _ = one_ui()
    driver.screens["home"] = driver.screens["search"]
    phone = Phone(driver)

    ensure_user_ca(driver, phone, CA, DISPLAY_NAME, tmp_path, timeout=1)

    assert driver.screens["search"][0].text == ""
    assert phone.typed == ["CA%scertificate"]


def test_a_ca_missing_from_the_user_tab_after_installing_is_not_trusted(tmp_path):
    driver, _ = one_ui(done_installs=False)

    with pytest.raises(TrafficInterceptionSetupError, match="not listed under User certificates") as error:
        ensure_user_ca(driver, Phone(driver), CA, DISPLAY_NAME, tmp_path, timeout=1)

    assert error.value.status == "CA_NOT_TRUSTED"
    assert (tmp_path / "ca_setup_failure.xml").read_text() == "<hierarchy/>"


def test_settings_that_cannot_be_driven_fail_with_diagnostics(tmp_path):
    driver, _ = one_ui(warning_appears=False)

    with pytest.raises(TrafficInterceptionSetupError, match="warning with 'Install anyway' did not appear") as error:
        ensure_user_ca(driver, Phone(driver), CA, DISPLAY_NAME, tmp_path, timeout=0.2)

    assert error.value.status == "CA_INSTALL_UI_FAILED"
    assert "ca_setup_failure.png" in driver.screenshots
    assert (tmp_path / "ca_setup_failure.xml").exists()


def test_a_different_file_at_the_ca_path_stops_before_settings_is_driven(tmp_path):
    driver, state = one_ui()
    phone = Phone(driver, files={DEVICE_CA_PATH: b"someone else's file"})

    with pytest.raises(TrafficInterceptionSetupError) as error:
        ensure_user_ca(driver, phone, CA, DISPLAY_NAME, tmp_path, timeout=1)

    assert error.value.status == "CA_PATH_OCCUPIED"
    assert phone.typed == []
    assert state["installed"] is False


MY_FILES = "com.sec.android.app.myfiles"


class PhoneWithStalePicker(Phone):
    """Fake phone where My Files was left open in Settings' task: until it is stopped, Settings opens behind its search."""

    def __init__(self, driver):
        super().__init__(driver)
        self.picker_open = True

    def run(self, args, timeout=None):
        if args == ["shell", "am", "force-stop", MY_FILES]:
            self.picker_open = False
        answer = super().run(args, timeout)
        if args[:4] == ["shell", "am", "start", "-a"] and self.picker_open:
            self.driver.screen = "my_files_search"
        return answer


def my_files_search():
    return [Element("Search by name only"), Element("No results found"),
            Element("", cls="android.widget.AutoCompleteTextView", package=MY_FILES)]


def test_a_file_picker_left_open_is_stopped_before_settings_is_opened(tmp_path):
    driver, state = one_ui()
    driver.screens["my_files_search"] = my_files_search()
    phone = PhoneWithStalePicker(driver)

    outcome = ensure_user_ca(driver, phone, CA, DISPLAY_NAME, tmp_path, timeout=1)

    assert outcome.action == "installed" and state["installed"] is True
    assert phone.typed == ["CA%scertificate"]
    stop = phone.commands.index(["shell", "am", "force-stop", MY_FILES])
    assert stop < phone.commands.index(["shell", "am", "start", "-a", TRUSTED_CREDENTIALS_USER_ACTION])


def test_another_apps_search_field_in_front_of_settings_is_never_typed_into(tmp_path):
    driver, state = one_ui()
    driver.screens["home"] = [Element("", cls="android.widget.EditText", package="com.example.notes")]
    phone = Phone(driver)

    with pytest.raises(TrafficInterceptionSetupError, match="com.example.notes was in front of Settings") as error:
        ensure_user_ca(driver, phone, CA, DISPLAY_NAME, tmp_path, timeout=1)

    assert error.value.status == "CA_INSTALL_UI_FAILED"
    assert phone.typed == [] and state["installed"] is False
