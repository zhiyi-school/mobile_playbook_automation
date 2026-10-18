import json
import os
import signal
import time
from pathlib import Path

import pytest

from mobile_playbook.common import env_file
from mobile_playbook.common.storage_paths import device_lock_dir
from mobile_playbook.platforms.android.device.phone_lock import LOCK_NAME, PhoneBusy, acquire_phone_lock, phone_lock_holder


def record_path() -> Path:
    return device_lock_dir() / LOCK_NAME


def test_the_lock_records_its_holder_until_it_is_released():
    lock = acquire_phone_lock("Android run 2026-10-18_10-00-00")

    holder = phone_lock_holder()
    assert (holder["purpose"], holder["pid"]) == ("Android run 2026-10-18_10-00-00", os.getpid())

    lock.release()
    lock.release()
    assert phone_lock_holder() is None
    assert record_path().read_text() == ""
    acquire_phone_lock("the next run").release()


def test_a_second_user_is_refused_with_the_holder_named():
    lock = acquire_phone_lock("Android install of one")
    try:
        with pytest.raises(PhoneBusy, match=rf"in use by Android install of one \(process {os.getpid()}"):
            acquire_phone_lock("Android run 2026-10-18_10-00-00")
        assert phone_lock_holder()["purpose"] == "Android install of one"
    finally:
        lock.release()


def test_another_process_holding_the_phone_blocks_this_one(phone_held_by_another_process):
    with pytest.raises(PhoneBusy, match=f"Android run in another process \\(process {phone_held_by_another_process.pid}"):
        acquire_phone_lock("Android run here")

    assert phone_lock_holder()["pid"] == phone_held_by_another_process.pid


def test_a_killed_holder_frees_the_phone_and_its_stale_record_is_ignored(phone_held_by_another_process):
    phone_held_by_another_process.send_signal(signal.SIGKILL)
    phone_held_by_another_process.wait(timeout=10)

    assert json.loads(record_path().read_text())["pid"] == phone_held_by_another_process.pid
    assert phone_lock_holder() is None
    acquire_phone_lock("Android run after the crash").release()


def test_an_unreadable_record_reads_as_free():
    device_lock_dir().mkdir(parents=True, exist_ok=True)
    record_path().write_text("not json")

    assert phone_lock_holder() is None


def test_no_lock_file_reads_as_free():
    assert not record_path().exists()
    assert phone_lock_holder() is None


def test_a_forked_child_does_not_keep_the_phone_locked():
    lock = acquire_phone_lock("parent run")
    read_end, write_end = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(read_end)
        os.write(write_end, b"ready")
        time.sleep(10)
        os._exit(0)
    try:
        os.close(write_end)
        assert os.read(read_end, 5) == b"ready"
        lock.release()
        acquire_phone_lock("next run").release()
    finally:
        os.close(read_end)
        os.kill(child, signal.SIGKILL)
        os.waitpid(child, 0)


def test_the_lock_directory_must_be_absolute_and_set_in_the_process_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVICE_LOCK_DIR", str(tmp_path))
    assert device_lock_dir() == tmp_path.resolve()
    monkeypatch.setenv("DEVICE_LOCK_DIR", "relative")
    with pytest.raises(ValueError, match="absolute"):
        device_lock_dir()
    env_file.ENV_FILE.write_text(f"DEVICE_LOCK_DIR={tmp_path}\n")
    monkeypatch.setenv("DEVICE_LOCK_DIR", str(tmp_path))
    with pytest.raises(ValueError, match="process environment, not in .env"):
        device_lock_dir()


def test_tests_never_default_to_the_real_host_lock():
    assert not device_lock_dir().is_relative_to(Path.home() / ".local" / "state")
