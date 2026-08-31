# -*- coding: utf-8 -*-
"""Burp/Jython capture extension for ios-feature-02-risk-01.

See docs/ios/risks.md#capturing-burps-traffic-into-capturejsonl.
"""

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
