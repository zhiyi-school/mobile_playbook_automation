# -*- coding: utf-8 -*-
# Burp Suite extension (Jython) for ios-feature-02-risk-01 (TLS traffic
# interception exposure). This is NOT a regular Python 3 script — it runs
# inside Burp Suite itself via Burp's Extender API, which requires a Jython
# interpreter, not the mobile_playbook_automation virtualenv.
#
# One-time setup:
#   1. Download a standalone Jython JAR (https://www.jython.org/download).
#   2. Burp Suite > Extender > Options > Python Environment > point at that JAR.
#   3. Burp Suite > Extender > Extensions > Add > Extension type: Python >
#      Extension file: this file.
#   4. Update CAPTURE_PATH below to match traffic_interception.burp.capture_path
#      in your mobile_playbook_automation config (an absolute path is safest,
#      since Burp's working directory isn't this repo's).
#
# What it does: for every HTTP message Burp's proxy decrypts — i.e. traffic
# from a device already configured to route through this Burp instance with
# Burp's CA trusted — appends one JSON line to CAPTURE_PATH with the
# request's host/method/path and the response status code. This is the same
# file ios-feature-02-risk-01 reads back to decide its verdict; see
# docs/ios/risks.md#ios-feature-02-risk-01.

from burp import IBurpExtender
from burp import IHttpListener

import json
import os
import threading

CAPTURE_PATH = "/Users/user/playbook/mobile_playbook_automation/work/ios/traffic_interception/capture.jsonl"

_write_lock = threading.Lock()


class BurpExtender(IBurpExtender, IHttpListener):
    def registerExtenderCallbacks(self, callbacks):
        self._callbacks = callbacks
        self._helpers = callbacks.getHelpers()
        callbacks.setExtensionName("mobile_playbook_automation traffic capture")
        callbacks.registerHttpListener(self)
        callbacks.printOutput("Traffic capture extension loaded. Writing to: " + CAPTURE_PATH)

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
        status_code = None
        response_bytes = messageInfo.getResponse()
        if response_bytes is not None:
            response_info = self._helpers.analyzeResponse(response_bytes)
            status_code = response_info.getStatusCode()
        entry = {
            "host": url.getHost(),
            "port": url.getPort(),
            "method": request_info.getMethod(),
            "path": url.getPath(),
            "status_code": status_code,
        }
        line = json.dumps(entry)
        with _write_lock:
            directory = os.path.dirname(CAPTURE_PATH)
            if directory and not os.path.isdir(directory):
                os.makedirs(directory)
            handle = open(CAPTURE_PATH, "a")
            try:
                handle.write(line + "\n")
            finally:
                handle.close()
