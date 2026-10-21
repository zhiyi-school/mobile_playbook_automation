from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from mobile_playbook.common.storage_paths import REPOSITORY_ROOT, android_intake_dir
from mobile_playbook.platforms.android.device import overlay_attacker
from mobile_playbook.platforms.android.device.install import STAGING_DIR
from mobile_playbook.platforms.android.device.overlay_attacker import (
    ATTACKER_APK_NAME,
    ATTACKER_PACKAGE,
    ATTACKER_VERSION,
    OUTPUT_DIR,
    CapturedCredential,
    OverlayAttacker,
    OverlayAttackerError,
    attacker_apk_path,
)
from mobile_playbook.platforms.android.preflight import check_requirements
from mobile_playbook.platforms.android.risks import get_risk
from tests.platforms.android.install_helpers import answer_apk_copy, is_pm_install


class AttackerAdb:
    """Fake adb backed by a dict of the attacker app's output files, readable by cat or only by run-as."""

    def __init__(self, version=None, files=None, on_command=None, install_result=(0, "Success", ""), run_as_only=False):
        self.version = version
        self.files = dict(files or {})
        self.on_command = on_command
        self.install_result = install_result
        self.run_as_only = run_as_only
        self.commands: list[list[str]] = []
        self.copied: dict[str, int] = {}

    def run(self, args, timeout=None):
        self.commands.append(list(args))
        if self.on_command:
            self.on_command(self, args)
        if args[:3] == ["shell", "dumpsys", "package"]:
            return (0, f"  versionCode=1\n  versionName={self.version}\n", "") if self.version else (0, "", "")
        if copy := answer_apk_copy(args, self.copied):
            return copy
        if is_pm_install(args):
            return self.install_result
        if args[:3] == ["shell", "rm", "-f"]:
            for path in args[3:]:
                self.files.pop(path.rsplit("/", 1)[1], None)
            return 0, "", ""
        if args[:2] == ["shell", "cat"] and self.run_as_only:
            return 1, "", "Permission denied"
        if args[:2] == ["shell", "cat"] or args[:4] == ["shell", "run-as", ATTACKER_PACKAGE, "cat"]:
            name = args[-1].rsplit("/", 1)[1]
            if name not in self.files:
                return 1, "", "No such file or directory"
            content = self.files[name]
            return 0, content if isinstance(content, str) else json.dumps(content), ""
        return 0, "", ""


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(overlay_attacker, "POLL_SECONDS", 0)


def starts_service(adb, args):
    if args[:3] == ["shell", "am", "start"]:
        adb.files["status.json"] = {"state": "service_started", "detail": "", "overlay_shown_count": 0}


def test_the_default_apk_is_the_intake_copy():
    assert attacker_apk_path() == android_intake_dir() / ATTACKER_APK_NAME


def test_a_relative_configured_apk_resolves_against_the_repository_root(tmp_path):
    assert attacker_apk_path("artifacts/other.apk") == REPOSITORY_ROOT / "artifacts/other.apk"
    assert attacker_apk_path(str(tmp_path / "a.apk")) == tmp_path / "a.apk"


def test_the_current_attacker_app_is_not_reinstalled(tmp_path):
    adb = AttackerAdb(version=ATTACKER_VERSION)

    OverlayAttacker(adb, tmp_path / "missing.apk").ensure_installed()

    assert not any(is_pm_install(command) for command in adb.commands)


def test_a_missing_attacker_app_is_installed_without_granting_permissions(tmp_path):
    apk = tmp_path / "attacker.apk"
    apk.write_bytes(b"apk")
    adb = AttackerAdb(version="0")

    OverlayAttacker(adb, apk).ensure_installed()

    assert ["shell", "pm", "install", "-r", f"{STAGING_DIR}/0.apk"] in adb.commands


def test_an_absent_apk_is_reported_with_its_path(tmp_path):
    with pytest.raises(OverlayAttackerError, match="missing.apk"):
        OverlayAttacker(AttackerAdb(), tmp_path / "missing.apk").ensure_installed()


def test_a_failed_install_is_reported(tmp_path):
    apk = tmp_path / "attacker.apk"
    apk.write_bytes(b"apk")
    adb = AttackerAdb(install_result=(1, "Failure [INSTALL_FAILED_UPDATE_INCOMPATIBLE]", ""))

    with pytest.raises(OverlayAttackerError, match="INSTALL_FAILED_UPDATE_INCOMPATIBLE"):
        OverlayAttacker(adb, apk).ensure_installed()


def test_start_clears_old_output_and_passes_the_quoted_target_label(tmp_path):
    adb = AttackerAdb(on_command=starts_service)

    status = OverlayAttacker(adb, tmp_path / "a.apk").start("Demo Bank's App", timeout=1)

    assert status.state == "service_started"
    assert adb.commands[0] == ["shell", "rm", "-f", f"{OUTPUT_DIR}/status.json", f"{OUTPUT_DIR}/captured.json"]
    assert adb.commands[1] == [
        "shell", "am", "start", "-S", "-n", f"{ATTACKER_PACKAGE}/.MainActivity",
        "--es", "target_label", "'Demo Bank'\"'\"'s App'", "--ez", "reset", "true",
    ]


def test_start_does_not_accept_a_status_left_by_an_earlier_session(tmp_path):
    adb = AttackerAdb(files={"status.json": {"state": "service_started"}})

    with pytest.raises(OverlayAttackerError, match="no status"):
        OverlayAttacker(adb, tmp_path / "a.apk").start("Demo", timeout=0)


def test_start_returns_permission_missing_before_the_overlay_is_allowed(tmp_path):
    def opens_without_permission(adb, args):
        if args[:3] == ["shell", "am", "start"]:
            adb.files["status.json"] = {"state": "permission_missing", "detail": "Display over other apps is off"}

    status = OverlayAttacker(AttackerAdb(on_command=opens_without_permission), tmp_path / "a.apk").start("Demo", timeout=1)

    assert status.state == "permission_missing"
    assert status.detail == "Display over other apps is off"


def test_status_reports_how_often_the_overlay_was_added(tmp_path):
    adb = AttackerAdb(files={"status.json": {
        "state": "overlay_shown", "detail": "", "updated_at": "2026-10-21T03:00:01Z",
        "overlay_shown_count": 2, "last_overlay_shown_at": "2026-10-21T03:00:01Z",
    }})

    status = OverlayAttacker(adb, tmp_path / "a.apk").status()

    assert (status.state, status.overlay_shown_count, status.last_overlay_shown_at) == ("overlay_shown", 2, "2026-10-21T03:00:01Z")


def test_a_status_without_a_state_or_with_partial_json_is_not_reported(tmp_path):
    assert OverlayAttacker(AttackerAdb(files={"status.json": {"detail": "x"}}), tmp_path / "a.apk").status() is None
    assert OverlayAttacker(AttackerAdb(files={"status.json": '{"state": "overl'}), tmp_path / "a.apk").status() is None


def test_wait_for_state_names_the_last_state_seen_on_timeout(tmp_path):
    adb = AttackerAdb(files={"status.json": {"state": "service_started"}})

    with pytest.raises(OverlayAttackerError, match="last: service_started"):
        OverlayAttacker(adb, tmp_path / "a.apk").wait_for_state({"captured"}, timeout=0)


def test_captured_returns_the_submissions_in_order(tmp_path):
    adb = AttackerAdb(files={"captured.json": [
        {"timestamp": "2026-10-21T03:00:00Z", "username": "first@example.com", "password": "one"},
        "not a record",
        {"timestamp": "2026-10-21T03:00:05Z", "username": "canary.logtest@example.com", "password": "Canary-Pw-7Q4z"},
    ]})

    records = OverlayAttacker(adb, tmp_path / "a.apk").captured()

    assert records == [
        CapturedCredential("2026-10-21T03:00:00Z", "first@example.com", "one"),
        CapturedCredential("2026-10-21T03:00:05Z", "canary.logtest@example.com", "Canary-Pw-7Q4z"),
    ]


def test_nothing_is_captured_when_the_file_is_absent(tmp_path):
    assert OverlayAttacker(AttackerAdb(), tmp_path / "a.apk").captured() == []


def test_output_is_read_with_run_as_when_shared_storage_is_not_readable(tmp_path):
    adb = AttackerAdb(files={"status.json": {"state": "captured"}}, run_as_only=True)

    status = OverlayAttacker(adb, tmp_path / "a.apk").status()

    assert status.state == "captured"
    assert ["shell", "run-as", ATTACKER_PACKAGE, "cat", f"{OUTPUT_DIR}/status.json"] in adb.commands


def test_stop_force_stops_the_attacker_app(tmp_path):
    adb = AttackerAdb()

    OverlayAttacker(adb, tmp_path / "a.apk").stop()

    assert adb.commands == [["shell", "am", "force-stop", ATTACKER_PACKAGE]]


def _overlay_config(apk):
    return SimpleNamespace(overlay={"attacker_apk": str(apk)})


def test_preflight_passes_with_the_attacker_apk_in_place(tmp_path):
    apk = tmp_path / "attacker.apk"
    apk.write_bytes(b"apk")

    [(name, ok, message)] = check_requirements(["overlay_attacker"], AttackerAdb(), _overlay_config(apk))

    assert (name, ok) == ("overlay_attacker", True)
    assert str(apk) in message


def test_preflight_passes_when_the_device_already_has_the_attacker_app(tmp_path):
    [(_, ok, message)] = check_requirements(["overlay_attacker"], AttackerAdb(version=ATTACKER_VERSION), _overlay_config(tmp_path / "missing.apk"))

    assert ok and "already installed" in message


@pytest.mark.parametrize("version", [None, "0"])
def test_preflight_fails_with_the_apk_path_when_the_attacker_app_is_missing_or_old(tmp_path, version):
    apk = tmp_path / "missing.apk"

    [(_, ok, message)] = check_requirements(["overlay_attacker"], AttackerAdb(version=version), _overlay_config(apk))

    assert not ok
    assert str(apk) in message and "F4_Overlay_Attacker.apk" in message


def test_preflight_without_a_device_checks_only_the_apk(tmp_path):
    [(_, ok, _)] = check_requirements(["overlay_attacker"], None, _overlay_config(tmp_path / "missing.apk"))

    assert not ok


def test_the_overlay_risk_requires_the_attacker_app():
    assert "overlay_attacker" in get_risk("android-feature-04-risk-01").requires
