from __future__ import annotations

import plistlib
import subprocess
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mobile_playbook.platforms.ios import keyboard_resign

VERIFICATION_ERROR = (
    "Cannot install the com.example.LocalKeyboard.4228qcqtj9 application because it could not be "
    "verified. ApplicationVerificationFailed: Failed to verify code signature"
)


def _ipa_with_profile(path: Path, expiry: datetime | None, *, name: str = "Payload/LocalKeyboard.app") -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(f"{name}/Info.plist", b"placeholder")
        if expiry is not None:
            zf.writestr(f"{name}/embedded.mobileprovision", plistlib.dumps({"ExpirationDate": expiry}))
    return path


@pytest.fixture
def decoded_profile(monkeypatch):
    """`security cms -D` is a macOS tool; stand in for it by returning the stored plist bytes."""

    def fake_run(command, **kwargs):
        if command[:2] == ["security", "cms"]:
            return subprocess.CompletedProcess(command, 0, stdout=Path(command[-1]).read_bytes(), stderr=b"")
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(keyboard_resign.subprocess, "run", fake_run)


def test_a_profile_that_has_already_expired_is_reported_as_expired(tmp_path, decoded_profile):
    ipa = _ipa_with_profile(tmp_path / "LocalKeyboard.ipa", datetime.now(timezone.utc) - timedelta(days=2))

    assert keyboard_resign.signature_expired(ipa) is True


def test_a_profile_expiring_inside_the_margin_is_reported_as_expired(tmp_path, decoded_profile):
    ipa = _ipa_with_profile(tmp_path / "LocalKeyboard.ipa", datetime.now(timezone.utc) + timedelta(minutes=30))

    assert keyboard_resign.signature_expired(ipa) is True
    assert keyboard_resign.signature_expired(ipa, margin_seconds=60) is False


def test_a_profile_with_days_left_is_not_reported_as_expired(tmp_path, decoded_profile):
    ipa = _ipa_with_profile(tmp_path / "LocalKeyboard.ipa", datetime.now(timezone.utc) + timedelta(days=5))

    assert keyboard_resign.signature_expired(ipa) is False


def test_a_naive_expiry_date_is_read_as_utc(tmp_path, decoded_profile):
    naive_now = datetime.now(timezone.utc).replace(tzinfo=None)
    past = _ipa_with_profile(tmp_path / "past.ipa", naive_now - timedelta(days=2))
    future = _ipa_with_profile(tmp_path / "future.ipa", naive_now + timedelta(days=5))

    assert keyboard_resign.signature_expired(past) is True
    assert keyboard_resign.signature_expired(future) is False


def test_the_soonest_expiry_decides_when_an_ipa_carries_several_profiles(tmp_path, decoded_profile):
    ipa = tmp_path / "LocalKeyboard.ipa"
    with zipfile.ZipFile(ipa, "w") as zf:
        zf.writestr(
            "Payload/LocalKeyboard.app/embedded.mobileprovision",
            plistlib.dumps({"ExpirationDate": datetime.now(timezone.utc) + timedelta(days=5)}),
        )
        zf.writestr(
            "Payload/LocalKeyboard.app/PlugIns/KeyboardExtension.appex/embedded.mobileprovision",
            plistlib.dumps({"ExpirationDate": datetime.now(timezone.utc) - timedelta(days=1)}),
        )

    assert keyboard_resign.signature_expired(ipa) is True


def test_an_ipa_with_no_profile_at_all_is_left_alone(tmp_path, decoded_profile):
    ipa = _ipa_with_profile(tmp_path / "LocalKeyboard.ipa", None)

    assert keyboard_resign.signature_expired(ipa) is False


def test_a_profile_that_cannot_be_decoded_is_left_alone(tmp_path, monkeypatch):
    ipa = _ipa_with_profile(tmp_path / "LocalKeyboard.ipa", datetime.now(timezone.utc) - timedelta(days=2))
    monkeypatch.setattr(
        keyboard_resign.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 1, stdout=b"", stderr=b"not a cms blob"),
    )

    assert keyboard_resign.signature_expired(ipa) is False


def test_the_real_install_rejection_is_recognised_as_a_verification_failure():
    assert keyboard_resign.is_verification_failure([VERIFICATION_ERROR]) is True


@pytest.mark.parametrize(
    "errors",
    [
        ["ApplicationVerificationFailed"],
        ["Failed to verify code signature"],
        ["something else", "Failed to verify code signature"],
    ],
)
def test_either_marker_on_its_own_counts(errors):
    assert keyboard_resign.is_verification_failure(errors) is True


@pytest.mark.parametrize(
    "errors",
    [[], ["Device is locked"], ["No such file or directory"], ["application verification failed"]],
)
def test_an_unrelated_install_failure_is_not_a_verification_failure(errors):
    assert keyboard_resign.is_verification_failure(errors) is False


def test_a_missing_resign_script_is_reported_rather_than_run(tmp_path, monkeypatch):
    monkeypatch.setattr(keyboard_resign, "RESIGN_SCRIPT", "tools/absent/resign.py")
    monkeypatch.setattr(
        keyboard_resign.subprocess, "run", lambda *args, **kwargs: pytest.fail("should not run a missing script")
    )

    result = keyboard_resign.resign(tmp_path / "LocalKeyboard.ipa", "udid", "TEAMID")

    assert result.status == "RESIGN_UNAVAILABLE"
    assert "not found" in result.errors[0]


def test_a_successful_run_reports_resigned_and_passes_the_ipa_explicitly(tmp_path, monkeypatch):
    seen: dict = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        seen["cwd"] = kwargs.get("cwd")
        seen["timeout"] = kwargs.get("timeout")
        return subprocess.CompletedProcess(command, 0, stdout="resigned", stderr="")

    monkeypatch.setattr(keyboard_resign.subprocess, "run", fake_run)
    ipa = tmp_path / "LocalKeyboard.ipa"

    result = keyboard_resign.resign(ipa, "device-udid", "TEAMID", timeout_seconds=120)

    assert result == keyboard_resign.ResignResult(status="RESIGNED", errors=[])
    assert seen["command"][2:] == ["--ipa", str(ipa), "--udid", "device-udid", "--team-id", "TEAMID"]
    assert seen["timeout"] == 120
    assert (Path(seen["cwd"]) / "tools" / "localkeyboard_resign" / "resign.py").exists()


def test_a_failing_run_reports_its_stderr(tmp_path, monkeypatch):
    monkeypatch.setattr(
        keyboard_resign.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 1, stdout="", stderr="  no signing identity  "),
    )

    result = keyboard_resign.resign(tmp_path / "LocalKeyboard.ipa", "udid", "TEAMID")

    assert result.status == "RESIGN_FAILED"
    assert result.errors == ["no signing identity"]


def test_a_failing_run_falls_back_to_stdout_when_stderr_is_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(
        keyboard_resign.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 2, stdout="xcodegen missing", stderr=""),
    )

    result = keyboard_resign.resign(tmp_path / "LocalKeyboard.ipa", "udid", "TEAMID")

    assert result.status == "RESIGN_FAILED"
    assert result.errors == ["xcodegen missing"]


def test_a_run_that_overruns_is_reported_as_a_timeout(tmp_path, monkeypatch):
    def fake_run(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs.get("timeout"))

    monkeypatch.setattr(keyboard_resign.subprocess, "run", fake_run)

    result = keyboard_resign.resign(tmp_path / "LocalKeyboard.ipa", "udid", "TEAMID", timeout_seconds=30)

    assert result.status == "RESIGN_TIMED_OUT"
    assert result.errors == ["resign.py exceeded 30s"]
