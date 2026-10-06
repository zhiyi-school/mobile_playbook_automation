"""
Calls a FastAPI app through its ASGI interface so tests cover the full HTTP boundary without a server.
"""

from __future__ import annotations

import json

from fastapi import FastAPI


# Send one JSON request to the app and return the response status and decoded body.
async def call(app: FastAPI, method: str, path: str, body: dict | None = None):
    sent: list[dict] = []
    payload = json.dumps(body).encode() if body is not None else b""
    received = False

    async def receive():
        nonlocal received
        if received:
            return {"type": "http.disconnect"}
        received = True
        return {"type": "http.request", "body": payload, "more_body": False}

    async def send(message):
        sent.append(message)

    headers = [(b"content-type", b"application/json")] if body is not None else []
    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": headers,
            "client": ("test", 1),
            "server": ("testserver", 80),
            "root_path": "",
        },
        receive,
        send,
    )
    start = next(message for message in sent if message["type"] == "http.response.start")
    raw = b"".join(message.get("body", b"") for message in sent if message["type"] == "http.response.body")
    return start["status"], json.loads(raw) if raw else None
