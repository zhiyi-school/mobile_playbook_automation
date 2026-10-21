from __future__ import annotations

import subprocess

import pytest

from mobile_playbook.platforms.android.device.logcat import (
    LogcatCapture,
    LogcatError,
    app_lines,
    app_pids,
    app_uid,
    clear_logcat,
    read_app_lines,
    start_logcat,
)

PACKAGE = "com.example.app"

DUMPSYS = """\
Packages:
  Package [com.example.app.helper] (1a2b3c4):
    userId=10500
  Package [com.example.app] (5d6e7f8):
    appId=10234
    userId=10234
    flags=[ HAS_CODE ]

Hidden system packages:
  Package [com.example.app] (9a8b7c6):
    userId=1000
"""

UID_LINES = [
    "--------- beginning of main\n",
    "10-21 12:00:00.100 10234  4321  4330 D LoginActivity: email=canary@example.com\n",
    "10-21 12:00:00.200 10500  4400  4400 I Other: from another app\n",
    "10-21 12:00:00.300  root   612   612 I Init: system line\n",
    "10-21 12:00:00.400        4321  4331 W Network: uid unknown to logd\n",
    "10-21 12:00:00.500 10234  4999  4999 E Payment: card 4111 1111 1111 1111\n",
]

PLAIN_LINES = [
    "10-21 12:00:00.100  4321  4330 D LoginActivity: email=canary@example.com\n",
    "10-21 12:00:00.200  4400  4400 I Other: from another app\n",
    "10-21 12:00:00.300  5000  5001 I Remote: from the app's remote process\n",
]


class FakeProcess:
    def __init__(self, exit_code=None, stubborn=False):
        self.returncode = exit_code
        self.stubborn = stubborn
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        if not self.stubborn:
            self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired("adb", timeout)
        return self.returncode


class FakeAdb:
    adb_path = "adb"

    def __init__(self, results=None, started=None):
        self.results = results or {}
        self.started = list(started or [])
        self.calls: list[list[str]] = []
        self.start_calls: list[list[str]] = []

    def run(self, args, timeout=None):
        self.calls.append(args)
        return self.results.get(tuple(args), (0, "", ""))

    def start_to_file(self, args, destination):
        self.start_calls.append(args)
        process, output = self.started.pop(0)
        with destination.open("ab") as handle:
            handle.write(output)
        return process


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr("mobile_playbook.platforms.android.device.logcat.time.sleep", lambda seconds: None)


def test_lines_with_a_uid_are_kept_by_the_apps_uid_and_others_by_its_pids():
    assert app_lines(UID_LINES, 10234, {4321}) == [
        "10-21 12:00:00.100 10234  4321  4330 D LoginActivity: email=canary@example.com",
        "10-21 12:00:00.400        4321  4331 W Network: uid unknown to logd",
        "10-21 12:00:00.500 10234  4999  4999 E Payment: card 4111 1111 1111 1111",
    ]


def test_a_restarted_process_is_kept_by_uid_even_when_its_pid_was_not_seen():
    kept = app_lines(UID_LINES, 10234, set())

    assert any("Payment" in line for line in kept)
    assert not any("uid unknown" in line for line in kept)


def test_without_uids_lines_are_kept_by_every_pid_the_app_was_seen_with():
    assert app_lines(PLAIN_LINES, 10234, {4321, 5000}) == [PLAIN_LINES[0].rstrip("\n"), PLAIN_LINES[2].rstrip("\n")]


def test_an_unknown_app_uid_falls_back_to_pids_for_lines_with_a_uid():
    assert app_lines(UID_LINES, None, {4400}) == [UID_LINES[2].rstrip("\n")]


def test_read_app_lines_reads_a_capture_with_undecodable_bytes(tmp_path):
    path = tmp_path / "logcat.txt"
    path.write_bytes("".join(PLAIN_LINES).encode() + b"10-21 12:00:01.000  4321  4330 D Bytes: \xff\xfe\n")

    kept = read_app_lines(path, None, {4321})

    assert len(kept) == 2
    assert kept[1].endswith("Bytes: ��")


def test_app_uid_reads_the_active_package_block_only():
    adb = FakeAdb({("shell", "dumpsys", "package", PACKAGE): (0, DUMPSYS, "")})

    assert app_uid(adb, PACKAGE) == 10234


@pytest.mark.parametrize("result", [(0, "", ""), (1, "", "error"), (0, "Packages:\n  Package [com.example.app] (1):\n    flags=[ ]\n  Package [com.other] (2):\n    userId=10999\n", "")])
def test_app_uid_is_none_when_the_device_does_not_report_it(result):
    assert app_uid(FakeAdb({("shell", "dumpsys", "package", PACKAGE): result}), PACKAGE) is None


def test_app_pids_include_named_processes_and_skip_similar_packages():
    ps = (
        "USER           PID  PPID     VSZ    RSS WCHAN            ADDR S NAME\n"
        "u0_a234       4321   612 1000000  90000 0                   0 S com.example.app\n"
        "u0_a234       5000   612 1000000  40000 0                   0 S com.example.app:remote\n"
        "u0_a500       4400   612 1000000  40000 0                   0 S com.example.application\n"
    )
    adb = FakeAdb({("shell", "ps", "-A"): (0, ps, "")})

    assert app_pids(adb, PACKAGE) == {4321, 5000}
    assert adb.calls == [["shell", "ps", "-A"]]


def test_app_pids_falls_back_to_plain_ps_on_older_devices():
    ps = "USER     PID   PPID  VSIZE  RSS     WCHAN    PC        NAME\nu0_a234   4321  190   900000 50000 ffffffff 00000000 S com.example.app\n"
    adb = FakeAdb({("shell", "ps", "-A"): (0, "USER     PID   PPID  VSIZE  RSS     WCHAN    PC        NAME\n", ""), ("shell", "ps"): (0, ps, "")})

    assert app_pids(adb, PACKAGE) == {4321}


def test_app_pids_is_empty_when_ps_fails():
    adb = FakeAdb({("shell", "ps", "-A"): (1, "", "error"), ("shell", "ps"): (1, "", "error")})

    assert app_pids(adb, PACKAGE) == set()


def test_clear_logcat_reports_why_the_buffers_were_not_cleared():
    assert clear_logcat(FakeAdb()) is None
    assert clear_logcat(FakeAdb({("logcat", "-c"): (1, "", "failed to clear the 'main' log")})) == "failed to clear the 'main' log"


def test_start_logcat_streams_with_uids_when_supported(tmp_path):
    process = FakeProcess()
    adb = FakeAdb(started=[(process, b"")])

    capture = start_logcat(adb, tmp_path / "logcat.txt")

    assert capture == LogcatCapture(process, tmp_path / "logcat.txt", True)
    assert adb.start_calls == [["logcat", "-v", "threadtime", "-v", "uid"]]


def test_start_logcat_restarts_without_uids_when_logcat_rejects_the_adverb(tmp_path):
    process = FakeProcess()
    adb = FakeAdb(started=[(FakeProcess(exit_code=1), b"Invalid parameter -v uid\n"), (process, b"10-21 12:00:00.100  4321  4330 D A: b\n")])

    capture = start_logcat(adb, tmp_path / "logcat.txt")

    assert capture.with_uid is False
    assert adb.start_calls == [["logcat", "-v", "threadtime", "-v", "uid"], ["logcat", "-v", "threadtime"]]
    assert "Invalid parameter" not in capture.path.read_text()


def test_start_logcat_fails_with_both_attempts_described(tmp_path):
    adb = FakeAdb(started=[(FakeProcess(exit_code=1), b"error: no devices/emulators found\n"), (FakeProcess(exit_code=1), b"")])

    with pytest.raises(LogcatError) as raised:
        start_logcat(adb, tmp_path / "logcat.txt")

    assert "logcat -v uid exited 1: error: no devices/emulators found" in str(raised.value)
    assert "logcat exited 1: no output" in str(raised.value)


def test_start_logcat_reports_a_missing_adb(tmp_path):
    adb = FakeAdb()
    adb.start_to_file = lambda args, destination: (_ for _ in ()).throw(FileNotFoundError("adb"))

    with pytest.raises(LogcatError, match="adb not found on PATH"):
        start_logcat(adb, tmp_path / "logcat.txt")


def test_stop_terminates_a_running_capture(tmp_path):
    process = FakeProcess()

    assert LogcatCapture(process, tmp_path / "logcat.txt", True).stop() is True
    assert process.terminated and not process.killed


def test_stop_kills_a_capture_that_ignores_terminate(tmp_path):
    process = FakeProcess(stubborn=True)

    assert LogcatCapture(process, tmp_path / "logcat.txt", True).stop(timeout=0) is True
    assert process.killed


def test_stop_reports_a_capture_that_ended_early(tmp_path):
    process = FakeProcess(exit_code=255)

    assert LogcatCapture(process, tmp_path / "logcat.txt", True).stop() is False
    assert not process.terminated
