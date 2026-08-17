from __future__ import annotations

import json
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class QueuedInput:
    id: int
    text: str
    created_at: str
    delivered_at: str | None = None


@dataclass
class ControlServerState:
    token: str
    paired: bool = False
    pair_payload: dict[str, Any] | None = None
    pair_timestamp: str | None = None
    next_id: int = 1
    queue: list[QueuedInput] = field(default_factory=list)
    delivered: list[QueuedInput] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    requests: list[dict[str, Any]] = field(default_factory=list)


class CommandControlServer:
    """Small local HTTP server used by feature5 custom-keyboard risks.

    The server deliberately keeps only the primitives needed by the test:
    pair, enqueue input, deliver the next queued input, and record app events.
    """

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8765,
        token: str | None = None,
        enqueue_requires_token: bool = False,
    ):
        self.host = host
        self.port = int(port)
        self.enqueue_requires_token = enqueue_requires_token
        self.state = ControlServerState(token=token or secrets.token_urlsafe(24))
        self._lock = threading.RLock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        if self._server is None:
            return f"http://{self.host}:{self.port}"
        host, port = self._server.server_address
        display_host = "127.0.0.1" if host in {"0.0.0.0", ""} else host
        return f"http://{display_host}:{port}"

    def start(self) -> "CommandControlServer":
        handler = self._make_handler()
        self._server = ThreadingHTTPServer((self.host, self.port), handler)
        self.port = int(self._server.server_address[1])
        self._thread = threading.Thread(target=self._server.serve_forever, name="feature5-keyboard-test-server", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def enqueue(self, text: str) -> QueuedInput:
        with self._lock:
            item = QueuedInput(id=self.state.next_id, text=text, created_at=_utc_now())
            self.state.next_id += 1
            self.state.queue.append(item)
            return item

    def next_input(self, token: str) -> QueuedInput | None:
        if token != self.state.token:
            return None
        with self._lock:
            if not self.state.queue:
                return None
            item = self.state.queue.pop(0)
            item.delivered_at = _utc_now()
            self.state.delivered.append(item)
            return item

    def wait_for_pair(self, timeout_seconds: float) -> bool:
        deadline = time.monotonic() + float(timeout_seconds)
        while time.monotonic() < deadline:
            with self._lock:
                if self.state.paired:
                    return True
            time.sleep(0.2)
        return False

    def wait_for_empty_queue(self, timeout_seconds: float) -> bool:
        deadline = time.monotonic() + float(timeout_seconds)
        while time.monotonic() < deadline:
            with self._lock:
                if not self.state.queue:
                    return True
            time.sleep(0.2)
        return False

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            next_requests = [item for item in self.state.requests if item.get("path") == "/next"]
            return {
                "base_url": self.base_url,
                "paired": self.state.paired,
                "pair_payload": self.state.pair_payload,
                "pair_timestamp": self.state.pair_timestamp,
                "queued_count": len(self.state.queue),
                "delivered_count": len(self.state.delivered),
                "events_count": len(self.state.events),
                "next_request_count": len(next_requests),
                "unauthorized_next_count": sum(1 for item in next_requests if item.get("status") == 401),
                "queue": [item.__dict__.copy() for item in self.state.queue],
                "delivered": [item.__dict__.copy() for item in self.state.delivered],
                "events": list(self.state.events),
                "errors": list(self.state.errors),
                "requests": list(self.state.requests[-100:]),
            }

    def queue_snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "queued_count": len(self.state.queue),
                "delivered_count": len(self.state.delivered),
                "queue": [item.__dict__.copy() for item in self.state.queue],
                "delivered": [item.__dict__.copy() for item in self.state.delivered],
            }

    def _record_request(self, path: str, method: str, status: int, metadata: dict[str, Any] | None = None) -> None:
        with self._lock:
            self.state.requests.append(
                {
                    "timestamp": _utc_now(),
                    "method": method,
                    "path": path,
                    "status": status,
                    **(metadata or {}),
                }
            )
            if len(self.state.requests) > 500:
                self.state.requests = self.state.requests[-500:]

    def _make_handler(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "Feature5KeyboardTest/0.1"

            def do_GET(self) -> None:
                parsed = urlparse(self.path)
                if parsed.path == "/health":
                    outer._record_request(parsed.path, "GET", 200)
                    self._send_json(200, {"status": "ok"})
                    return
                if parsed.path == "/next":
                    token = self.headers.get("X-Control-Token") or parse_qs(parsed.query).get("token", [None])[0]
                    if token != outer.state.token:
                        outer._record_request(parsed.path, "GET", 401, {"token_present": bool(token), "token_valid": False})
                        self._send_json(401, {"error": "unauthorized"})
                        return
                    item = outer.next_input(token)
                    if item is None:
                        outer._record_request(parsed.path, "GET", 200, {"token_present": True, "token_valid": True, "delivered_id": None})
                        self._send_json(200, {"id": None, "text": None})
                        return
                    outer._record_request(parsed.path, "GET", 200, {"token_present": True, "token_valid": True, "delivered_id": item.id})
                    self._send_json(200, {"id": item.id, "text": item.text})
                    return
                if parsed.path == "/events":
                    outer._record_request(parsed.path, "GET", 200)
                    self._send_json(200, {"events": outer.snapshot()["events"]})
                    return
                if parsed.path == "/queue":
                    outer._record_request(parsed.path, "GET", 200)
                    self._send_json(200, outer.queue_snapshot())
                    return
                if parsed.path == "/snapshot":
                    outer._record_request(parsed.path, "GET", 200)
                    self._send_json(200, outer.snapshot())
                    return
                outer._record_request(parsed.path, "GET", 404)
                self._send_json(404, {"error": "not_found"})

            def do_POST(self) -> None:
                parsed = urlparse(self.path)
                if parsed.path == "/pair":
                    payload = self._read_json()
                    with outer._lock:
                        outer.state.paired = True
                        outer.state.pair_payload = payload
                        outer.state.pair_timestamp = _utc_now()
                    outer._record_request(parsed.path, "POST", 200)
                    self._send_json(200, {"token": outer.state.token})
                    return
                if parsed.path == "/enqueue":
                    if outer.enqueue_requires_token and not self._has_valid_token(parsed):
                        outer._record_request(parsed.path, "POST", 401)
                        self._send_json(401, {"error": "unauthorized"})
                        return
                    payload = self._read_json()
                    text = payload.get("text")
                    if not isinstance(text, str):
                        outer._record_request(parsed.path, "POST", 400)
                        self._send_json(400, {"error": "text must be a string"})
                        return
                    item = outer.enqueue(text)
                    outer._record_request(parsed.path, "POST", 200, {"queued_id": item.id})
                    self._send_json(200, {"id": item.id, "queued": True})
                    return
                if parsed.path == "/events":
                    if not self._has_valid_token(parsed):
                        outer._record_request(parsed.path, "POST", 401)
                        self._send_json(401, {"error": "unauthorized"})
                        return
                    payload = self._read_json()
                    with outer._lock:
                        outer.state.events.append({"timestamp": _utc_now(), "payload": payload})
                    outer._record_request(parsed.path, "POST", 200)
                    self._send_json(200, {"recorded": True})
                    return
                outer._record_request(parsed.path, "POST", 404)
                self._send_json(404, {"error": "not_found"})

            def log_message(self, format: str, *args: Any) -> None:
                return

            def _has_valid_token(self, parsed) -> bool:
                header = self.headers.get("X-Control-Token")
                query = parse_qs(parsed.query).get("token", [None])[0]
                return (header or query) == outer.state.token

            def _read_json(self) -> dict[str, Any]:
                length = int(self.headers.get("Content-Length", "0") or "0")
                if length <= 0:
                    return {}
                raw = self.rfile.read(length)
                try:
                    value = json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError:
                    return {}
                return value if isinstance(value, dict) else {}

            def _send_json(self, status: int, payload: dict[str, Any]) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return Handler
