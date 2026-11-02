# -*- coding: utf-8 -*-
"""
Burp/Jython capture extension for the traffic interception risks: proxy metadata for every exchange, and full content only
while a risk arms it; see docs/burp-capture.md.
"""

from burp import IBurpExtender
from burp import IHttpListener
from burp import IProxyListener

import json
import os
import threading
import time

CAPTURE_PATH_ENV = "MPA_BURP_CAPTURE_PATH"
DEFAULT_RELATIVE_CAPTURE_PATH = os.path.join("artifacts", "work", "ios", "traffic_interception", "capture.jsonl")
CONTENT_DIR_ENV = "MPA_BURP_CONTENT_DIR"
DEFAULT_RELATIVE_CONTENT_DIR = os.path.join("artifacts", "work", "traffic_interception")
CONTENT_FILE_NAME = "content.jsonl"
MARKER_FILE_NAME = "content.armed"
DEFAULT_MAX_BODY_BYTES = 65536

_write_lock = threading.Lock()


# Return the repository root above the loaded extension file, or None when it cannot be located.
def repository_root(callbacks):
    try:
        extension_file = callbacks.getExtensionFilename()
    except Exception:
        extension_file = None

    if not extension_file:
        extension_file = globals().get("__file__")

    if not extension_file:
        return None

    # The extension lives at tools/burp/, two levels below the repository root.
    return os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(extension_file)))
    )


# Return the capture path from MPA_BURP_CAPTURE_PATH or the default, relative paths under the repository root.
def resolve_capture_path(callbacks):
    configured = (os.environ.get(CAPTURE_PATH_ENV) or "").strip()

    if configured:
        configured = os.path.expanduser(configured)
        if os.path.isabs(configured):
            return configured

    root = repository_root(callbacks)
    if not root:
        return None

    if configured:
        return os.path.join(root, configured)

    return os.path.join(root, DEFAULT_RELATIVE_CAPTURE_PATH)


# Return the content capture directory from MPA_BURP_CONTENT_DIR or the default, relative paths under the repository root.
def resolve_content_dir(callbacks):
    configured = os.path.expanduser((os.environ.get(CONTENT_DIR_ENV) or "").strip())
    if configured and os.path.isabs(configured):
        return configured
    root = repository_root(callbacks)
    if not root:
        return None
    return os.path.join(root, configured or DEFAULT_RELATIVE_CONTENT_DIR)


# Return the arming marker's listener ports and body limit while it is present and unexpired, else None.
def read_marker(marker_path, now):
    try:
        handle = open(marker_path, "r")
    except (IOError, OSError):
        return None
    try:
        marker = json.loads(handle.read())
    finally:
        handle.close()
    if not isinstance(marker, dict) or marker.get("schema_version") != 1:
        return None
    if float(marker.get("expires_at") or 0) <= now:
        return None
    ports = [int(port) for port in (marker.get("listener_ports") or [])]
    # Without ports the window would record every listener's traffic, including other platforms' risks.
    if not ports:
        return None
    return {"listener_ports": ports, "max_body_bytes": int(marker.get("max_body_bytes") or DEFAULT_MAX_BODY_BYTES)}


# Return the port of a listener interface such as "0.0.0.0:8081", or None when it has none.
def listener_port(listener_interface):
    try:
        return int(str(listener_interface).rsplit(":", 1)[1])
    except (IndexError, ValueError):
        return None


# Append one JSON line to a file, creating its directory, under the write lock.
def append_line(path, entry):
    line = json.dumps(entry)
    with _write_lock:
        directory = os.path.dirname(path)
        if directory and not os.path.isdir(directory):
            os.makedirs(directory)
        handle = open(path, "a")
        try:
            handle.write(line + "\n")
        finally:
            handle.close()


class BurpExtender(IBurpExtender, IHttpListener, IProxyListener):
    """Burp extension that records proxy responses as JSON lines, with their content while a risk arms it."""

    # Registers the proxy listener, or reports an error when the capture path cannot be resolved.
    def registerExtenderCallbacks(self, callbacks):
        self._callbacks = callbacks
        self._helpers = callbacks.getHelpers()
        callbacks.setExtensionName("mobile_playbook_automation traffic capture")
        self._capture_path = resolve_capture_path(callbacks)
        if not self._capture_path:
            callbacks.printError(
                "Traffic capture extension is NOT recording: this extension could not locate the repository it "
                "was loaded from. Set %s to the absolute traffic_interception.burp.capture_path and reload the "
                "extension." % CAPTURE_PATH_ENV
            )
            return
        callbacks.registerHttpListener(self)
        callbacks.printOutput("Traffic capture extension loaded. Writing to: " + self._capture_path)
        self._content_dir = resolve_content_dir(callbacks)
        if not self._content_dir:
            callbacks.printError(
                "Content capture is NOT available: set %s to an absolute directory and reload the extension." % CONTENT_DIR_ENV
            )
            return
        callbacks.registerProxyListener(self)
        callbacks.printOutput("Content capture is armed by: " + os.path.join(self._content_dir, MARKER_FILE_NAME))

    # Record each proxy response, reporting failures to Burp's error log.
    def processHttpMessage(self, toolFlag, messageIsRequest, messageInfo):
        if messageIsRequest:
            return
        if toolFlag != self._callbacks.TOOL_PROXY:
            return
        try:
            self._record(messageInfo)
        except Exception as exc:
            self._callbacks.printError("traffic capture failed: %s" % exc)

    # Record a proxied response's full content while the marker arms its listener, reporting failures to Burp's error log.
    def processProxyMessage(self, messageIsRequest, message):
        if messageIsRequest:
            return
        try:
            marker = read_marker(os.path.join(self._content_dir, MARKER_FILE_NAME), time.time())
            if marker is None or listener_port(message.getListenerInterface()) not in marker["listener_ports"]:
                return
            self._record_content(message, marker["max_body_bytes"])
        except Exception as exc:
            self._callbacks.printError("content capture failed: %s" % exc)

    # Append one exchange's URL, headers and bodies, each body base64 encoded and cut at max_body_bytes, to the content file.
    def _record_content(self, message, max_body_bytes):
        messageInfo = message.getMessageInfo()
        request_bytes = messageInfo.getRequest()
        request_info = self._helpers.analyzeRequest(messageInfo)
        url = request_info.getUrl()
        entry = {
            "schema_version": 1,
            "captured_at": time.time(),
            "listener": str(message.getListenerInterface()),
            "client_ip": str(message.getClientIpAddress()),
            "scheme": str(url.getProtocol()).lower(),
            "host": url.getHost(),
            "port": url.getPort(),
            "method": request_info.getMethod(),
            "url": str(url),
            "request_headers": [str(header) for header in request_info.getHeaders()],
            "status_code": None,
            "response_headers": [],
        }
        entry.update(self._body("request", request_bytes, request_info.getBodyOffset(), max_body_bytes))
        response_bytes = messageInfo.getResponse()
        if response_bytes is not None:
            response_info = self._helpers.analyzeResponse(response_bytes)
            entry["status_code"] = response_info.getStatusCode()
            entry["response_headers"] = [str(header) for header in response_info.getHeaders()]
            entry.update(self._body("response", response_bytes, response_info.getBodyOffset(), max_body_bytes))
        else:
            entry.update(self._body("response", None, 0, max_body_bytes))
        append_line(os.path.join(self._content_dir, CONTENT_FILE_NAME), entry)

    # Return a message body's base64 text, length and whether it was cut, under keys named for the prefix.
    def _body(self, prefix, message_bytes, body_offset, max_body_bytes):
        if message_bytes is None:
            body = []
        else:
            body = message_bytes[body_offset:]
        return {
            prefix + "_body_base64": self._helpers.base64Encode(body[:max_body_bytes]) if len(body) else "",
            prefix + "_body_length": len(body),
            prefix + "_body_truncated": len(body) > max_body_bytes,
        }

    # Append one message's scheme, host, method, path, status and body sizes to the capture file.
    def _record(self, messageInfo):
        request_info = self._helpers.analyzeRequest(messageInfo)
        url = request_info.getUrl()
        request_bytes = messageInfo.getRequest()
        request_body_offset = request_info.getBodyOffset()
        status_code = None
        response_bytes = messageInfo.getResponse()
        if response_bytes is not None:
            response_info = self._helpers.analyzeResponse(response_bytes)
            status_code = response_info.getStatusCode()
        entry = {
            "schema_version": 1,
            "scheme": str(url.getProtocol()).lower(),
            "host": url.getHost(),
            "port": url.getPort(),
            "method": request_info.getMethod(),
            "path": url.getPath(),
            "status_code": status_code,
            "request_body_length": max(0, len(request_bytes) - request_body_offset),
            "response_body_length": (
                max(0, len(response_bytes) - response_info.getBodyOffset())
                if response_bytes is not None
                else 0
            ),
            "response_content_type": (
                response_info.getStatedMimeType()
                if response_bytes is not None
                else ""
            ),
        }
        append_line(self._capture_path, entry)
