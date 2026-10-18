"""
Holds the host-wide lock on the physical Android test phone, so CLI, API and worker runs and installs never use the
phone at the same time.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import weakref
from datetime import datetime, timezone

from mobile_playbook.common.storage_paths import device_lock_dir

logger = logging.getLogger(__name__)

LOCK_NAME = "android-physical-phone.lock"
_HANDLES: weakref.WeakSet = weakref.WeakSet()


# Close inherited lock handles in forked children so only the owning process keeps the phone locked.
def _close_inherited_locks() -> None:
    for handle in list(_HANDLES):
        handle.close()


os.register_at_fork(after_in_child=_close_inherited_locks)


class PhoneBusy(RuntimeError):
    """Another run or install on this host is using the physical test phone."""


class PhoneLock:
    """The held phone lock; releasing it clears the holder record before dropping the kernel lock."""

    # Keep the open, locked file handle.
    def __init__(self, handle):
        self._handle = handle

    # Clear the holder record and release the lock; safe to call more than once.
    def release(self) -> None:
        if self._handle.closed:
            return
        try:
            self._handle.seek(0)
            self._handle.truncate()
            self._handle.flush()
        except OSError as exc:
            logger.warning("Could not clear the physical phone lock record: %s", exc)
        finally:
            self._handle.close()
        logger.debug("android phone lock: released")


# Take the phone lock without waiting, recording who holds it; PhoneBusy names the current holder when it is taken.
def acquire_phone_lock(purpose: str) -> PhoneLock:
    directory = device_lock_dir()
    directory.mkdir(parents=True, exist_ok=True)
    handle = (directory / LOCK_NAME).open("a+b")
    os.set_inheritable(handle.fileno(), False)
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        holder = phone_lock_holder()
        detail = f" by {holder['purpose']} (process {holder['pid']}, since {holder['since']})" if holder else ""
        raise PhoneBusy(f"The physical test phone is in use{detail}; wait for that run or install to finish.") from exc
    _HANDLES.add(handle)
    record = {"pid": os.getpid(), "purpose": purpose, "since": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    handle.seek(0)
    handle.truncate()
    handle.write(json.dumps(record).encode())
    handle.flush()
    logger.debug("android phone lock: acquired for %s", purpose)
    return PhoneLock(handle)


# Return the live holder's record, or None when the phone is free; read-only, so polling never takes the lock.
def phone_lock_holder() -> dict | None:
    try:
        text = (device_lock_dir() / LOCK_NAME).read_text()
    except FileNotFoundError:
        return None
    try:
        record = json.loads(text) if text.strip() else None
        pid = int(record["pid"]) if record else None
    except (ValueError, TypeError, KeyError):
        logger.warning("The physical phone lock record is unreadable; treating the phone as free: %r", text[:200])
        return None
    if pid is None or not _alive(pid):
        return None
    return record


# Report whether a process exists, counting one this user cannot signal as alive.
def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
