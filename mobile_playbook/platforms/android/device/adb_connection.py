"""
Connects adb to a network Android device, telling unauthorized, offline and ready states apart, and checks that the
device has booted with the expected root state.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass

from mobile_playbook.platforms.android.device.adb import AdbClient
from mobile_playbook.platforms.corellium.client import ROOTED_ANDROID_BUILDS

logger = logging.getLogger(__name__)

READY = "device"
UNAUTHORIZED = "unauthorized"
OFFLINE = "offline"
ABSENT = "absent"

Authorizer = Callable[[AdbClient, AdbClient], None]


class AdbConnectionError(RuntimeError):
    """adb could not reach an authorized, booted device in the expected root state."""

    # Keep the serial and the last adb state so callers can report them without parsing the message.
    def __init__(self, message: str, *, serial: str, state: str):
        super().__init__(message)
        self.serial = serial
        self.state = state


@dataclass(frozen=True)
class AdbConnectPolicy:
    """Bounds for connecting adb to one device and waiting until it is ready."""

    timeout: float = 600
    connect_attempts: int = 5
    unauthorized_grace: float = 5
    poll: float = 2

    # Reject budgets that could wait forever or never try to connect.
    def __post_init__(self):
        numbers = (self.timeout, self.unauthorized_grace, self.poll)
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in numbers):
            raise ValueError("adb connection timeouts must be finite numbers")
        if self.timeout <= 0 or self.poll <= 0 or self.unauthorized_grace < 0:
            raise ValueError("adb connection timeout and poll must be positive and the grace nonnegative")
        if isinstance(self.connect_attempts, bool) or not isinstance(self.connect_attempts, int) or self.connect_attempts < 1:
            raise ValueError("adb connect_attempts must be a positive integer")


# Report whether Android on the device has finished booting.
def boot_completed(phone: AdbClient) -> bool:
    code, out, _ = phone.run(["shell", "getprop", "sys.boot_completed"])
    return code == 0 and out.strip() == "1"


# Fail unless the device's build type gives the expected root state; userdebug and eng builds are rooted.
def verify_root_state(phone: AdbClient, serial: str, rooted: bool) -> str:
    code, out, _ = phone.run(["shell", "getprop", "ro.build.type"])
    build_type = out.strip() if code == 0 else ""
    if not build_type:
        raise AdbConnectionError(f"{serial} did not report its build type, so its root state is unknown.", serial=serial, state=READY)
    actual = build_type in ROOTED_ANDROID_BUILDS
    if actual != rooted:
        raise AdbConnectionError(
            f"{serial} runs a {build_type} build, which is {'rooted' if actual else 'not rooted'}, "
            f"but a {'rooted' if rooted else 'non-rooted'} device was expected.",
            serial=serial,
            state=READY,
        )
    return build_type


# Connect adb to the serial and wait until it is an authorized, booted device whose root state matches rooted,
# reconnecting an absent or offline device a bounded number of times and authorizing an unauthorized one once.
def connect_ready(
    adb_path: str,
    serial: str,
    *,
    rooted: bool,
    policy: AdbConnectPolicy | None = None,
    authorize: Authorizer | None = None,
) -> str:
    policy = policy or AdbConnectPolicy()
    host = AdbClient(adb_path)
    phone = AdbClient(adb_path, serial)
    deadline = time.monotonic() + policy.timeout
    attempts = 0
    unauthorized_since: float | None = None
    authorization_attempted = False
    while True:
        state = host.device_states().get(serial, ABSENT)
        if state == READY:
            unauthorized_since = None
            if boot_completed(phone):
                build_type = verify_root_state(phone, serial, rooted)
                logger.debug("android adb: %s is ready (%s build)", serial, build_type)
                return build_type
        elif state == UNAUTHORIZED:
            now = time.monotonic()
            unauthorized_since = now if unauthorized_since is None else unauthorized_since
            # adb briefly reports unauthorized while the key handshake runs, even for an authorized host.
            if now - unauthorized_since >= policy.unauthorized_grace:
                if authorization_attempted:
                    raise AdbConnectionError(f"adb on {serial} is still unauthorized after approving the debugging prompt.", serial=serial, state=state)
                if authorize is None:
                    raise AdbConnectionError(
                        f"adb on {serial} is unauthorized: the device has not approved this host's adb key.",
                        serial=serial,
                        state=state,
                    )
                authorization_attempted = True
                authorize(host, phone)
                continue
        elif state in (ABSENT, OFFLINE):
            unauthorized_since = None
            if attempts >= policy.connect_attempts:
                raise AdbConnectionError(
                    f"adb could not connect to {serial} after {attempts} attempts; it is {state}. "
                    "Check that this host can reach the device (for Corellium, the project's VPN).",
                    serial=serial,
                    state=state,
                )
            attempts += 1
            if state == OFFLINE:
                host.run(["disconnect", serial])
            code, out, err = host.run(["connect", serial])
            logger.debug("android adb: connect %s (attempt %s) exited %s (%s)", serial, attempts, code, (out or err)[:200])
        if time.monotonic() >= deadline:
            waiting = "booting" if state == READY else state
            raise AdbConnectionError(f"{serial} was still {waiting} after {policy.timeout:g}s.", serial=serial, state=state)
        time.sleep(min(policy.poll, max(0.0, deadline - time.monotonic())))
