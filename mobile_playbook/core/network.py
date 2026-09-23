from __future__ import annotations

import ipaddress
import socket


def detect_lan_ip() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        try:
            sock.connect(("8.8.8.8", 80))
            return str(sock.getsockname()[0])
        except OSError as exc:
            raise ValueError("could not detect the Mac's routable LAN address") from exc


def resolve_lan_host(configured: str, *, label: str) -> str:
    """A host the iPhone can reach: `auto` detects the Mac's LAN address, loopback is rejected."""
    configured = str(configured or "").strip()
    if not configured:
        raise ValueError(f"{label} is required")
    host = detect_lan_ip() if configured.lower() == "auto" else configured
    normalized = host.strip("[]").lower()
    if normalized == "localhost" or normalized.endswith(".localhost"):
        raise ValueError(f"{label} must not be a loopback address")
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return host
    if address.is_loopback or address.is_unspecified or address.is_link_local:
        raise ValueError(f"{label} must be a routable non-loopback address")
    return host
