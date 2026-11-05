from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

import pytest

from mobile_playbook.platforms.android.traffic_interception import interception_check
from mobile_playbook.platforms.android.traffic_interception.device_proxy import TrafficInterceptionSetupError
from mobile_playbook.platforms.android.traffic_interception.interception_check import check_interception

FORCE_STOP_CHROME = ["shell", "am", "force-stop", "com.android.chrome"]


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(interception_check, "POLL_SECONDS", 0)


def _entry(host, scheme="https"):
    return json.dumps({"schema_version": 1, "scheme": scheme, "host": host, "port": 443, "method": "GET", "path": "/"}) + "\n"


class Browser:
    """Fake adb whose browser, once opened, makes Burp's extension append the given lines to the capture file."""

    def __init__(self, capture, lines, start_result=(0, "Starting: Intent", "")):
        self.capture = capture
        self.lines = lines
        self.start_result = start_result
        self.commands: list[list[str]] = []

    def run(self, args, timeout=None):
        self.commands.append(list(args))
        if args[:3] == ["shell", "am", "start"]:
            with self.capture.open("a") as handle:
                handle.writelines(self.lines)
            return self.start_result
        return 0, "", ""


def test_decrypted_https_for_the_check_host_verifies_the_setup(tmp_path):
    capture = tmp_path / "capture.jsonl"
    capture.write_text(_entry("example.com"))
    adb = Browser(capture, [_entry("clients4.google.com"), _entry("example.com")])

    result = check_interception(adb, capture, timeout=1)

    assert result["status"] == "VERIFIED"
    assert adb.commands[0][:6] == ["shell", "am", "start", "-a", "android.intent.action.VIEW", "-d"]
    assert adb.commands[0][7:] == ["-p", "com.android.chrome"]
    assert adb.commands[0][6] == result["url"]
    assert adb.commands[-1] == FORCE_STOP_CHROME


def _opened_url(adb):
    return urlsplit(adb.commands[0][6])


def test_each_check_opens_a_url_the_browser_has_not_cached(tmp_path):
    capture = tmp_path / "capture.jsonl"
    first, second = (Browser(capture, [_entry("example.com")]) for _ in range(2))

    check_interception(first, capture, url="https://example.com/page?lang=en", timeout=1)
    check_interception(second, capture, url="https://example.com/page?lang=en", timeout=1)

    opened = [_opened_url(first), _opened_url(second)]
    assert [(url.scheme, url.netloc, url.path) for url in opened] == [("https", "example.com", "/page")] * 2
    queries = [parse_qs(url.query) for url in opened]
    assert all(query["lang"] == ["en"] for query in queries)
    assert queries[0]["interception_check"] != queries[1]["interception_check"]


@pytest.mark.parametrize("lines", [[], [_entry("example.com", scheme="http")], [_entry("clients4.google.com")]])
def test_no_decrypted_https_for_the_check_host_is_not_verified(tmp_path, lines):
    capture = tmp_path / "capture.jsonl"
    capture.write_text(_entry("example.com"))
    adb = Browser(capture, lines)

    with pytest.raises(TrafficInterceptionSetupError, match="no decrypted HTTPS for example.com") as error:
        check_interception(adb, capture, timeout=0.1)

    assert error.value.status == "INTERCEPTION_NOT_VERIFIED"
    assert adb.commands[-1] == FORCE_STOP_CHROME


def test_a_browser_that_does_not_open_is_not_verified(tmp_path):
    capture = tmp_path / "capture.jsonl"
    adb = Browser(capture, [], start_result=(0, "Error: Activity not started, unable to resolve Intent", ""))

    with pytest.raises(TrafficInterceptionSetupError, match=r"opening https://example.com/\?interception_check=\w+ in com.android.chrome failed") as error:
        check_interception(adb, capture, timeout=1)

    assert error.value.status == "INTERCEPTION_NOT_VERIFIED"
    assert adb.commands[-1] == FORCE_STOP_CHROME


def test_a_replaced_capture_file_is_not_verified(tmp_path):
    capture = tmp_path / "capture.jsonl"
    capture.write_text(_entry("example.com") * 3)

    class Replacing(Browser):
        def run(self, args, timeout=None):
            if args[:3] == ["shell", "am", "start"]:
                capture.unlink()
                capture.write_text(_entry("example.com"))
            return super().run(args, timeout)

    with pytest.raises(TrafficInterceptionSetupError, match="was replaced or became unreadable"):
        check_interception(Replacing(capture, []), capture, timeout=1)
