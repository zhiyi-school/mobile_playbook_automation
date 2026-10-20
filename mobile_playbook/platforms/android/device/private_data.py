"""
Lists and copies an app's private data directories off an Android device as a tar archive, as root on a rooted device
or through `run-as` for a debuggable app.
"""

from __future__ import annotations

import logging
import shlex
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from mobile_playbook.platforms.android.device.adb import AdbClient
from mobile_playbook.platforms.android.device.root import RootShell
from mobile_playbook.platforms.android.device.run_as import RunAsShell

logger = logging.getLogger(__name__)

PRIVATE_DATA_DIRS = ("/data/data/{package}", "/data/user_de/0/{package}")
REMOTE_ARCHIVE = "/data/local/tmp/mobile-playbook-private-data-{package}.tar"
PULL_TIMEOUT = 600


class PrivateDataShell(Protocol):
    """Runs device shell commands with access to the app's private data, as root or as the app's user."""

    adb: AdbClient
    access: str

    def run(self, command: str, timeout: float | None = ...) -> tuple[int, str, str]: ...


class PrivateDataError(RuntimeError):
    """The app's private data could not be listed or copied off the device."""


@dataclass
class PrivateDataCopy:
    archive: Path
    directories: list[str]
    listing: str
    size_bytes: int
    tar_warnings: list[str] = field(default_factory=list)


# Return the app's private data directories that exist on the device; the first is required.
def existing_private_dirs(shell: PrivateDataShell, package: str) -> list[str]:
    present = []
    for template in PRIVATE_DATA_DIRS:
        directory = template.format(package=package)
        code, _, _ = shell.run(f"test -d {shlex.quote(directory)}")
        logger.debug("android private data: %s exists=%s", directory, code == 0)
        if code == 0:
            present.append(directory)
        elif not present:
            raise PrivateDataError(f"{directory} does not exist; is {package} installed on the device?")
    return present


# List the directories recursively, as the playbook's `su -c ls` and `run-as ls` do.
def list_private_dirs(shell: PrivateDataShell, directories: list[str]) -> str:
    code, out, err = shell.run("ls -laR " + " ".join(shlex.quote(item) for item in directories), timeout=120)
    if code != 0 and not out:
        raise PrivateDataError(f"Listing the private data directories {shell.access} failed: {err or f'exit {code}'}")
    return out


# Return the directories' total size in bytes as `du` reports it.
def private_dirs_size(shell: PrivateDataShell, directories: list[str]) -> int:
    code, out, err = shell.run("du -sk " + " ".join(shlex.quote(item) for item in directories), timeout=120)
    sizes = [line.split()[0] for line in out.splitlines() if line.split()]
    if code != 0 or not sizes or not all(size.isdigit() for size in sizes):
        raise PrivateDataError(f"Measuring the private data directories {shell.access} failed: {err or out or f'exit {code}'}")
    return sum(int(size) for size in sizes) * 1024


# Archive the app's private data directories as root, pull the archive to `destination` and remove the device copy.
def copy_private_data(root: RootShell, package: str, destination: Path, max_bytes: int) -> PrivateDataCopy:
    directories = existing_private_dirs(root, package)
    listing = list_private_dirs(root, directories)
    size = private_dirs_size(root, directories)
    logger.debug("android private data: %s holds %s bytes in %s", package, size, directories)
    if size > max_bytes:
        raise PrivateDataError(f"The private data directories hold {size} bytes, more than the {max_bytes} byte limit (max_archive_mb).")
    remote = REMOTE_ARCHIVE.format(package=package)
    members = " ".join(shlex.quote(item.lstrip("/")) for item in directories)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        code, _, err = root.run(f"tar -cf {shlex.quote(remote)} -C / {members} && chmod 644 {shlex.quote(remote)}", timeout=PULL_TIMEOUT)
        # tar exits non-zero when it skips sockets or files that vanish mid-copy, but the archive still holds the rest.
        warnings = [line.strip() for line in (err or "").splitlines() if line.strip()]
        if code != 0:
            kept_code, _, _ = root.run(f"test -s {shlex.quote(remote)} && chmod 644 {shlex.quote(remote)}")
            if kept_code != 0:
                raise PrivateDataError(f"Archiving the private data directories as root failed: {err or f'exit {code}'}")
        code, _, err = root.adb.run(["pull", remote, str(destination)], timeout=PULL_TIMEOUT)
        if code != 0 or not destination.is_file():
            raise PrivateDataError(f"Pulling the private data archive failed: {err or f'exit {code}'}")
    finally:
        cleanup_code, _, cleanup_err = root.run(f"rm -f {shlex.quote(remote)}")
        logger.debug("android private data: removed %s exit %s %r", remote, cleanup_code, (cleanup_err or "")[:200])
    logger.debug("android private data: pulled %s (%s bytes), tar warnings=%s", destination, destination.stat().st_size, warnings)
    return PrivateDataCopy(destination, directories, listing, destination.stat().st_size, warnings)


# Stream the app's private data directories as a tar archive into `destination` through run-as, writing nothing on the
# device, since the app's user cannot write to /data/local/tmp.
def copy_private_data_run_as(shell: RunAsShell, destination: Path, max_bytes: int) -> PrivateDataCopy:
    directories = existing_private_dirs(shell, shell.package)
    listing = list_private_dirs(shell, directories)
    size = private_dirs_size(shell, directories)
    logger.debug("android private data: %s holds %s bytes in %s", shell.package, size, directories)
    if size > max_bytes:
        raise PrivateDataError(f"The private data directories hold {size} bytes, more than the {max_bytes} byte limit (max_archive_mb).")
    destination.parent.mkdir(parents=True, exist_ok=True)
    # `shell -T` keeps stderr out of the binary stream and returns tar's exit code; `exec-out` would mix them in.
    members = [shlex.quote(item.lstrip("/")) for item in directories]
    code, err = shell.adb.run_to_file(["shell", "-T", "run-as", shell.package, "tar", "-cf", "-", "-C", "/", *members], destination, timeout=PULL_TIMEOUT)
    # tar exits 1 when it skips sockets or files that vanish mid-copy, but the archive still holds the rest.
    warnings = [line.strip() for line in (err or "").splitlines() if line.strip()]
    if code not in (0, 1) or not destination.is_file() or not tarfile.is_tarfile(destination):
        destination.unlink(missing_ok=True)
        raise PrivateDataError(f"Streaming the private data directories with run-as failed: {err or f'exit {code}'}")
    logger.debug("android private data: streamed %s (%s bytes), tar exit %s, warnings=%s", destination, destination.stat().st_size, code, warnings)
    return PrivateDataCopy(destination, directories, listing, destination.stat().st_size, warnings)
