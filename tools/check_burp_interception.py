#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from mobile_playbook.orchestration.appium_process import tcp_reachable
from mobile_playbook.platforms.ios.burp_capture import capture_line_count, read_new_capture_entries
from mobile_playbook.platforms.ios.config import load_config
from mobile_playbook.platforms.ios.device_client import AppiumDeviceClient

DEFAULT_TEST_URL = "https://example.com"


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    config = load_config(Path(args.config), dry_run=False)
    burp = config.traffic_interception.get("burp") or {}
    proxy_url = args.proxy_url or burp.get("proxy_url")
    capture_path = Path(args.capture_path or burp.get("capture_path") or "work/ios/traffic_interception/capture.jsonl")
    test_host = urlparse(args.test_url).hostname or args.test_url

    if not proxy_url:
        print("FAILED: no burp.proxy_url configured (pass --proxy-url, or set traffic_interception.burp.proxy_url)", file=sys.stderr)
        return 1
    print(f"Checking Burp proxy reachability at {proxy_url}...")
    if not tcp_reachable(proxy_url, timeout=2):
        print(f"FAILED: Burp proxy not reachable at {proxy_url}. Start Burp Suite first.", file=sys.stderr)
        return 1
    print("Burp proxy is reachable.")

    start_line = capture_line_count(capture_path)
    print(f"Connecting to device and opening {args.test_url} in Safari...")
    client = AppiumDeviceClient(config.device).connect()
    try:
        client.open_url(args.test_url)
        print(f"Waiting up to {args.timeout:g}s for {test_host} to appear in {capture_path}...")
        deadline = time.monotonic() + args.timeout
        matched: list[dict] = []
        while time.monotonic() < deadline:
            matched = read_new_capture_entries(capture_path, start_line, [test_host.lower()])
            if matched:
                break
            time.sleep(1)
    finally:
        client.quit()

    if matched:
        print(f"PASS: {test_host} appeared in Burp's capture file — interception is working end-to-end.")
        return 0
    print(
        f"FAILED: {test_host} never appeared in {capture_path}. Check: is the device's Wi-Fi proxy pointed at "
        "Burp, is Burp's CA fully trusted (Settings > General > About > Certificate Trust Settings), and is "
        "tools/burp_traffic_capture_extension.py loaded in Burp?",
        file=sys.stderr,
    )
    return 1


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="One-time functional check that device proxy + CA trust + the Burp capture extension are all working together, before running ios-feature-02-risk-01 across a batch of apps."
    )
    parser.add_argument("--config", required=True, help="Path to configs/ios.yaml")
    parser.add_argument("--test-url", default=DEFAULT_TEST_URL)
    parser.add_argument("--proxy-url", default=None, help="Override traffic_interception.burp.proxy_url")
    parser.add_argument("--capture-path", default=None, help="Override traffic_interception.burp.capture_path")
    parser.add_argument("--timeout", type=float, default=20.0)
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
