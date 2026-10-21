from __future__ import annotations

import base64
import gzip
import json
import time

import pytest

from mobile_playbook.platforms.traffic_interception.burp_content import (
    ContentCaptureBusy,
    arm_content_capture,
    disarm_content_capture,
    discard_content,
    read_content,
)


def _record(**overrides) -> dict:
    entry = {
        "schema_version": 1,
        "listener": "0.0.0.0:8081",
        "client_ip": "192.0.2.10",
        "scheme": "https",
        "host": "api.example.com",
        "port": 443,
        "method": "POST",
        "url": "https://api.example.com:443/login",
        "request_headers": ["POST /login HTTP/1.1", "Host: api.example.com"],
        "request_body_base64": base64.b64encode(b'{"password":"Canary-Pw-7Q4z"}').decode(),
        "request_body_length": 29,
        "request_body_truncated": False,
        "status_code": 200,
        "response_headers": ["HTTP/1.1 200 OK"],
        "response_body_base64": base64.b64encode(b'{"ok":true}').decode(),
        "response_body_length": 11,
        "response_body_truncated": False,
    }
    entry.update(overrides)
    return entry


def _write(window, *entries, tail: bytes = b"") -> None:
    window.content_path.write_bytes(b"".join(json.dumps(entry).encode() + b"\n" for entry in entries) + tail)


def test_arm_publishes_marker_and_discards_earlier_content(tmp_path):
    (tmp_path / "content.jsonl").write_text("left over from an earlier run\n")

    window = arm_content_capture(tmp_path, [8081, 8081], max_body_bytes=1024, ttl_seconds=60)

    marker = json.loads(window.marker_path.read_text())
    assert marker["schema_version"] == 1
    assert marker["listener_ports"] == [8081]
    assert marker["max_body_bytes"] == 1024
    assert marker["expires_at"] > time.time()
    assert not window.content_path.exists()
    assert [path.name for path in tmp_path.iterdir()] == ["content.armed"]


def test_arm_refuses_a_live_window_and_keeps_its_content(tmp_path):
    first = arm_content_capture(tmp_path, [8081])
    _write(first, _record())

    with pytest.raises(ContentCaptureBusy):
        arm_content_capture(tmp_path, [8082])

    assert json.loads(first.marker_path.read_text())["listener_ports"] == [8081]
    assert len(read_content(first).exchanges) == 1


def test_arm_replaces_an_expired_marker(tmp_path):
    (tmp_path / "content.armed").write_text(json.dumps({"schema_version": 1, "listener_ports": [8080], "expires_at": time.time() - 1}))

    window = arm_content_capture(tmp_path, [8081])

    assert json.loads(window.marker_path.read_text())["listener_ports"] == [8081]


def test_arm_needs_a_listener_port(tmp_path):
    with pytest.raises(ValueError):
        arm_content_capture(tmp_path, [])
    assert not (tmp_path / "content.armed").exists()


def test_disarm_and_discard_remove_marker_and_content(tmp_path):
    window = arm_content_capture(tmp_path, [8081])
    _write(window, _record())

    disarm_content_capture(window)
    discard_content(window)

    assert list(tmp_path.iterdir()) == []


def test_read_keeps_the_window_listener_and_counts_the_rest(tmp_path):
    window = arm_content_capture(tmp_path, [8081])
    _write(
        window,
        _record(),
        _record(listener="127.0.0.1:8080"),
        _record(schema_version=2),
        _record(request_body_base64="not base64!"),
        tail=b'{"schema_version": 1',
    )

    read = read_content(window)

    assert [exchange.host for exchange in read.exchanges] == ["api.example.com"]
    exchange = read.exchanges[0]
    assert exchange.request_body == b'{"password":"Canary-Pw-7Q4z"}'
    assert exchange.response_body == b'{"ok":true}'
    assert exchange.response_body_encoding == "identity"
    assert exchange.status_code == 200
    assert read.other_listener_count == 1
    assert read.malformed_count == 2
    assert read.trailing_partial_line is True


def test_read_without_a_content_file_is_empty(tmp_path):
    read = read_content(arm_content_capture(tmp_path, [8081]))

    assert read.exchanges == []
    assert read.malformed_count == 0


def test_gzip_response_is_decoded_even_when_cut(tmp_path):
    body = json.dumps({"items": [index * 7919 % 10007 for index in range(2000)]}).encode()
    compressed = gzip.compress(body)
    window = arm_content_capture(tmp_path, [8081])
    headers = ["HTTP/1.1 200 OK", "Content-Encoding: gzip"]
    _write(
        window,
        _record(response_headers=headers, response_body_base64=base64.b64encode(compressed).decode()),
        _record(response_headers=headers, response_body_base64=base64.b64encode(compressed[: len(compressed) // 2]).decode(), response_body_truncated=True),
    )

    full, cut = read_content(window).exchanges

    assert full.response_body == body
    assert full.response_body_encoding == "gzip"
    assert cut.response_body_encoding == "gzip"
    assert body.startswith(cut.response_body) and cut.response_body


def test_unsupported_encoding_keeps_the_recorded_body(tmp_path):
    window = arm_content_capture(tmp_path, [8081])
    _write(window, _record(response_headers=["HTTP/1.1 200 OK", "Content-Encoding: br"], response_body_base64=base64.b64encode(b"\x8b\x00").decode()))

    exchange = read_content(window).exchanges[0]

    assert exchange.response_body == b"\x8b\x00"
    assert exchange.response_body_encoding == "undecoded:br"
