from __future__ import annotations

from types import SimpleNamespace

from mobile_playbook.platforms.ios.ipa.mutability import detect_macho_encryption


def test_inspect_main_executable_detects_non_encrypted_executable_when_mocked(monkeypatch, tmp_path):
    exe = tmp_path / "AppExec"
    exe.write_bytes(b"macho")
    monkeypatch.setattr("subprocess.run", lambda *a, **k: SimpleNamespace(returncode=0, stdout="          cryptid 0\n", stderr=""))
    result = detect_macho_encryption(exe)
    assert result.status == "MUTABLE_AS_PROVIDED"


def test_inspect_main_executable_detects_protected_encrypted_executable_when_mocked(monkeypatch, tmp_path):
    exe = tmp_path / "AppExec"
    exe.write_bytes(b"macho")
    monkeypatch.setattr("subprocess.run", lambda *a, **k: SimpleNamespace(returncode=0, stdout="          cryptid 1\n", stderr=""))
    result = detect_macho_encryption(exe)
    assert result.status == "PROTECTED_OR_ENCRYPTED_BINARY"
