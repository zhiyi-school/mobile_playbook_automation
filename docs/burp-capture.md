# Burp Traffic Capture

The traffic interception risks, [`ios-feature-02-risk-01`](ios/risks.md#ios-feature-02-risk-01) and [`android-feature-03-risk-01`](android/risks.md#android-feature-03-risk-01), route a device through Burp Suite and judge what Burp read. They don't talk to Burp's own APIs, since Burp has no simple built-in "give me proxy history" endpoint. Instead, a small companion Burp extension, [tools/burp/traffic_capture_extension.py](../tools/burp/traffic_capture_extension.py), writes what passes through Burp's proxy to plain append-only files that the risks read. This mirrors how the framework exposes evidence to itself elsewhere (`events.jsonl`, `appium.log`): a file both sides agree on, rather than a live API integration.

The extension writes two kinds of record:

- **Metadata**, for every proxied exchange, to the capture file. Both platforms read it: the iOS risk judges it, and the Android risk checks its setup with it.
- **Content**, the exchange in full, only while a risk arms it. Only the Android risk arms it.

## Loading the extension

The extension is a Jython Burp extension with no dependencies. One-time setup:

1. Use the standalone Jython JAR vendored at `tools/vendor/jython/jython-standalone-2.7.4.jar`, or download one from `https://www.jython.org/download`.
2. In Burp Suite, open Extender > Options > Python Environment and point it at that JAR.
3. Open Extender > Extensions > Add, choose extension type `Python`, and select `tools/burp/traffic_capture_extension.py`.
4. Check the Extender output. It prints the capture file it writes to and the marker that arms content capture. If it cannot locate the repository it was loaded from, it records nothing and says so.

Reload the extension after updating it, so Burp runs the new version.

## Listeners

The risks proxy through Burp listeners that must be bound to all interfaces, so the device can reach them. iOS uses `traffic_interception.burp.proxy_url` and its `device_setup.proxy_port`, and Android uses `tools.burp_proxy` or `traffic_interception.proxy_port`. Both examples use port 8081, as playbook feature-03 does, so by default the two platforms share one listener.

The capture file holds every listener's traffic. Content capture records only the listener ports the arming risk names, but it cannot tell devices apart on one listener: while the Android risk has it armed, an iPhone proxied through the same listener has its exchanges recorded in full too. They are deleted with the rest of the content file after the scan, and the Android report keeps only the app's own exchanges. To keep iOS traffic out of content capture altogether, give iOS a listener of its own, such as 8082, and point its `proxy_url` and `proxy_port` at it.

## The capture file

For each completed exchange, the extension appends one metadata-only JSON object to the capture file:

```json
{"schema_version": 1, "scheme": "https", "host": "api.example.com", "port": 443, "method": "GET", "path": "/profile", "status_code": 200, "request_body_length": 0, "response_body_length": 1234, "response_content_type": "JSON"}
```

It holds no headers, cookies, authorization values or bodies. `platforms/traffic_interception/burp_capture.py` reads it: a risk snapshots the file's size and identity before its test window, then reads only the lines appended since, and reports a replaced, truncated or unreadable file instead of reading from it.

The extension resolves the file on its own, as `artifacts/work/ios/traffic_interception/capture.jsonl` under the repository it was loaded from, so nothing in the extension needs editing. It does not read the backend's storage settings, so set `MPA_BURP_CAPTURE_PATH` to an absolute path when Burp runs somewhere that path does not exist, or when `WORK_DIR` puts the capture file elsewhere. The risks must then read the same file: `traffic_interception.burp.capture_path` on iOS, `traffic_interception.capture_path` on Android. On iOS, [scripts/check_burp_interception.py](ios/risks.md#checking-the-whole-chain-before-a-batch-of-runs) prints the path it reads, so the two can be compared.

## Content capture

The extension can also record each exchange in full: its URL, request and response headers, and request and response bodies, each body base64 encoded and cut at a size limit. It does this only while a risk arms it, and only for the Burp listener ports the risk names.

A risk arms it with `arm_content_capture()` in `platforms/traffic_interception/burp_content.py`, which writes the marker `content.armed` beside the content file `content.jsonl`, with the listener ports, the body limit and an expiry. The extension checks the marker on every proxied response and stops recording when the marker is removed or expires, so a run that dies without disarming does not leave content capture on. Arming deletes any earlier `content.jsonl`, and refuses while another unexpired window is armed. `read_content()` reads the exchanges back, keeping the armed listeners' traffic and decoding gzip and deflate response bodies.

Both files live in `artifacts/work/traffic_interception/` under the repository the extension was loaded from. Set `MPA_BURP_CONTENT_DIR` to an absolute directory when Burp runs somewhere that path does not exist, or when `WORK_DIR` moves the work folder; the risk then has to use the same directory, through `traffic_interception.content_dir` on Android. The content file holds raw request bodies such as passwords and tokens, so the risk that reads it deletes it once it has been scanned. Work retention never prunes this folder.

## Status

The extension's behavior is tested against a fake Burp API (`tests/repository/test_burp_traffic_capture_extension.py`) and it compiles under the vendored Jython, but it hasn't been exercised against a real Burp Suite instance. Treat it as a starting point to verify, not a guaranteed-working drop-in.
