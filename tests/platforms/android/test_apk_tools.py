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


XMLTREE = """N: android=http://schemas.android.com/apk/res/android (line=2)
  E: adaptive-icon (line=2)
      E: background (line=3)
        A: http://schemas.android.com/apk/res/android:drawable(0x01010199)=@0x7f0e0001
      E: foreground (line=4)
        E: inset (line=5)
          A: http://schemas.android.com/apk/res/android:drawable(0x01010199)=@0x7F0E0002
      E: monochrome (line=6)
        A: http://schemas.android.com/apk/res/android:drawable(0x01010199)=@0x7f0e0009
"""

RESOURCES = """Binary APK
Package name=com.example.placeholder id=7f
  type color id=05 entryCount=2
    resource 0x7f050001 color/ic_launcher_background
      () #ff2b5797
  type mipmap id=0e entryCount=3
    resource 0x7f0e0001 mipmap/ic_launcher_background
      (mdpi) (file) res/mipmap-mdpi-v4/ic_launcher_background.png type=PNG
      (xxxhdpi) (file) res/mipmap-xxxhdpi-v4/ic_launcher_background.png type=PNG
    resource 0x7f0e0002 mipmap/ic_launcher_foreground
      (xxxhdpi) (file) res/mipmap-xxxhdpi-v4/ic_launcher_foreground.png type=PNG
      (anydpi-v26) (file) res/drawable/ic_launcher_foreground.xml type=XML
"""


def test_adaptive_icon_layers_resolve_each_layer_to_its_files(monkeypatch, tmp_path):
    from mobile_playbook.platforms.android import apk_tools

    calls = []

    def fake_run(commands):
        calls.append(commands[0])
        return XMLTREE if commands[0][2] == "xmltree" else RESOURCES

    monkeypatch.setattr(apk_tools, "_run_first_available", fake_run)

    layers = apk_tools.adaptive_icon_layers(tmp_path / "app.apk", "res/mipmap-anydpi-v26/ic_launcher.xml")

    assert layers == {
        "background": {
            "files": ["res/mipmap-mdpi-v4/ic_launcher_background.png", "res/mipmap-xxxhdpi-v4/ic_launcher_background.png"],
            "color": None,
        },
        "foreground": {"files": ["res/mipmap-xxxhdpi-v4/ic_launcher_foreground.png"], "color": None},
    }
    assert calls[0][:5] == ["aapt2", "dump", "xmltree", "--file", "res/mipmap-anydpi-v26/ic_launcher.xml"]


def test_a_colour_background_is_returned_as_its_value(monkeypatch, tmp_path):
    from mobile_playbook.platforms.android import apk_tools

    tree = XMLTREE.replace("@0x7f0e0001", "@0x7f050001")
    monkeypatch.setattr(
        apk_tools, "_run_first_available", lambda commands: tree if commands[0][2] == "xmltree" else RESOURCES
    )

    layers = apk_tools.adaptive_icon_layers(tmp_path / "app.apk", "res/mipmap-anydpi-v26/ic_launcher.xml")

    assert layers["background"] == {"files": [], "color": "#ff2b5797"}


@pytest.mark.parametrize(
    "tree",
    [
        XMLTREE.replace("E: background", "E: other"),
        XMLTREE.replace("@0x7f0e0002", "@0x7f0e0042").replace("@0x7F0E0002", "@0x7f0e0042"),
    ],
)
def test_layers_that_are_not_plain_resources_are_not_guessed(monkeypatch, tmp_path, tree):
    from mobile_playbook.platforms.android import apk_tools

    monkeypatch.setattr(
        apk_tools, "_run_first_available", lambda commands: tree if commands[0][2] == "xmltree" else RESOURCES
    )

    assert apk_tools.adaptive_icon_layers(tmp_path / "app.apk", "res/mipmap-anydpi-v26/ic_launcher.xml") is None


def test_without_aapt2_the_layers_are_unknown(monkeypatch, tmp_path):
    from mobile_playbook.platforms.android import apk_tools

    def unavailable(commands):
        raise RuntimeError("aapt2 not on PATH")

    monkeypatch.setattr(apk_tools, "_run_first_available", unavailable)

    assert apk_tools.adaptive_icon_layers(tmp_path / "app.apk", "res/mipmap-anydpi-v26/ic_launcher.xml") is None
