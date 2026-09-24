from __future__ import annotations

import hashlib
import plistlib
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from mobile_playbook.platforms.ios.models import InstallResult
from mobile_playbook.platforms.ios.risks.feature_01_risk_02 import Feature01Risk02
from mobile_playbook.report import ReportWriter
from tests.conftest import MockDevice

FAKE_CERT = b"fake-developer-cert"
TEST_IDENTITY = hashlib.sha1(FAKE_CERT).hexdigest().upper()


def _profile_bytes(team="TEAM", bundle="com.example.app", udid="udid") -> bytes:
    return plistlib.dumps(
        {
            "Entitlements": {"application-identifier": f"{team}.{bundle}"},
            "TeamIdentifier": [team],
            "ProvisionedDevices": [udid],
            "ExpirationDate": datetime(2999, 1, 1),
            "DeveloperCertificates": [FAKE_CERT],
        }
    )


class RepackDevice(MockDevice):
    def __init__(
        self,
        *,
        baseline_states=(4,),
        repackaged_states=(4,),
        baseline_source="<App><Text>Login</Text></App>",
        repackaged_source="<App><Text>Login</Text></App>",
        button_fails_after_repack=False,
        fail_repackaged_install=False,
    ):
        super().__init__()
        self.baseline_states = list(baseline_states)
        self.repackaged_states = list(repackaged_states)
        self.baseline_source = baseline_source
        self.repackaged_source = repackaged_source
        self.button_fails_after_repack = button_fails_after_repack
        self.fail_repackaged_install = fail_repackaged_install
        self.install_count = 0
        self.repackaged = False
        self._q_index = 0

    def install_app(self, ipa_path: Path, timeout_ms: int) -> InstallResult:
        self.install_count += 1
        if self.install_count >= 2:
            self.repackaged = True
            self._q_index = 0
            if self.fail_repackaged_install:
                return InstallResult(status="INSTALL_FAILED", ipa_path=ipa_path, errors=["repackaged install failed"])
        self.installed.add("com.example.app")
        return InstallResult(status="INSTALLED", ipa_path=ipa_path)

    def query_app_state(self, bundle_id: str) -> int:
        seq = self.repackaged_states if self.repackaged else self.baseline_states
        idx = self._q_index
        self._q_index += 1
        if idx < len(seq):
            return seq[idx]
        return seq[-1] if seq else 4

    def page_source(self) -> str:
        return self.repackaged_source if self.repackaged else self.baseline_source

    def tap_first_button_matching(self, label_contains=None, exclude_label_contains=None, allow_any=False) -> dict:
        if self.repackaged and self.button_fails_after_repack:
            raise RuntimeError("sensitive control is no longer reachable")
        return super().tap_first_button_matching(label_contains, exclude_label_contains, allow_any)


@pytest.fixture
def frida_files(tmp_path):
    dylib = tmp_path / "FridaGadget.dylib"
    dylib.write_bytes(b"\xca\xfe\xba\xbe fake gadget")
    config = tmp_path / "FridaGadget.config"
    config.write_text("{}")
    return dylib, config


def _configure(app, tmp_path, frida_files, *, exercise=None):
    dylib, config = frida_files
    profile = tmp_path / "app.mobileprovision"
    profile.write_bytes(b"fake-mobileprovision")
    app.artifact["workspace_dir"] = str(tmp_path / "acquired")
    app.risks = {
        "ios-feature-01-risk-02": {
            "enabled": True,
            "frida": {"dylib_path": str(dylib), "config_path": str(config)},
            "insert_dylib_path": "insert_dylib",
            "resign": {"identity": TEST_IDENTITY, "provisioning_profile": str(profile)},
            "tamper_markers": ["Tamper detected"],
            "exercise": exercise or {"state_samples": 2, "sample_interval_seconds": 0},
        }
    }


def _fake_subprocess(*, injection_ok=True, codesign_ok=True):
    def run(command, *args, **kwargs):
        tool = Path(str(command[0])).name if command else ""
        if tool == "security":
            return SimpleNamespace(returncode=0, stdout=_profile_bytes(), stderr=b"")
        if tool == "insert_dylib":
            return SimpleNamespace(returncode=0 if injection_ok else 1, stdout="", stderr="insert_dylib error")
        if tool == "codesign":
            return SimpleNamespace(returncode=0 if codesign_ok else 1, stdout="", stderr="codesign error")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return run


def _pass_gadget(monkeypatch):
    monkeypatch.setattr(
        "mobile_playbook.platforms.ios.risks.feature_01_risk_02.confirm_gadget",
        lambda cfg, bundle_id, evidence_dir: {"ok": True, "messages": []},
    )


def _run(app, global_config, device, tmp_path):
    writer = ReportWriter(tmp_path / "reports", "run1")
    return Feature01Risk02().run(app, global_config, device, writer)


def test_survives_when_repackaged_matches_baseline(global_config, tmp_path, frida_files, monkeypatch):
    monkeypatch.setattr("subprocess.run", _fake_subprocess())
    _pass_gadget(monkeypatch)
    app = global_config.apps[0]
    _configure(app, tmp_path, frida_files)
    device = RepackDevice()

    result = _run(app, global_config, device, tmp_path)

    assert result.final_status == "REPACKAGING_SURVIVED"
    assert result.verdict == "At Risk"
    assert result.launch_result["comparison"]["divergences"] == []


def test_survived_diff_but_failed_gadget_attach_is_inconclusive(global_config, tmp_path, frida_files, monkeypatch):
    monkeypatch.setattr("subprocess.run", _fake_subprocess())
    monkeypatch.setattr(
        "mobile_playbook.platforms.ios.risks.feature_01_risk_02.confirm_gadget",
        lambda cfg, bundle_id, evidence_dir: {"ok": False, "errors": ["frida is not installed: no module"]},
    )
    app = global_config.apps[0]
    _configure(app, tmp_path, frida_files)
    device = RepackDevice()

    result = _run(app, global_config, device, tmp_path)

    assert result.final_status == "GADGET_ATTACH_FAILED"
    assert result.verdict == "Inconclusive"


def test_non_blocking_tamper_marker_is_reduced_risk(global_config, tmp_path, frida_files, monkeypatch):
    monkeypatch.setattr("subprocess.run", _fake_subprocess())
    app = global_config.apps[0]
    _configure(app, tmp_path, frida_files)
    device = RepackDevice(repackaged_source="<App><Text>Tamper detected</Text></App>")

    result = _run(app, global_config, device, tmp_path)

    assert result.final_status == "REPACKAGING_BLOCKED"
    assert result.verdict == "Reduced Risk"
    assert any("Tamper" in d for d in result.launch_result["comparison"]["divergences"])


def test_unreachable_sensitive_screen_is_reduced_risk(global_config, tmp_path, frida_files, monkeypatch):
    monkeypatch.setattr("subprocess.run", _fake_subprocess())
    app = global_config.apps[0]
    _configure(app, tmp_path, frida_files, exercise={"state_samples": 2, "sample_interval_seconds": 0, "button_label_contains": ["Account"]})
    device = RepackDevice(button_fails_after_repack=True)

    result = _run(app, global_config, device, tmp_path)

    assert result.final_status == "REPACKAGING_BLOCKED"
    assert result.verdict == "Reduced Risk"
    assert any("Sensitive path" in d for d in result.launch_result["comparison"]["divergences"])


def test_midwindow_termination_is_reduced_risk(global_config, tmp_path, frida_files, monkeypatch):
    monkeypatch.setattr("subprocess.run", _fake_subprocess())
    app = global_config.apps[0]
    _configure(app, tmp_path, frida_files, exercise={"state_samples": 3, "sample_interval_seconds": 0})
    device = RepackDevice(baseline_states=(4, 4, 4), repackaged_states=(4, 1, 1))

    result = _run(app, global_config, device, tmp_path)

    assert result.final_status == "REPACKAGING_BLOCKED"
    assert result.verdict == "Reduced Risk"
    assert any("terminated" in d.lower() for d in result.launch_result["comparison"]["divergences"])


def test_installed_app_reference_is_inconclusive(global_config, tmp_path, frida_files):
    app = global_config.apps[0]
    _configure(app, tmp_path, frida_files)
    app.artifact["source"] = "installed_app_reference"
    device = RepackDevice()

    result = _run(app, global_config, device, tmp_path)

    assert result.final_status == "ARTIFACT_REQUIRED"
    assert result.verdict == "Inconclusive"


def test_missing_ipa_is_inconclusive(global_config, tmp_path, frida_files):
    app = global_config.apps[0]
    _configure(app, tmp_path, frida_files)
    app.artifact["ipa"] = str(tmp_path / "does-not-exist.ipa")
    device = RepackDevice()

    result = _run(app, global_config, device, tmp_path)

    assert result.final_status == "ARTIFACT_NOT_FOUND"
    assert result.verdict == "Inconclusive"


def test_injection_failure_is_inconclusive(global_config, tmp_path, frida_files, monkeypatch):
    monkeypatch.setattr("subprocess.run", _fake_subprocess(injection_ok=False))
    app = global_config.apps[0]
    _configure(app, tmp_path, frida_files)
    device = RepackDevice()

    result = _run(app, global_config, device, tmp_path)

    assert result.final_status == "DYLIB_INJECTION_FAILED"
    assert result.verdict == "Inconclusive"


def test_resign_failure_is_inconclusive(global_config, tmp_path, frida_files, monkeypatch):
    monkeypatch.setattr("subprocess.run", _fake_subprocess(codesign_ok=False))
    app = global_config.apps[0]
    _configure(app, tmp_path, frida_files)
    device = RepackDevice()

    result = _run(app, global_config, device, tmp_path)

    assert result.final_status == "RESIGN_FAILED"
    assert result.verdict == "Inconclusive"


def test_repackaged_install_failure_is_inconclusive(global_config, tmp_path, frida_files, monkeypatch):
    monkeypatch.setattr("subprocess.run", _fake_subprocess())
    app = global_config.apps[0]
    _configure(app, tmp_path, frida_files)
    device = RepackDevice(fail_repackaged_install=True)

    result = _run(app, global_config, device, tmp_path)

    assert result.final_status == "INSTALL_FAILED"
    assert result.verdict == "Inconclusive"


def test_undrivable_baseline_is_inconclusive(global_config, tmp_path, frida_files, monkeypatch):
    monkeypatch.setattr("subprocess.run", _fake_subprocess())
    app = global_config.apps[0]
    _configure(app, tmp_path, frida_files, exercise={"state_samples": 2, "sample_interval_seconds": 0})
    device = RepackDevice(baseline_states=(1, 1))

    result = _run(app, global_config, device, tmp_path)

    assert result.final_status == "BASELINE_FAILED"
    assert result.verdict == "Inconclusive"


def test_evidence_files_are_written_into_the_report_dir(global_config, tmp_path, frida_files, monkeypatch):
    monkeypatch.setattr("subprocess.run", _fake_subprocess())
    _pass_gadget(monkeypatch)
    app = global_config.apps[0]
    _configure(app, tmp_path, frida_files)
    device = RepackDevice()

    _run(app, global_config, device, tmp_path)

    report_dir = tmp_path / "reports" / "run1" / "ios" / app.id / "ios-feature-01-risk-02" / "repackaging"
    assert (report_dir / "baseline_comparison.json").is_file()
    assert (report_dir / "baseline_screen.png").is_file()
    assert (report_dir / "repackaged_screen.png").is_file()


def test_discover_provisioning_profile_picks_a_matching_local_profile(tmp_path, monkeypatch):
    from mobile_playbook.platforms.ios import repackage_resign

    monkeypatch.setattr("subprocess.run", _fake_subprocess())
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    (profiles / "a.mobileprovision").write_bytes(b"x")

    found = repackage_resign.discover_provisioning_profile(
        "com.example.app", "TEAM", "udid", TEST_IDENTITY, search_dir=str(profiles)
    )

    assert found == profiles / "a.mobileprovision"


def test_discover_provisioning_profile_returns_none_when_nothing_matches(tmp_path, monkeypatch):
    from mobile_playbook.platforms.ios import repackage_resign

    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout=_profile_bytes(team="OTHER"), stderr=b""),
    )
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    (profiles / "a.mobileprovision").write_bytes(b"x")

    assert (
        repackage_resign.discover_provisioning_profile(
            "com.example.app", "TEAM", "udid", TEST_IDENTITY, search_dir=str(profiles)
        )
        is None
    )


def test_set_bundle_identifier_rewrites_info_plist(tmp_path):
    from mobile_playbook.platforms.ios.mutations.repackage import set_bundle_identifier

    app_dir = tmp_path / "Example.app"
    app_dir.mkdir()
    (app_dir / "Info.plist").write_bytes(
        plistlib.dumps({"CFBundleIdentifier": "com.old.id", "CFBundleExecutable": "X"})
    )

    previous = set_bundle_identifier(app_dir, "com.new.id")

    assert previous == "com.old.id"
    with (app_dir / "Info.plist").open("rb") as handle:
        assert plistlib.load(handle)["CFBundleIdentifier"] == "com.new.id"


def test_rewrite_bundle_id_is_used_for_resign_and_install(global_config, tmp_path, frida_files, monkeypatch):
    def fake_subprocess(command, *args, **kwargs):
        tool = Path(str(command[0])).name if command else ""
        if tool == "security":
            return SimpleNamespace(returncode=0, stdout=_profile_bytes(bundle="com.example.repack"), stderr=b"")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", fake_subprocess)
    _pass_gadget(monkeypatch)
    calls = []
    monkeypatch.setattr(
        "mobile_playbook.platforms.ios.risks.feature_01_risk_02.set_bundle_identifier",
        lambda app_dir, new_id: calls.append(new_id) or "com.example.app",
    )
    app = global_config.apps[0]
    _configure(app, tmp_path, frida_files)
    app.risks["ios-feature-01-risk-02"]["resign"]["rewrite_bundle_id"] = "com.example.repack"
    device = RepackDevice()

    result = _run(app, global_config, device, tmp_path)

    assert calls == ["com.example.repack"]
    assert result.final_status == "REPACKAGING_SURVIVED"
    assert result.verdict == "At Risk"


def test_ensure_provisioning_profile_generates_when_missing(monkeypatch, tmp_path):
    from mobile_playbook.platforms.ios import repackage_resign

    seq = [None, tmp_path / "new.mobileprovision"]
    monkeypatch.setattr(repackage_resign, "discover_provisioning_profile", lambda *a, **k: seq.pop(0))
    gen = []
    monkeypatch.setattr(repackage_resign, "generate_provisioning_profile", lambda b, t, u: gen.append(b))

    result = repackage_resign.ensure_provisioning_profile("com.x.repack", "TEAM", "udid", TEST_IDENTITY)

    assert gen == ["com.x.repack"]
    assert result == tmp_path / "new.mobileprovision"


def test_discover_rejects_profile_expiring_within_margin(monkeypatch, tmp_path):
    from datetime import datetime, timedelta

    from mobile_playbook.platforms.ios import repackage_resign

    soon = plistlib.dumps({
        "Entitlements": {"application-identifier": "TEAM.com.example.app"},
        "TeamIdentifier": ["TEAM"],
        "ProvisionedDevices": ["udid"],
        "ExpirationDate": datetime.utcnow() + timedelta(seconds=120),
        "DeveloperCertificates": [FAKE_CERT],
    })
    monkeypatch.setattr("subprocess.run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=soon, stderr=b""))
    profiles = tmp_path / "p"
    profiles.mkdir()
    (profiles / "a.mobileprovision").write_bytes(b"x")
    args = ("com.example.app", "TEAM", "udid", TEST_IDENTITY)

    assert repackage_resign.discover_provisioning_profile(*args, search_dir=str(profiles), margin_seconds=3600) is None
    assert repackage_resign.discover_provisioning_profile(*args, search_dir=str(profiles), margin_seconds=0) is not None
