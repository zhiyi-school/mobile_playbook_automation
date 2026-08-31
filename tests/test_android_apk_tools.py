from __future__ import annotations

import subprocess

import pytest

from mobile_playbook.platforms.android.apk_tools import inspect_apk_metadata


def test_inspect_apk_metadata_parses_aapt_badging(tmp_path, monkeypatch):
    apk = tmp_path / "app.apk"
    apk.write_bytes(b"apk")

    def fake_run(command, capture_output, text, check):
        assert command[:3] == ["aapt", "dump", "badging"]
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=(
                "package: name='com.example.app' versionCode='42' versionName='1.2.3'\n"
                "application-label:'Example App'\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert inspect_apk_metadata(apk) == {
        "package_name": "com.example.app",
        "version_code": "42",
        "version_name": "1.2.3",
        "display_name": "Example App",
    }


def test_inspect_apk_metadata_reports_missing_sdk_tool(tmp_path, monkeypatch):
    apk = tmp_path / "app.apk"
    apk.write_bytes(b"apk")

    def fake_run(*args, **kwargs):
        raise FileNotFoundError("missing")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="aapt"):
        inspect_apk_metadata(apk)
