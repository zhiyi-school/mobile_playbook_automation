from __future__ import annotations

import ipaddress
import logging
import socket

logger = logging.getLogger(__name__)


def detect_lan_ip() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        try:
            sock.connect(("8.8.8.8", 80))
            address = str(sock.getsockname()[0])
            logger.debug("network: detected LAN address %s", address)
            return address
        except OSError as exc:
            logger.debug("network: LAN address detection failed: %s", exc, exc_info=True)
            raise ValueError("could not detect the Mac's routable LAN address") from exc


def resolve_lan_host(configured: str, *, label: str) -> str:
    """A host the iPhone can reach: `auto` detects the Mac's LAN address, loopback is rejected."""
    configured = str(configured or "").strip()
    logger.debug("network: resolving %s from configured value %r", label, configured)
    if not configured:
        raise ValueError(f"{label} is required")
    host = detect_lan_ip() if configured.lower() == "auto" else configured
    normalized = host.strip("[]").lower()
    if normalized == "localhost" or normalized.endswith(".localhost"):
        logger.debug("network: %s rejected, %s is a loopback name", label, host)
        raise ValueError(f"{label} must not be a loopback address")
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError as exc:
        logger.debug("network: %s host %s is a hostname, not an IP (%s); accepting", label, host, exc)
        return host
    if address.is_loopback or address.is_unspecified or address.is_link_local:
        logger.debug("network: %s rejected, %s is loopback=%s unspecified=%s link_local=%s", label, host, address.is_loopback, address.is_unspecified, address.is_link_local)
        raise ValueError(f"{label} must be a routable non-loopback address")
    logger.debug("network: %s resolved to %s", label, host)
    return host
