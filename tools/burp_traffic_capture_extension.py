# -*- coding: utf-8 -*-
"""Burp/Jython capture extension for ios-feature-02-risk-01.

See docs/ios/risks.md#capturing-burps-traffic-into-capturejsonl.
"""

from burp import IBurpExtender
from burp import IHttpListener

import json
import os
import threading

CAPTURE_PATH_ENV = "MPA_BURP_CAPTURE_PATH"
DEFAULT_RELATIVE_CAPTURE_PATH = os.path.join("artifacts", "work", "ios", "traffic_interception", "capture.jsonl")

_write_lock = threading.Lock()


def repository_root(callbacks):
    try:
        extension_file = callbacks.getExtensionFilename()
    except Exception:
        extension_file = None

    if not extension_file:
        extension_file = globals().get("__file__")

    if not extension_file:
        return None

    return os.path.dirname(
        os.path.dirname(os.path.abspath(extension_file))
    )


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


class BurpExtender(IBurpExtender, IHttpListener):
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

    def processHttpMessage(self, toolFlag, messageIsRequest, messageInfo):
        if messageIsRequest:
            return
        if toolFlag != self._callbacks.TOOL_PROXY:
            return
        try:
            self._record(messageInfo)
        except Exception as exc:
            self._callbacks.printError("traffic capture failed: %s" % exc)

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
        line = json.dumps(entry)
        with _write_lock:
            directory = os.path.dirname(self._capture_path)
            if directory and not os.path.isdir(directory):
                os.makedirs(directory)
            handle = open(self._capture_path, "a")
            try:
                handle.write(line + "\n")
            finally:
                handle.close()
