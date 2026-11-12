"""
Calls a FastAPI app through its ASGI interface so tests cover the full HTTP boundary without a server.
"""

from __future__ import annotations

import asyncio
import json

from fastapi import FastAPI


# Send one request to the app and return the response status, headers and raw body.
async def request(
    app: FastAPI,
    method: str,
    path: str,
    query: str = "",
    body: bytes = b"",
    headers: list[tuple[bytes, bytes]] | None = None,
):
    sent: list[dict] = []
    received = False
    response_complete = asyncio.Event()

    # Like a connected client, report a disconnect only after the full response,
    # because Starlette cancels a streamed body when it sees one.
    async def receive():
        nonlocal received
        if not received:
            received = True
            return {"type": "http.request", "body": body, "more_body": False}
        await response_complete.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)
        if message["type"] == "http.response.body" and not message.get("more_body", False):
            response_complete.set()

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": query.encode(),
            "headers": headers or [],
            "client": ("test", 1),
            "server": ("testserver", 80),
            "root_path": "",
        },
        receive,
        send,
    )
    start = next(message for message in sent if message["type"] == "http.response.start")
    raw = b"".join(message.get("body", b"") for message in sent if message["type"] == "http.response.body")
    response_headers = {key.decode(): value.decode() for key, value in start["headers"]}
    return start["status"], response_headers, raw


# Send one JSON request to the app and return the response status and decoded body.
async def call(app: FastAPI, method: str, path: str, body: dict | None = None):
    path, _, query = path.partition("?")
    payload = json.dumps(body).encode() if body is not None else b""
    headers = [(b"content-type", b"application/json")] if body is not None else []
    status, _, raw = await request(app, method, path, query, payload, headers)
    return status, json.loads(raw) if raw else None
