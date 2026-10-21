from __future__ import annotations

import subprocess

from mobile_playbook.platforms.android.device import adb as adb_module
from mobile_playbook.platforms.android.device.adb import AdbClient

PAYLOAD = b"ustar\x00\r\n\x00\xff binary \n\r"


def test_run_to_file_writes_stdout_unchanged_and_targets_the_serial(tmp_path, monkeypatch):
    calls = []

    def fake_run(command, stdout, stderr, timeout):
        calls.append((command, timeout))
        stdout.write(PAYLOAD)
        return subprocess.CompletedProcess(command, 1, stderr=b"tar: socket ignored\n")

    monkeypatch.setattr(adb_module.subprocess, "run", fake_run)

    code, err = AdbClient("adb", "SERIAL1").run_to_file(["shell", "-T", "cat", "x"], tmp_path / "out.bin", timeout=9)

    assert (code, err) == (1, "tar: socket ignored")
    assert (tmp_path / "out.bin").read_bytes() == PAYLOAD
    assert calls == [(["adb", "-s", "SERIAL1", "shell", "-T", "cat", "x"], 9)]


def test_run_to_file_reports_a_missing_adb_and_a_timeout_as_run_does(tmp_path, monkeypatch):
    def missing(command, **kwargs):
        raise FileNotFoundError(command[0])

    monkeypatch.setattr(adb_module.subprocess, "run", missing)
    assert AdbClient("adb").run_to_file(["shell", "true"], tmp_path / "out.bin") == (127, "adb not found on PATH")

    def slow(command, **kwargs):
        raise subprocess.TimeoutExpired(command, 5)

    monkeypatch.setattr(adb_module.subprocess, "run", slow)
    code, err = AdbClient("adb").run_to_file(["shell", "true"], tmp_path / "out.bin", timeout=5)
    assert code == 124
    assert err.startswith("adb timed out after 5s")


def test_start_to_file_appends_output_and_targets_the_serial(tmp_path, monkeypatch):
    calls = []

    def fake_popen(command, stdout, stderr, stdin):
        calls.append((command, stderr, stdin))
        stdout.write(b"line\n")
        return "process"

    monkeypatch.setattr(adb_module.subprocess, "Popen", fake_popen)
    destination = tmp_path / "logcat.txt"
    destination.write_bytes(b"earlier\n")

    assert AdbClient("adb", "SERIAL1").start_to_file(["logcat", "-v", "threadtime"], destination) == "process"
    assert destination.read_bytes() == b"earlier\nline\n"
    assert calls == [(["adb", "-s", "SERIAL1", "logcat", "-v", "threadtime"], subprocess.STDOUT, subprocess.DEVNULL)]
