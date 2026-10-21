from __future__ import annotations

import base64
import importlib.util
import json
import sys
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from mobile_playbook.platforms.traffic_interception import burp_content
from mobile_playbook.platforms.traffic_interception.burp_capture import poll_capture, snapshot_capture
from mobile_playbook.platforms.traffic_interception.burp_content import arm_content_capture, disarm_content_capture, read_content

EXTENSION_PATH = Path(__file__).resolve().parents[2] / "tools" / "burp" / "traffic_capture_extension.py"
TOOL_PROXY = 4
REQUEST = b'POST /login HTTP/1.1\r\nHost: api.example.com\r\n\r\n{"email":"canary.logtest@example.com","password":"Canary-Pw-7Q4z"}'
RESPONSE = b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n{"token":"synthetic-session"}'


class FakeUrl:
    def getProtocol(self):
        return "https"

    def getHost(self):
        return "api.example.com"

    def getPort(self):
        return 443

    def getPath(self):
        return "/login"

    def __str__(self):
        return "https://api.example.com:443/login"


class FakeHelpers:
    def analyzeRequest(self, message_info):
        head, _, _ = message_info.getRequest().partition(b"\r\n\r\n")
        return SimpleNamespace(
            getUrl=FakeUrl,
            getMethod=lambda: "POST",
            getHeaders=lambda: head.decode().split("\r\n"),
            getBodyOffset=lambda: len(head) + 4,
        )

    def analyzeResponse(self, response):
        head, _, _ = response.partition(b"\r\n\r\n")
        return SimpleNamespace(
            getStatusCode=lambda: 200,
            getHeaders=lambda: head.decode().split("\r\n"),
            getBodyOffset=lambda: len(head) + 4,
            getStatedMimeType=lambda: "JSON",
        )

    def base64Encode(self, data):
        return base64.b64encode(bytes(data)).decode()


class FakeCallbacks:
    TOOL_PROXY = TOOL_PROXY

    def __init__(self):
        self.output, self.errors, self.http_listeners, self.proxy_listeners = [], [], [], []

    def getExtensionFilename(self):
        return str(EXTENSION_PATH)

    def getHelpers(self):
        return FakeHelpers()

    def setExtensionName(self, name):
        self.name = name

    def registerHttpListener(self, listener):
        self.http_listeners.append(listener)

    def registerProxyListener(self, listener):
        self.proxy_listeners.append(listener)

    def printOutput(self, text):
        self.output.append(text)

    def printError(self, text):
        self.errors.append(text)


def _message_info(response=RESPONSE):
    return SimpleNamespace(getRequest=lambda: REQUEST, getResponse=lambda: response)


def _proxy_message(listener="0.0.0.0:8081", response=RESPONSE):
    info = _message_info(response)
    return SimpleNamespace(getListenerInterface=lambda: listener, getClientIpAddress=lambda: "192.0.2.10", getMessageInfo=lambda: info)


@pytest.fixture
def extension(monkeypatch, tmp_path):
    burp = ModuleType("burp")
    for name in ("IBurpExtender", "IHttpListener", "IProxyListener"):
        setattr(burp, name, type(name, (), {}))
    monkeypatch.setitem(sys.modules, "burp", burp)
    monkeypatch.setenv("MPA_BURP_CAPTURE_PATH", str(tmp_path / "capture.jsonl"))
    monkeypatch.setenv("MPA_BURP_CONTENT_DIR", str(tmp_path / "content"))
    spec = importlib.util.spec_from_file_location("traffic_capture_extension", EXTENSION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    callbacks = FakeCallbacks()
    extender = module.BurpExtender()
    extender.registerExtenderCallbacks(callbacks)
    return SimpleNamespace(module=module, extender=extender, callbacks=callbacks, capture=tmp_path / "capture.jsonl", content_dir=tmp_path / "content")


def test_names_match_the_reader(extension):
    assert extension.module.CONTENT_FILE_NAME == burp_content.CONTENT_FILE_NAME
    assert extension.module.MARKER_FILE_NAME == burp_content.MARKER_FILE_NAME
    assert extension.module.DEFAULT_MAX_BODY_BYTES == burp_content.DEFAULT_MAX_BODY_BYTES


def test_registers_both_listeners(extension):
    assert extension.callbacks.http_listeners == [extension.extender]
    assert extension.callbacks.proxy_listeners == [extension.extender]
    assert extension.callbacks.errors == []


def test_content_is_not_recorded_while_unarmed(extension):
    extension.extender.processProxyMessage(False, _proxy_message())

    assert not (extension.content_dir / "content.jsonl").exists()


def test_armed_window_round_trips_through_the_reader(extension):
    window = arm_content_capture(extension.content_dir, [8081])

    extension.extender.processProxyMessage(True, _proxy_message())
    extension.extender.processProxyMessage(False, _proxy_message())
    extension.extender.processProxyMessage(False, _proxy_message(listener="0.0.0.0:8080"))
    extension.extender.processProxyMessage(False, _proxy_message(response=None))
    disarm_content_capture(window)
    extension.extender.processProxyMessage(False, _proxy_message())

    read = read_content(window)
    assert extension.callbacks.errors == []
    assert read.malformed_count == 0
    assert len(read.exchanges) == 2
    exchange, unanswered = read.exchanges
    assert exchange.request_body == REQUEST.partition(b"\r\n\r\n")[2]
    assert exchange.request_headers == ["POST /login HTTP/1.1", "Host: api.example.com"]
    assert exchange.response_body == b'{"token":"synthetic-session"}'
    assert (exchange.listener, exchange.client_ip, exchange.status_code) == ("0.0.0.0:8081", "192.0.2.10", 200)
    assert exchange.url == "https://api.example.com:443/login"
    assert (unanswered.status_code, unanswered.response_body, unanswered.response_headers) == (None, b"", [])


def test_bodies_are_cut_at_the_armed_limit(extension):
    window = arm_content_capture(extension.content_dir, [8081], max_body_bytes=10)

    extension.extender.processProxyMessage(False, _proxy_message())

    exchange = read_content(window).exchanges[0]
    assert exchange.request_body == REQUEST.partition(b"\r\n\r\n")[2][:10]
    assert exchange.request_body_truncated is True
    assert exchange.request_body_length == len(REQUEST.partition(b"\r\n\r\n")[2])


def test_expired_marker_records_nothing(extension):
    extension.content_dir.mkdir()
    (extension.content_dir / "content.armed").write_text(json.dumps({"schema_version": 1, "listener_ports": [8081], "expires_at": time.time() - 1}))

    extension.extender.processProxyMessage(False, _proxy_message())

    assert not (extension.content_dir / "content.jsonl").exists()


def test_metadata_capture_is_unchanged(extension):
    cursor = snapshot_capture(extension.capture)

    extension.extender.processHttpMessage(TOOL_PROXY, False, _message_info())

    _, observed = poll_capture(cursor, ["api.example.com"])
    assert observed.matched_entries == [
        {
            "schema_version": 1,
            "scheme": "https",
            "host": "api.example.com",
            "port": 443,
            "method": "POST",
            "path": "/login",
            "status_code": 200,
            "request_body_length": len(REQUEST.partition(b"\r\n\r\n")[2]),
            "response_body_length": len(b'{"token":"synthetic-session"}'),
            "response_content_type": "JSON",
        }
    ]
