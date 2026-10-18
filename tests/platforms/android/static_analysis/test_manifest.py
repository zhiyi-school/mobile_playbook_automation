from __future__ import annotations

import pytest

from mobile_playbook.platforms.android.static_analysis.manifest import (
    declares_play_services,
    parse_manifest_tree,
    unprotected_exported_components,
)

AAPT_TREE = """N: android=http://schemas.android.com/apk/res/android
  E: manifest (line=2)
    A: android:versionCode(0x0101021b)=(type 0x10)0x7
    A: android:versionName(0x0101021c)="1.2.3" (Raw: "1.2.3")
    A: package="com.example.app" (Raw: "com.example.app")
    E: uses-sdk (line=7)
      A: android:minSdkVersion(0x0101020c)=(type 0x10)0x18
      A: android:targetSdkVersion(0x01010270)=(type 0x10)0x22
    E: uses-permission (line=9)
      A: android:name(0x01010003)="android.permission.INTERNET" (Raw: "android.permission.INTERNET")
    E: uses-permission (line=10)
      A: android:name(0x01010003)="android.permission.CAMERA" (Raw: "android.permission.CAMERA")
    E: application (line=12)
      A: android:name(0x01010003)="com.example.app.App" (Raw: "com.example.app.App")
      A: android:debuggable(0x0101000f)=(type 0x12)0xffffffff
      A: android:allowBackup(0x01010280)=(type 0x12)0x0
      A: android:networkSecurityConfig(0x01010527)=@0x7f170003
      E: activity (line=14)
        A: android:name(0x01010003)="com.example.app.MainActivity" (Raw: "com.example.app.MainActivity")
        A: android:exported(0x01010010)=(type 0x12)0xffffffff
        E: intent-filter (line=16)
          E: action (line=17)
            A: android:name(0x01010003)="android.intent.action.MAIN" (Raw: "android.intent.action.MAIN")
          E: category (line=18)
            A: android:name(0x01010003)="android.intent.category.LAUNCHER" (Raw: "android.intent.category.LAUNCHER")
      E: activity (line=20)
        A: android:name(0x01010003)="com.example.app.DeepLinkActivity" (Raw: "com.example.app.DeepLinkActivity")
        A: android:exported(0x01010010)=(type 0x12)0xffffffff
      E: service (line=22)
        A: android:name(0x01010003)="com.example.app.SyncService" (Raw: "com.example.app.SyncService")
        A: android:exported(0x01010010)=(type 0x12)0xffffffff
        A: android:permission(0x01010006)="com.example.app.SYNC" (Raw: "com.example.app.SYNC")
      E: receiver (line=24)
        A: android:name(0x01010003)="com.example.app.LegacyReceiver" (Raw: "com.example.app.LegacyReceiver")
        E: intent-filter (line=25)
          E: action (line=26)
            A: android:name(0x01010003)="com.example.app.PING" (Raw: "com.example.app.PING")
      E: provider (line=28)
        A: android:name(0x01010003)="com.example.app.Files" (Raw: "com.example.app.Files")
"""

AAPT2_TREE = """N: android=http://schemas.android.com/apk/res/android (line=2)
  E: manifest (line=2)
    A: http://schemas.android.com/apk/res/android:versionCode(0x0101021b)=7
    A: http://schemas.android.com/apk/res/android:versionName(0x0101021c)="1.2.3" (Raw: "1.2.3")
    A: package="com.example.app" (Raw: "com.example.app")
    E: uses-sdk (line=7)
      A: http://schemas.android.com/apk/res/android:minSdkVersion(0x0101020c)=24
      A: http://schemas.android.com/apk/res/android:targetSdkVersion(0x01010270)=34
    E: application (line=12)
      A: http://schemas.android.com/apk/res/android:usesCleartextTraffic(0x010104ec)=true
      E: activity (line=20)
        A: http://schemas.android.com/apk/res/android:name(0x01010003)="com.example.app.DeepLinkActivity" (Raw: "com.example.app.DeepLinkActivity")
        A: http://schemas.android.com/apk/res/android:exported(0x01010010)=false
"""


def test_aapt_tree_is_parsed_into_the_manifest_summary():
    manifest = parse_manifest_tree(AAPT_TREE)

    assert manifest["package_name"] == "com.example.app"
    assert (manifest["version_code"], manifest["version_name"]) == ("7", "1.2.3")
    assert (manifest["min_sdk"], manifest["target_sdk"]) == (24, 34)
    assert manifest["permissions"] == ["android.permission.CAMERA", "android.permission.INTERNET"]
    assert manifest["dangerous_permissions"] == ["android.permission.CAMERA"]
    assert manifest["application"] == {
        "debuggable": True,
        "allow_backup": False,
        "uses_cleartext_traffic": None,
        "network_security_config": True,
    }


def test_only_unguarded_non_launcher_exported_components_are_counted():
    manifest = parse_manifest_tree(AAPT_TREE)
    components = {item["name"]: item for item in manifest["components"]}

    assert components["com.example.app.MainActivity"]["launcher"] is True
    assert components["com.example.app.SyncService"]["permission"] == "com.example.app.SYNC"
    # No android:exported but an intent filter: exported by the pre-API 31 default.
    assert components["com.example.app.LegacyReceiver"]["exported"] is True
    # Providers stopped being exported by default at API 17.
    assert components["com.example.app.Files"]["exported"] is False
    assert [item["name"] for item in unprotected_exported_components(manifest["components"])] == [
        "com.example.app.DeepLinkActivity",
        "com.example.app.LegacyReceiver",
    ]
    assert manifest["exported_counts"] == {"activity": 1, "activity-alias": 0, "service": 0, "receiver": 1, "provider": 0}


def test_aapt2_literals_parse_like_aapt_typed_values():
    manifest = parse_manifest_tree(AAPT2_TREE)

    assert (manifest["version_code"], manifest["min_sdk"], manifest["target_sdk"]) == ("7", 24, 34)
    assert manifest["application"]["uses_cleartext_traffic"] is True
    assert manifest["components"][0]["exported"] is False


def test_a_dump_without_a_manifest_is_rejected():
    with pytest.raises(ValueError):
        parse_manifest_tree("N: android=http://schemas.android.com/apk/res/android\n")


def test_the_play_services_version_metadata_on_the_application_is_detected():
    from tests.platforms.android.manifest_fakes import manifest_tree

    assert declares_play_services(manifest_tree(play_services=True)) is True
    assert declares_play_services(manifest_tree(play_services=False)) is False
    assert declares_play_services(AAPT_TREE) is False


def test_play_services_metadata_is_detected_in_an_aapt2_dump():
    tree = AAPT2_TREE.replace(
        "      E: activity (line=20)\n",
        "      E: meta-data (line=18)\n"
        '        A: http://schemas.android.com/apk/res/android:name(0x01010003)="com.google.android.gms.version" '
        '(Raw: "com.google.android.gms.version")\n'
        "      E: activity (line=20)\n",
    )

    assert declares_play_services(tree) is True


def test_a_dump_without_a_manifest_cannot_say_whether_play_services_are_needed():
    with pytest.raises(ValueError, match="no <manifest>"):
        declares_play_services("N: android=http://schemas.android.com/apk/res/android\n")
