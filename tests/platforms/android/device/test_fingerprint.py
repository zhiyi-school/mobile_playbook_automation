from __future__ import annotations

from mobile_playbook.platforms.android.device.fingerprint import installed_base_apk_sha256, installer_of

PACKAGE = "com.example.app"
SHA = "d78846589889da4fb56a3d961638c33484ba0f1bfa181d4d20cce2e6bb8b8bb5"


class FakeAdb:
    def __init__(self, responses):
        self.responses = responses
        self.commands: list[list[str]] = []

    def run(self, args, timeout=None):
        self.commands.append(list(args))
        for prefix, response in self.responses.items():
            if tuple(args[: len(prefix)]) == prefix:
                return response
        return 1, "", "unexpected command"


def test_the_base_apk_is_hashed_on_the_device():
    adb = FakeAdb(
        {
            ("shell", "pm", "path"): (0, "package:/data/app/x/base.apk\npackage:/data/app/x/split_config.en.apk\n", ""),
            ("shell", "sha256sum"): (0, f"{SHA}  /data/app/x/base.apk\n", ""),
        }
    )

    assert installed_base_apk_sha256(adb, PACKAGE) == SHA
    assert adb.commands[-1] == ["shell", "sha256sum", "/data/app/x/base.apk"]


def test_no_fingerprint_when_the_app_is_not_installed():
    adb = FakeAdb({("shell", "pm", "path"): (1, "", "")})

    assert installed_base_apk_sha256(adb, PACKAGE) is None
    assert len(adb.commands) == 1


def test_no_fingerprint_when_the_device_cannot_hash():
    adb = FakeAdb(
        {
            ("shell", "pm", "path"): (0, "package:/data/app/x/base.apk\n", ""),
            ("shell", "sha256sum"): (127, "", "sha256sum: not found"),
        }
    )

    assert installed_base_apk_sha256(adb, PACKAGE) is None


def test_the_installer_of_the_exact_package_is_returned():
    listing = f"package:{PACKAGE}2  installer=com.aurora.store\npackage:{PACKAGE}  installer=com.android.vending\n"

    assert installer_of(FakeAdb({("shell", "pm", "list"): (0, listing, "")}), PACKAGE) == "com.android.vending"


def test_an_adb_install_has_no_installer():
    listing = f"package:{PACKAGE}  installer=null\n"

    assert installer_of(FakeAdb({("shell", "pm", "list"): (0, listing, "")}), PACKAGE) is None
