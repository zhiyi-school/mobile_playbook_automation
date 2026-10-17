"""Persist exclusive host claims and recovery history independently of device connection packages."""

from __future__ import annotations

import fcntl
import functools
import hashlib
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
import weakref
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from mobile_playbook.common.storage_paths import corellium_state_dir

from .client import CorelliumError, CorelliumInstance, api_base_url
from .selection import ROLES, ResolvedDevice

_LOCK_HANDLES: weakref.WeakSet = weakref.WeakSet()


# Close inherited lock handles in forked children so only the owning process keeps an instance locked.
def _close_inherited_locks() -> None:
    for handle in list(_LOCK_HANDLES):
        handle.close()


os.register_at_fork(after_in_child=_close_inherited_locks)


# Identify this machine stably across hostname changes, which macOS makes when networks change.
@functools.lru_cache(maxsize=1)
def host_identity() -> str:
    if sys.platform == "darwin":
        try:
            output = subprocess.run(["/usr/sbin/ioreg", "-rd1", "-c", "IOPlatformExpertDevice"], capture_output=True,
                                    text=True, timeout=10, check=True).stdout
        except (OSError, subprocess.SubprocessError):
            output = ""
        match = re.search(r'"IOPlatformUUID" = "([^"]+)"', output)
        if match:
            return match.group(1)
    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            value = Path(path).read_text().strip()
        except OSError:
            continue
        if value:
            return value
    return socket.gethostname()


class ClaimBusy(CorelliumError):
    """An active owner or unresolved cleanup prevents instance reuse."""


class ClaimStore:
    """SQLite durable ownership plus kernel locks held for an owner's lifetime on one host."""

    # Use a shared host path, scoped by API identity so unrelated Corellium servers cannot collide.
    def __init__(self, api_url: str, root: Path | None = None):
        self.api_url = api_base_url(api_url)
        namespace = hashlib.sha256(self.api_url.encode()).hexdigest()
        self.root = (root if root is not None else corellium_state_dir()) / namespace
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "ownership.sqlite3"
        self.host_id = host_identity()
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS claims (
                    instance_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, role TEXT NOT NULL,
                    baseline_id TEXT NOT NULL, owner TEXT NOT NULL, pid INTEGER NOT NULL,
                    hostname TEXT NOT NULL, host_id TEXT NOT NULL, claimed_at REAL NOT NULL, phase TEXT NOT NULL,
                    device_json TEXT NOT NULL, error TEXT
                );
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY, at REAL NOT NULL, instance_id TEXT NOT NULL,
                    run_id TEXT NOT NULL, owner TEXT NOT NULL, phase TEXT NOT NULL, error TEXT
                );
                CREATE TABLE IF NOT EXISTS run_devices (
                    run_id TEXT NOT NULL, instance_id TEXT NOT NULL, role TEXT NOT NULL,
                    baseline_id TEXT NOT NULL, device_json TEXT NOT NULL,
                    PRIMARY KEY (run_id, instance_id)
                );
            """)

    # Open a transaction with synchronous durability; corrupt state fails closed rather than becoming empty.
    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    # Acquire a nonblocking kernel lock that is never unlinked, including after successful cleanup.
    def lock(self, instance_id: str):
        name = hashlib.sha256(instance_id.encode()).hexdigest() + ".lock"
        handle = (self.root / name).open("a+b")
        os.set_inheritable(handle.fileno(), False)
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise ClaimBusy(f"Instance {instance_id} has an active owner") from exc
        _LOCK_HANDLES.add(handle)
        return handle

    # Persist the complete sorted claim set atomically, releasing all new locks if any instance is unavailable.
    def claim_all(self, run_id: str, devices: tuple[ResolvedDevice, ...]) -> ClaimSet:
        if not run_id:
            raise ValueError("run_id is required")
        if len({device.instance.id for device in devices}) != len(devices):
            raise CorelliumError("Cannot claim the same instance for multiple roles")
        if len({device.role for device in devices}) != len(devices):
            raise CorelliumError("Cannot claim multiple instances for one role")
        for device in devices:
            device.baseline.validate()
            if (device.role not in ROLES or device.instance.rooted is not ROLES[device.role]
                    or not device.instance.id or not device.baseline_id
                    or (device.baseline.snapshot_id and device.baseline_id != device.baseline.snapshot_id)):
                raise CorelliumError("Invalid resolved role, instance or baseline identity")
        owner = str(uuid.uuid4())
        handles = {}
        try:
            for device in sorted(devices, key=lambda item: item.instance.id):
                handles[device.instance.id] = self.lock(device.instance.id)
            with self.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                if devices and db.execute("SELECT 1 FROM events WHERE run_id=?", (run_id,)).fetchone():
                    raise ClaimBusy(f"Run {run_id} already claimed devices; its resolved identities cannot be replaced")
                for device in sorted(devices, key=lambda item: item.instance.id):
                    instance_id = device.instance.id
                    if db.execute("SELECT 1 FROM claims WHERE instance_id=?", (instance_id,)).fetchone():
                        raise ClaimBusy(f"Instance {instance_id} has pending ownership; recover cleanup before reuse")
                    db.execute("INSERT INTO claims VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                               (instance_id, run_id, device.role, device.baseline_id, owner, os.getpid(),
                                socket.gethostname(), self.host_id, time.time(), "claimed",
                                json.dumps(asdict(device)), None))
                    db.execute("INSERT INTO run_devices VALUES (?, ?, ?, ?, ?)",
                               (run_id, instance_id, device.role, device.baseline_id, json.dumps(asdict(device))))
                    self._event(db, instance_id, run_id, owner, "claimed", None)
        except BaseException:
            for handle in handles.values():
                handle.close()
            raise
        return ClaimSet(self, run_id, owner, devices, handles)

    # Read durable ownership without taking a mutation lock or contacting any VM.
    def pending(self) -> list[dict]:
        with self.connection() as db:
            return [dict(row) for row in db.execute("SELECT * FROM claims ORDER BY instance_id")]

    # Record a state transition in the same transaction as the ownership update.
    def transition(self, instance_id: str, owner: str, phase: str, error: str | None = None) -> None:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM claims WHERE instance_id=? AND owner=?", (instance_id, owner)).fetchone()
            if row is None:
                raise CorelliumError(f"Ownership lost for {instance_id}")
            db.execute("UPDATE claims SET phase=?, error=? WHERE instance_id=? AND owner=?", (phase, error, instance_id, owner))
            self._event(db, instance_id, row["run_id"], owner, phase, error)

    # Remove ownership only after confirmed shutdown, retaining an audit event for the run.
    def release(self, instance_id: str, owner: str) -> None:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM claims WHERE instance_id=? AND owner=?", (instance_id, owner)).fetchone()
            if row is None:
                raise CorelliumError(f"Ownership lost for {instance_id}")
            if row["phase"] != "off":
                raise CorelliumError("Ownership may only be released after confirmed off")
            self._event(db, instance_id, row["run_id"], owner, "released", None)
            db.execute("DELETE FROM claims WHERE instance_id=? AND owner=?", (instance_id, owner))

    # Return durable lifecycle reporting even after successful ownership release.
    def history(self, run_id: str) -> list[dict]:
        with self.connection() as db:
            return [dict(row) for row in db.execute("""
                SELECT events.*, run_devices.role, run_devices.baseline_id, run_devices.device_json
                FROM events JOIN run_devices USING (run_id, instance_id)
                WHERE events.run_id=? ORDER BY sequence
            """, (run_id,))]

    # Append a transition while the caller holds the database transaction.
    @staticmethod
    def _event(db, instance_id, run_id, owner, phase, error):
        db.execute("INSERT INTO events(at, instance_id, run_id, owner, phase, error) VALUES (?, ?, ?, ?, ?, ?)",
                   (time.time(), instance_id, run_id, owner, phase, error))


class ClaimSet:
    """An explicit owner handle; dropping it never deletes durable cleanup obligations."""

    # Keep locks alive until cleanup or explicit detachment, and reject inherited use after fork.
    def __init__(self, store, run_id, owner, devices, handles):
        self.store = store
        self.run_id = run_id
        self.owner = owner
        self.devices = devices
        self.handles = handles
        self.pid = os.getpid()
        self.mutex = threading.RLock()
        self.prepared: dict[str, CorelliumInstance] = {}

    # Verify this process still holds its lock and the exact persisted claim before mutating a VM.
    def record(self, instance_id: str) -> dict:
        if self.pid != os.getpid() or instance_id not in self.handles:
            raise CorelliumError(f"No active ownership handle for {instance_id}")
        for row in self.store.pending():
            if row["instance_id"] == instance_id and row["owner"] == self.owner:
                return row
        raise CorelliumError(f"Ownership lost for {instance_id}")

    # Drop this process's locks after cleanup attempts, preserving unresolved database rows for recovery.
    def detach(self) -> None:
        for handle in self.handles.values():
            handle.close()
        self.handles.clear()
