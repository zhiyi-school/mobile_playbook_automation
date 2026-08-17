#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import socket
import sys
import time
from typing import Any

from mobile_playbook.platforms.ios.control_server import CommandControlServer


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    server = CommandControlServer(
        host=args.host,
        port=args.port,
        token=None if args.token == "auto" else args.token,
        enqueue_requires_token=args.enqueue_requires_token,
    ).start()
    try:
        local_url = server.base_url
        lan_host = args.advertised_host or _detect_lan_ip()
        phone_url = f"http://{lan_host}:{server.port}" if lan_host else local_url
        print("LocalKeyboard manual test server")
        print(f"Mac/local URL: {local_url}")
        print(f"Phone/server URL to enter in LocalKeyboard: {phone_url}")
        print(f"Token: {server.state.token}")
        print("")
        print("Useful curl commands:")
        print(f"  curl -X POST {local_url}/pair")
        print(f"  curl -X POST {local_url}/enqueue -H 'Content-Type: application/json' -d '{{\"text\":\"hello123\"}}'")
        print(f"  curl -X POST {local_url}/enqueue -H 'Content-Type: application/json' -d '{{\"text\":\"\\n\"}}'")
        print(f"  curl {local_url}/queue")
        print(f"  curl {local_url}/events")
        print("")

        for text in args.enqueue:
            server.enqueue(_decode_text(text))
        if args.enqueue:
            print(f"Queued {len(args.enqueue)} initial item(s).")

        if args.no_prompt:
            print("Server is running. Press Ctrl+C to stop.")
            while True:
                time.sleep(1)

        _interactive_loop(server)
        return 0
    except KeyboardInterrupt:
        print("\nStopping server.")
        return 0
    finally:
        server.stop()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manual LocalKeyboard collection server for ios-feature5-risk1 testing.")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host. Use 0.0.0.0 so the iPhone can reach the Mac.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--advertised-host", default=None, help="Mac LAN IP to show for the phone. Auto-detected if omitted.")
    parser.add_argument("--token", default="auto", help="Token returned by /pair. Use 'auto' to generate one.")
    parser.add_argument("--enqueue", action="append", default=[], help="Initial text item to queue. Repeat for multiple items.")
    parser.add_argument("--enqueue-requires-token", action="store_true", help="Require token for POST /enqueue.")
    parser.add_argument("--no-prompt", action="store_true", help="Run server without the interactive prompt.")
    return parser.parse_args(argv)


def _interactive_loop(server: CommandControlServer) -> None:
    print("Interactive commands:")
    print("  enqueue <text>  queue text")
    print("  return          queue a newline as a separate item")
    print("  queue           show queued and delivered items")
    print("  snapshot        print server state")
    print("  clear           clear queued and delivered items")
    print("  help            show this help")
    print("  quit            stop server")
    print("")
    while True:
        try:
            raw = input("local-keyboard> ").strip()
        except EOFError:
            return
        if not raw:
            continue
        command, _, value = raw.partition(" ")
        normalized = command.lower()
        if normalized in {"quit", "exit", "q"}:
            return
        if normalized in {"help", "h", "?"}:
            print("enqueue <text>, return, queue, snapshot, clear, quit")
            continue
        if normalized in {"return", "enter", "newline"}:
            item = server.enqueue("\n")
            print(f"queued id={item.id} text=\\n")
            continue
        if normalized in {"enqueue", "send"}:
            if not value:
                print("usage: enqueue <text>")
                continue
            item = server.enqueue(_decode_text(value))
            print(f"queued id={item.id} text={_display_text(item.text)}")
            continue
        if normalized in {"snapshot", "status", "state"}:
            print(json.dumps(server.snapshot(), indent=2, sort_keys=True))
            continue
        if normalized in {"queue", "queued"}:
            _print_queue(server.queue_snapshot())
            continue
        if normalized == "clear":
            _clear_state(server)
            print("cleared queue, delivered items, events, errors, and request history")
            continue
        print(f"unknown command: {command}")


def _detect_lan_ip() -> str | None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return str(sock.getsockname()[0])
    except OSError:
        return None
    finally:
        sock.close()


def _decode_text(value: str) -> str:
    return value.encode("utf-8").decode("unicode_escape")


def _display_text(value: str) -> str:
    return value.replace("\n", "\\n")


def _print_queue(snapshot: dict[str, Any]) -> None:
    print(f"queued: {snapshot['queued_count']} | delivered: {snapshot['delivered_count']}")
    if snapshot["queue"]:
        print("Current queue:")
        for item in snapshot["queue"]:
            print(f"  #{item['id']} text={_display_text(item['text'])} created_at={item['created_at']}")
    else:
        print("Current queue: empty")
    if snapshot["delivered"]:
        print("Delivered:")
        for item in snapshot["delivered"][-10:]:
            print(f"  #{item['id']} text={_display_text(item['text'])} delivered_at={item['delivered_at']}")


def _clear_state(server: CommandControlServer) -> None:
    with server._lock:
        server.state.queue.clear()
        server.state.delivered.clear()
        server.state.events.clear()
        server.state.errors.clear()
        server.state.requests.clear()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
