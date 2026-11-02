"""
Points the device's global HTTP proxy at Burp with `adb shell settings put global http_proxy`, as playbook feature-03
step 03 does, and resets it afterwards.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# The value playbook feature-03 resets the proxy with when testing is finished.
NO_PROXY = ":0"


class TrafficInterceptionSetupError(RuntimeError):
    """Traffic interception setup failure carrying a status and the setup state reached."""

    # Store the failure status and setup state alongside the message.
    def __init__(self, status: str, message: str, state: dict[str, Any] | None = None):
        super().__init__(message)
        self.status = status
        self.state = state or {}


@dataclass(frozen=True)
class ProxyState:
    """The proxy the device had before the run and the one the run set."""

    previous: str | None
    applied: str

    # Describe the proxy change for report metadata.
    def to_dict(self) -> dict[str, Any]:
        return {"previous": self.previous, "applied": self.applied}


# Return the device's global HTTP proxy, or None when it has none ("null" or empty).
def read_device_proxy(adb) -> str | None:
    code, out, err = adb.run(["shell", "settings", "get", "global", "http_proxy"])
    if code != 0:
        raise RuntimeError(f"reading http_proxy failed: {err or out}")
    value = (out or "").strip()
    return None if value in {"", "null"} else value


# Set the device's global HTTP proxy to host:port, recording the value it replaced, and confirm the device reports it.
def set_device_proxy(adb, host: str, port: int) -> ProxyState:
    if not 1 <= int(port) <= 65535:
        raise TrafficInterceptionSetupError("DEVICE_PROXY_SET_FAILED", f"proxy port {port} is not between 1 and 65535")
    applied = f"{host}:{int(port)}"
    try:
        previous = read_device_proxy(adb)
        _put_proxy(adb, applied)
    except Exception as exc:
        logger.debug("android traffic setup: setting the proxy to %s failed: %s", applied, exc, exc_info=True)
        raise TrafficInterceptionSetupError("DEVICE_PROXY_SET_FAILED", str(exc), {"applied": applied}) from exc
    logger.debug("android traffic setup: proxy set to %s (previous %s)", applied, previous)
    return ProxyState(previous, applied)


# Put back the proxy the device had before the run, or reset it with ":0" as the playbook does when it had none. A
# previous value equal to the one applied was left by an earlier run that did not finish, so it is reset too.
def restore_device_proxy(adb, state: ProxyState) -> dict[str, Any]:
    keep_previous = state.previous not in (None, NO_PROXY, state.applied)
    target = state.previous if keep_previous else NO_PROXY
    try:
        _put_proxy(adb, target)
    except Exception as exc:
        logger.debug("android traffic restore: setting the proxy to %s failed: %s", target, exc, exc_info=True)
        raise TrafficInterceptionSetupError("DEVICE_PROXY_RESTORE_FAILED", str(exc), state.to_dict()) from exc
    logger.debug("android traffic restore: proxy set to %s", target)
    return {"status": "RESTORED", "proxy": target}


# Write the global HTTP proxy and raise unless the device then reports that value; ":0" may read back as no proxy.
def _put_proxy(adb, value: str) -> None:
    code, out, err = adb.run(["shell", "settings", "put", "global", "http_proxy", value])
    if code != 0:
        raise RuntimeError(f"settings put global http_proxy {value} failed: {err or out}")
    reported = read_device_proxy(adb)
    if reported != value and not (value == NO_PROXY and reported is None):
        raise RuntimeError(f"the device reports http_proxy {reported!r} after setting it to {value!r}")
