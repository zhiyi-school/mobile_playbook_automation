from __future__ import annotations

import shlex
import tarfile

import pytest

from mobile_playbook.platforms.android.device.private_data import PrivateDataError, copy_private_data_run_as
from mobile_playbook.platforms.android.device.run_as import RunAsShell
from tests.platforms.android.private_data_fakes import DATA, PACKAGE, PLAIN_PREFS, make_archive


class RunAsAdb:
    """A debuggable app whose run-as answers the directory checks and streams `tar_bytes` for `shell -T run-as tar`."""

    def __init__(self, tar_bytes: bytes = b"", *, user_de=False, du_kb=8, tar_code=0, tar_err=""):
        self.tar_bytes = tar_bytes
        self.user_de = user_de
        self.du_kb = du_kb
        self.tar_code = tar_code
        self.tar_err = tar_err
        self.commands: list[str] = []
        self.streams: list[list[str]] = []

    def run(self, args, timeout=None):
        assert args[:5] == ["shell", "run-as", PACKAGE, "sh", "-c"]
        command = shlex.split(args[5])[0]
        self.commands.append(command)
        if command == f"test -d /data/data/{PACKAGE}":
            return 0, "", ""
        if command == f"test -d /data/user_de/0/{PACKAGE}":
            return (0, "", "") if self.user_de else (1, "", "")
        if command.startswith("ls -laR"):
            return 0, f"/data/data/{PACKAGE}:\nshared_prefs\n", ""
        if command.startswith("du -sk"):
            return 0, f"{self.du_kb}\t/data/data/{PACKAGE}\n", ""
        return 1, "", "unexpected"

    def run_to_file(self, args, destination, timeout=None):
        self.streams.append(args)
        destination.write_bytes(self.tar_bytes)
        return self.tar_code, self.tar_err


def tar_bytes(tmp_path, files) -> bytes:
    return make_archive(tmp_path / "device.tar", files).read_bytes()


def test_the_directories_are_streamed_through_run_as_without_writing_to_the_device(tmp_path):
    adb = RunAsAdb(tar_bytes(tmp_path, {f"{DATA}/shared_prefs/settings.xml": PLAIN_PREFS}), user_de=True)
    destination = tmp_path / "work" / "copy.tar"

    copied = copy_private_data_run_as(RunAsShell(adb, PACKAGE), destination, max_bytes=1024 * 1024)

    assert adb.streams == [["shell", "-T", "run-as", PACKAGE, "tar", "-cf", "-", "-C", "/", f"data/data/{PACKAGE}", f"data/user_de/0/{PACKAGE}"]]
    assert copied.directories == [f"/data/data/{PACKAGE}", f"/data/user_de/0/{PACKAGE}"]
    assert copied.listing.startswith(f"/data/data/{PACKAGE}:")
    assert copied.size_bytes == destination.stat().st_size
    assert copied.tar_warnings == []
    with tarfile.open(destination) as tar:
        assert tar.getnames() == [f"{DATA}/shared_prefs/settings.xml"]
    assert not any(command.startswith(("tar", "rm", "chmod")) for command in adb.commands)


def test_tar_warnings_about_skipped_files_keep_the_archive(tmp_path):
    adb = RunAsAdb(tar_bytes(tmp_path, {f"{DATA}/shared_prefs/settings.xml": PLAIN_PREFS}), tar_code=1, tar_err="tar: socket ignored")

    copied = copy_private_data_run_as(RunAsShell(adb, PACKAGE), tmp_path / "copy.tar", max_bytes=1024 * 1024)

    assert copied.tar_warnings == ["tar: socket ignored"]


def test_private_data_above_the_size_limit_is_not_streamed(tmp_path):
    adb = RunAsAdb(du_kb=3 * 1024)

    with pytest.raises(PrivateDataError, match="max_archive_mb"):
        copy_private_data_run_as(RunAsShell(adb, PACKAGE), tmp_path / "copy.tar", max_bytes=2 * 1024 * 1024)
    assert adb.streams == []


@pytest.mark.parametrize(
    ("output", "code", "err"),
    [
        (b"run-as: package not debuggable", 0, ""),
        (b"", 255, "error: closed"),
        (b"partial", 124, "adb timed out after 600s"),
        (None, 2, "tar: data/data/com.example.app: Permission denied"),
    ],
    ids=["not-a-tar", "connection-closed", "timed-out", "tar-failed"],
)
def test_a_stream_that_is_not_a_complete_tar_fails_and_leaves_no_partial_copy(tmp_path, output, code, err):
    data = tar_bytes(tmp_path, {}) if output is None else output
    destination = tmp_path / "copy.tar"

    with pytest.raises(PrivateDataError, match="Streaming the private data directories with run-as failed"):
        copy_private_data_run_as(RunAsShell(RunAsAdb(data, tar_code=code, tar_err=err), PACKAGE), destination, max_bytes=1024 * 1024)
    assert not destination.exists()


def test_listing_and_measuring_failures_say_they_used_run_as(tmp_path):
    class FailingAdb(RunAsAdb):
        def run(self, args, timeout=None):
            command = shlex.split(args[5])[0]
            if command.startswith("ls -laR"):
                return 1, "", "ls: Permission denied"
            return super().run(args, timeout)

    with pytest.raises(PrivateDataError, match="Listing the private data directories with run-as failed: ls: Permission denied"):
        copy_private_data_run_as(RunAsShell(FailingAdb(), PACKAGE), tmp_path / "copy.tar", max_bytes=1024 * 1024)
