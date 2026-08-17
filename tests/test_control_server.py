from mobile_playbook.platforms.ios.control_server import CommandControlServer


def test_control_server_pair_enqueue_next_preserves_return_as_separate_item():
    server = CommandControlServer(host="127.0.0.1", port=0, token="test-token")
    server.enqueue("hello123")
    server.enqueue("\n")

    first = server.next_input("test-token")
    second = server.next_input("test-token")

    assert first is not None
    assert second is not None
    assert first.text == "hello123"
    assert second.text == "\n"
    assert server.snapshot()["delivered_count"] == 2
    assert server.queue_snapshot()["queued_count"] == 0
    assert server.queue_snapshot()["delivered"][1]["text"] == "\n"


def test_control_server_records_next_request_diagnostics():
    server = CommandControlServer(host="127.0.0.1", port=0, token="test-token")
    server._record_request("/next", "GET", 401, {"token_present": True, "token_valid": False})
    server._record_request("/next", "GET", 200, {"token_present": True, "token_valid": True, "delivered_id": None})

    snapshot = server.snapshot()

    assert snapshot["next_request_count"] == 2
    assert snapshot["unauthorized_next_count"] == 1
    assert snapshot["requests"][0]["path"] == "/next"
