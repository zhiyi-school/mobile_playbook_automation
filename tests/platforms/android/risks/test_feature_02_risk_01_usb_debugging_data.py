from __future__ import annotations

import json
import shlex
from types import SimpleNamespace

import pytest

from mobile_playbook.common.storage_paths import android_work_dir
from mobile_playbook.platforms.android.device_roles import NON_ROOTED, risk_role
from mobile_playbook.platforms.android.risks.feature_02_risk_01_usb_debugging_data import AndroidUsbDebuggingDataRisk, backup_returns_data
from mobile_playbook.platforms.android.device.package_flags import PackageFlags
from tests.platforms.android.private_data_fakes import CIPHERTEXT, DATA, ENCRYPTED_PREFS, PACKAGE, PLAIN_PREFS, TOKEN, make_archive

RUN_TIMESTAMP = "2026-10-20_00-00-00"


class FakeReportWriter:
    run_timestamp = RUN_TIMESTAMP

    def __init__(self, root):
        self.root = root
        self.results = []

    def test_report_dir(self, app_id, risk_id, test_case_id, platform):
        return self.root

    def write_result(self, result, report_dir):
        self.results.append(result)


class UsbAdb:
    """A device over USB debugging without root: dumpsys reports `flags`, and run-as works only when `run_as` is true."""

    def __init__(self, tar: bytes = b"", *, flags="HAS_CODE ALLOW_CLEAR_USER_DATA", target_sdk=34, device_sdk="34", run_as=True, du_kb=8, installed=True):
        self.tar = tar
        self.flags = flags
        self.target_sdk = target_sdk
        self.device_sdk = device_sdk
        self.run_as = run_as
        self.du_kb = du_kb
        self.installed = installed
        self.calls: list[list[str]] = []

    def run(self, args, timeout=None):
        self.calls.append(args)
        if args == ["shell", "dumpsys", "package", PACKAGE]:
            if not self.installed:
                return 0, "", ""
            return 0, f"Packages:\n  Package [{PACKAGE}] (1a2b):\n    versionCode=1 minSdk=24 targetSdk={self.target_sdk}\n    flags=[ {self.flags} ]\n", ""
        if args == ["shell", "getprop", "ro.build.version.sdk"]:
            return 0, self.device_sdk, ""
        if args == ["shell", "run-as", PACKAGE, "id", "-u"]:
            return (0, "10234\n", "") if self.run_as else (1, "", f"run-as: package not debuggable: {PACKAGE}")
        if args[:5] == ["shell", "run-as", PACKAGE, "sh", "-c"]:
            return self._run_as(shlex.split(args[5])[0])
        if args[:2] == ["shell", "monkey"]:
            return 0, "Events injected: 1", ""
        if args[:3] == ["shell", "am", "force-stop"]:
            return 0, "", ""
        return 1, "", ""

    def _run_as(self, command):
        if command == f"test -d /data/data/{PACKAGE}":
            return 0, "", ""
        if command.startswith("test -d"):
            return 1, "", ""
        if command.startswith("ls -laR"):
            return 0, f"/data/data/{PACKAGE}:\nshared_prefs\n", ""
        if command.startswith("du -sk"):
            return 0, f"{self.du_kb}\t/data/data/{PACKAGE}\n", ""
        return 1, "", "unexpected"

    def run_to_file(self, args, destination, timeout=None):
        self.calls.append(args)
        destination.write_bytes(self.tar)
        return 0, ""


def run_risk(tmp_path, adb, private_data_config=None):
    report_dir = tmp_path / "report"
    report_dir.mkdir()
    app_config = SimpleNamespace(id="example", name="Example", package_name=PACKAGE, risks={})
    global_config = SimpleNamespace(private_data={"launch_wait": 0, **(private_data_config or {})})
    writer = FakeReportWriter(report_dir)
    result = AndroidUsbDebuggingDataRisk().run(app_config, global_config, SimpleNamespace(adb=adb), writer)
    assert writer.results == [result]
    return result


def tar_of(tmp_path, files) -> bytes:
    return make_archive(tmp_path / "device.tar", files).read_bytes()


def critical_flags(tmp_path) -> list[str]:
    return [flag["id"] for flag in json.loads((tmp_path / "report" / "critical_findings.json").read_text())["flags"]]


def test_a_debuggable_app_with_plain_text_secrets_is_at_risk(tmp_path):
    tar = tar_of(tmp_path, {f"{DATA}/shared_prefs/session.xml": f'<map><string name="auth_token">{TOKEN}</string></map>'.encode()})
    adb = UsbAdb(tar, flags="DEBUGGABLE HAS_CODE ALLOW_BACKUP")

    result = run_risk(tmp_path, adb)

    assert (result.final_status, result.verdict, result.errors) == ("PRIVATE_DATA_EXPOSED", "At Risk", [])
    assert result.metadata["run_as"] == {"available": True}
    assert result.metadata["package_flags"]["debuggable"] is True
    assert result.metadata["device_sdk"] == 34
    assert result.metadata["launch"] == {"launched": True}
    assert result.metadata["file_count"] == 1
    assert critical_flags(tmp_path) == ["PRIVATE_DATA_READABLE_VIA_USB_DEBUGGING", "SENSITIVE_INFORMATION_EXPOSURE"]
    assert (tmp_path / "report" / "critical_findings.md").read_text().startswith("# Android USB Debugging Private Data Critical Findings")
    assert {item["kind"] for item in result.evidence} == {"private_data_listing", "private_data_findings", "critical_findings"}
    assert TOKEN not in json.dumps(result.to_dict())
    assert not any(path.suffix == ".tar" for path in (tmp_path / "report").iterdir())
    assert not (android_work_dir() / "private_data" / PACKAGE.replace(".", "_") / f"{RUN_TIMESTAMP}_run_as.tar").exists()
    assert ["shell", "-T", "run-as", PACKAGE, "tar", "-cf", "-", "-C", "/", f"data/data/{PACKAGE}"] in adb.calls


def test_a_debuggable_app_that_encrypts_its_data_is_reduced_risk_with_the_debuggable_flag(tmp_path):
    tar = tar_of(tmp_path, {f"{DATA}/shared_prefs/secure_prefs.xml": ENCRYPTED_PREFS, f"{DATA}/files/pii.json": CIPHERTEXT})

    result = run_risk(tmp_path, UsbAdb(tar, flags="DEBUGGABLE HAS_CODE"))

    assert (result.final_status, result.verdict) == ("PRIVATE_DATA_PROTECTED", "Reduced Risk")
    assert critical_flags(tmp_path) == ["PRIVATE_DATA_READABLE_VIA_USB_DEBUGGING"]


def test_a_debuggable_app_with_nothing_found_is_inconclusive(tmp_path):
    result = run_risk(tmp_path, UsbAdb(tar_of(tmp_path, {f"{DATA}/shared_prefs/settings.xml": PLAIN_PREFS}), flags="DEBUGGABLE"))

    assert (result.final_status, result.verdict) == ("PRIVATE_DATA_NO_FINDINGS", "Inconclusive")


def test_an_app_that_refuses_run_as_and_backups_cannot_be_read(tmp_path):
    adb = UsbAdb(run_as=False)

    result = run_risk(tmp_path, adb)

    assert (result.final_status, result.verdict, result.errors) == ("PRIVATE_DATA_NOT_ACCESSIBLE", "Reduced Risk", [])
    assert result.metadata["run_as"] == {"available": False, "detail": f"run-as: package not debuggable: {PACKAGE}"}
    assert result.metadata["backup_returns_data"] is False
    assert critical_flags(tmp_path) == []
    assert {item["kind"] for item in result.evidence} == {"critical_findings"}
    assert not any(args[:2] == ["shell", "monkey"] for args in adb.calls)


@pytest.mark.parametrize(
    ("target_sdk", "device_sdk", "status"),
    [
        (30, "34", "PRIVATE_DATA_BACKUP_POSSIBLE"),
        (34, "30", "PRIVATE_DATA_BACKUP_POSSIBLE"),
        (34, "", "PRIVATE_DATA_BACKUP_POSSIBLE"),
        (31, "31", "PRIVATE_DATA_NOT_ACCESSIBLE"),
    ],
    ids=["targets-api-30", "device-before-android-12", "device-level-unknown", "android-12-and-target-31"],
)
def test_an_app_that_allows_backup_is_inconclusive_when_a_backup_would_hold_its_data(tmp_path, target_sdk, device_sdk, status):
    result = run_risk(tmp_path, UsbAdb(flags="HAS_CODE ALLOW_BACKUP", target_sdk=target_sdk, device_sdk=device_sdk, run_as=False))

    assert result.final_status == status
    if status == "PRIVATE_DATA_BACKUP_POSSIBLE":
        assert result.verdict == "Inconclusive"
        assert critical_flags(tmp_path) == ["APP_DATA_BACKUP_ALLOWED"]
    else:
        assert result.verdict == "Reduced Risk"
        assert critical_flags(tmp_path) == []


def test_a_debuggable_app_that_run_as_refuses_fails(tmp_path):
    result = run_risk(tmp_path, UsbAdb(flags="DEBUGGABLE HAS_CODE", run_as=False))

    assert (result.final_status, result.verdict) == ("FAILED", "Inconclusive")
    assert result.errors == [f"The app is debuggable, but the device refused run-as: run-as: package not debuggable: {PACKAGE}"]


def test_an_app_that_is_not_installed_fails_before_run_as(tmp_path):
    adb = UsbAdb(installed=False)

    result = run_risk(tmp_path, adb)

    assert result.final_status == "FAILED"
    assert "is it installed" in result.errors[0]
    assert ["shell", "run-as", PACKAGE, "id", "-u"] not in adb.calls


def test_private_data_above_the_size_limit_is_not_copied(tmp_path):
    adb = UsbAdb(flags="DEBUGGABLE", du_kb=3 * 1024)

    result = run_risk(tmp_path, adb, private_data_config={"max_archive_mb": 2})

    assert result.final_status == "FAILED"
    assert "max_archive_mb" in result.errors[0]
    assert not any(args[:2] == ["shell", "-T"] for args in adb.calls)


def test_the_archive_is_kept_only_when_asked(tmp_path):
    result = run_risk(tmp_path, UsbAdb(tar_of(tmp_path, {f"{DATA}/shared_prefs/settings.xml": PLAIN_PREFS}), flags="DEBUGGABLE"), private_data_config={"keep_archive": True})

    kept = android_work_dir() / "private_data" / PACKAGE.replace(".", "_") / f"{RUN_TIMESTAMP}_run_as.tar"
    assert result.metadata["archive"] == str(kept)
    assert kept.is_file()


def test_backup_eligibility_follows_the_android_12_rule():
    assert backup_returns_data(PackageFlags(False, False, 30, ()), 29) is False
    assert backup_returns_data(PackageFlags(False, True, 30, ()), 34) is True
    assert backup_returns_data(PackageFlags(False, True, 31, ()), 34) is False
    assert backup_returns_data(PackageFlags(False, True, None, ()), 34) is True


def test_the_risk_runs_on_the_non_rooted_role():
    assert risk_role(AndroidUsbDebuggingDataRisk()) == NON_ROOTED
    assert AndroidUsbDebuggingDataRisk.risk_id == "android-feature-02-risk-01"
