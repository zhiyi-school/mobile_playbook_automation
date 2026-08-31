from __future__ import annotations

import subprocess

import pytest

from mobile_playbook.platforms.android.apk_tools import icon_resource_paths, inspect_apk_metadata


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


def test_icon_resource_paths_are_returned_densest_first(tmp_path, monkeypatch):
    apk = tmp_path / "app.apk"
    apk.write_bytes(b"apk")

    def fake_run(command, capture_output, text, check):
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=(
                "package: name='com.example.placeholder'\n"
                "application-icon-160:'res/mipmap-mdpi-v4/ic_launcher.png'\n"
                "application-icon-640:'res/mipmap-xxxhdpi-v4/ic_launcher.png'\n"
                "application: label='Example App' icon='res/mipmap-anydpi-v26/ic_launcher.xml'\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert icon_resource_paths(apk) == [
        "res/mipmap-xxxhdpi-v4/ic_launcher.png",
        "res/mipmap-mdpi-v4/ic_launcher.png",
        "res/mipmap-anydpi-v26/ic_launcher.xml",
    ]


def test_icon_resource_paths_are_empty_without_sdk_tools(tmp_path, monkeypatch):
    apk = tmp_path / "app.apk"
    apk.write_bytes(b"apk")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("missing")))

    assert icon_resource_paths(apk) == []
