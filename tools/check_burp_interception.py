#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from mobile_playbook.orchestration.appium_process import tcp_reachable
from mobile_playbook.platforms.ios.burp_capture import CaptureObservation, poll_capture, snapshot_capture
from mobile_playbook.platforms.ios.config import load_config
from mobile_playbook.platforms.ios.device_client import AppiumDeviceClient
from mobile_playbook.platforms.ios.burp_health import write_health_record
from mobile_playbook.platforms.ios.traffic_interception_setup import (
    TrafficInterceptionSetupError,
    prepare_traffic_interception,
    restore_traffic_interception,
)
from mobile_playbook.storage import ios_capture_path, resolve_under_repository

DEFAULT_TEST_URL = "https://example.com"


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    config = load_config(Path(args.config), dry_run=False)
    burp = config.traffic_interception.get("burp") or {}
    proxy_url = args.proxy_url or burp.get("proxy_url")
    configured_capture_path = args.capture_path or burp.get("capture_path")
    capture_path = resolve_under_repository(configured_capture_path) if configured_capture_path else ios_capture_path()
    test_host = urlparse(args.test_url).hostname or args.test_url

    if not proxy_url:
        print("FAILED: no burp.proxy_url configured (pass --proxy-url, or set traffic_interception.burp.proxy_url)", file=sys.stderr)
        return 1
    print(f"Checking Burp proxy reachability at {proxy_url}...")
    if not tcp_reachable(proxy_url, timeout=2):
        print(f"FAILED: Burp proxy not reachable at {proxy_url}. Start Burp Suite first.", file=sys.stderr)
        return 1
    print("Burp proxy is reachable.")

    device_setup = config.traffic_interception.get("device_setup") or {}
    print("Connecting to device and preparing traffic interception...")
    client = AppiumDeviceClient(config.device).connect()
    setup_state = None
    aggregate = CaptureObservation(matched_entries=[])
    failure = None
    try:
        try:
            setup_state = prepare_traffic_interception(client, device_setup, capture_path.parent)
        except TrafficInterceptionSetupError as exc:
            setup_state = exc.state
            failure = f"{exc.status}: {exc}"

        if failure is None:
            cursor = snapshot_capture(capture_path)
            state = f"{cursor.initial_size} existing byte(s)" if cursor.existed else "no such file yet"
            print(f"Watching Burp's capture file at {capture_path} ({state}).")
            print(f"Opening {args.test_url} in Safari...")
            client.open_url(args.test_url)
            print(f"Waiting up to {args.timeout:g}s for {test_host} to appear in {capture_path}...")
            deadline = time.monotonic() + args.timeout
            while time.monotonic() < deadline:
                cursor, observed = poll_capture(cursor, [test_host])
                aggregate.matched_entries.extend(observed.matched_entries)
                aggregate.valid_entry_count += observed.valid_entry_count
                aggregate.malformed_entry_count += observed.malformed_entry_count
                aggregate.unmatched_entry_count += observed.unmatched_entry_count
                aggregate.source_changed |= observed.source_changed
                aggregate.source_unavailable |= observed.source_unavailable
                if aggregate.matched_entries or aggregate.source_changed or aggregate.source_unavailable:
                    break
                time.sleep(min(1, max(0, deadline - time.monotonic())))
    except Exception as exc:
        failure = str(exc)
    finally:
        if setup_state is not None:
            try:
                restore_traffic_interception(client, setup_state, capture_path.parent)
            except TrafficInterceptionSetupError as exc:
                failure = f"{exc.status}: {exc}"
        client.quit()

    if failure is not None:
        print(f"FAILED: {failure}", file=sys.stderr)
        return 1

    if aggregate.matched_entries:
        record_path = write_health_record(
            capture_path=capture_path,
            proxy_url=proxy_url,
            canary_host=test_host,
            valid_entry_count=aggregate.valid_entry_count,
            device_udid=config.device.udid,
        )
        print(f"PASS: {test_host} appeared in Burp's capture file — interception is working end-to-end.")
        print(f"Health record: {record_path}")
        return 0
    detail = (
        f" New records: {aggregate.valid_entry_count} valid, "
        f"{aggregate.malformed_entry_count} malformed, {aggregate.unmatched_entry_count} unmatched."
    )
    if aggregate.source_changed:
        detail += " The capture file was replaced or truncated."
    if aggregate.source_unavailable:
        detail += " The capture file became unavailable."
    print(
        f"FAILED: {test_host} never appeared in {capture_path}. Check: is the device's Wi-Fi proxy pointed at "
        "Burp, is Burp's CA fully trusted (Settings > General > About > Certificate Trust Settings), and is "
        f"tools/burp_traffic_capture_extension.py loaded in Burp?{detail}",
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
