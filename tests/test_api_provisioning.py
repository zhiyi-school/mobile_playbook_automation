from __future__ import annotations

import pytest

from mobile_playbook.api import config_editor as ce
from mobile_playbook.api import provisioning
from tests.conftest import make_ipa
from tests.test_api_config_editor import config_root  # noqa: F401 — reused fixture

#: Anything a dashboard renders must not name paths, files or config internals.
LEAKY_FRAGMENTS = ("intake/", "configs/", ".ipa", ".apk", ".yaml", "bundle_id", "artifact", "apps[")


def _stage(report: dict, stage_id: str) -> dict:
    return next(stage for stage in report["stages"] if stage["id"] == stage_id)


def _assert_discloses_nothing(report: dict) -> None:
    text = " ".join(
        [str(stage["label"]) + " " + str(stage["detail"] or "") for stage in report["stages"]]
        + [str(report.get("error") or ""), str(report.get("detail") or "")]
    )
    for fragment in LEAKY_FRAGMENTS:
        assert fragment not in text, f"stage text leaked {fragment!r}: {text}"


@pytest.fixture
def no_device(monkeypatch):
    monkeypatch.setattr(provisioning.AdbClient, "is_available", lambda self: True)
    monkeypatch.setattr(
        provisioning.AdbClient, "run", lambda self, args, timeout=None: (1, "", "no devices/emulators found")
    )


@pytest.fixture
def device_with(monkeypatch):
    def _install(packages: list[str]):
        def fake_run(self, args, timeout=None):
            if args == ["get-state"]:
                return 0, "device", ""
            if args[:3] == ["shell", "pm", "list"]:
                wanted = args[4] if len(args) > 4 else ""
                return (0, f"package:{wanted}", "") if wanted in packages else (0, "", "")
            return 0, "", ""

        monkeypatch.setattr(provisioning.AdbClient, "is_available", lambda self: True)
        monkeypatch.setattr(provisioning.AdbClient, "run", fake_run)

    return _install


def _append_broken_app(config_root) -> str:
    """Add an app with an invalid entry by hand, the way an out-of-band edit would."""
    path = config_root / "configs/split/ios/apps.yaml"
    path.write_text(
        path.read_text().rstrip("\n")
        + "\n"
        + "  - id: broken_app\n"
        + '    name: "Broken App"\n'
        + '    bundle_id: "com.example.broken"\n'
        + '    test_bundle_id: "com.example.broken"\n'
        + "    artifact:\n"
        + '      source: "not_a_real_source"\n'
        + "    risks: {}\n"
    )
    return "broken_app"


def _register_by_name(name: str) -> str:
    return ce.add_ios_app(
        {
            "name": name,
            "artifact": {"source": "intake_ipa"},
            "risks": {"ios-feature-01-risk-01": {"enabled": True}},
        }
    )["id"]


def test_reports_three_setup_stages(config_root):
    report = provisioning.describe("ios", _register_by_name("Example Wallet"))

    assert [stage["id"] for stage in report["stages"]] == [
        "app_registered",
        "service_online",
        "configuration_applied",
    ]


def test_app_not_registered_shows_environment_being_prepared(config_root):
    report = provisioning.describe("ios", "never_added")

    assert report["status"] == "pending"
    assert _stage(report, "app_registered")["state"] == "in_progress"
    assert _stage(report, "service_online")["state"] == "done"
    assert _stage(report, "configuration_applied")["state"] == "pending"
    _assert_discloses_nothing(report)


def test_registered_app_waits_on_configuration_until_its_build_arrives(config_root):
    app_id = _register_by_name("Example Wallet")

    report = provisioning.describe("ios", app_id)
    assert report["status"] == "pending"
    assert _stage(report, "app_registered")["state"] == "done"
    assert _stage(report, "configuration_applied")["state"] == "in_progress"
    assert report["bundle_id"] is None
    _assert_discloses_nothing(report)

    make_ipa(
        config_root / "intake/ios/ipas/Example_Wallet_6.28.1.ipa",
        bundle_id="com.example.wallet",
        display_name="Example Wallet",
    )

    report = provisioning.describe("ios", app_id)
    assert report["status"] == "ready"
    assert _stage(report, "configuration_applied")["state"] == "done"
    assert report["bundle_id"] == "com.example.wallet"
    _assert_discloses_nothing(report)


def test_ambiguous_builds_fail_without_naming_them(config_root):
    app_id = _register_by_name("Wallet")
    make_ipa(config_root / "intake/ios/ipas/A.ipa", bundle_id="com.a.wallet", display_name="Wallet")
    make_ipa(config_root / "intake/ios/ipas/B.ipa", bundle_id="com.b.wallet", display_name="Wallet")

    report = provisioning.describe("ios", app_id)

    assert report["status"] == "failed"
    assert "com.a.wallet" not in str(report)
    _assert_discloses_nothing(report)


def test_another_apps_broken_entry_does_not_affect_this_one(config_root):
    app_id = _register_by_name("Example Wallet")
    make_ipa(
        config_root / "intake/ios/ipas/Example_Wallet.ipa",
        bundle_id="com.example.wallet",
        display_name="Example Wallet",
    )
    broken_id = _append_broken_app(config_root)

    assert provisioning.describe("ios", app_id)["status"] == "ready"
    assert provisioning.describe("ios", broken_id)["status"] == "failed"


def test_own_broken_entry_reports_generically(config_root):
    report = provisioning.describe("ios", _append_broken_app(config_root))

    assert report["status"] == "failed"
    assert "not_a_real_source" not in str(report)
    _assert_discloses_nothing(report)


def test_a_missing_build_waits_instead_of_failing_config(config_root):
    """A registered app whose build hasn't been provided yet is pending, not broken."""
    ce.edit_ios_app("app_one", {"artifact": {"ipa": "intake/ios/ipas/not_extracted_yet.ipa"}})

    report = provisioning.describe("ios", "app_one")

    assert report["status"] == "pending"
    assert _stage(report, "configuration_applied")["state"] == "in_progress"
    _assert_discloses_nothing(report)


def test_android_waits_on_install_then_goes_ready(config_root, device_with):
    device_with([])
    report = provisioning.describe("android", "one")
    assert report["status"] == "pending"
    assert _stage(report, "configuration_applied")["state"] == "in_progress"
    _assert_discloses_nothing(report)

    device_with(["com.example.one"])
    report = provisioning.describe("android", "one")
    assert report["status"] == "ready"
    assert _stage(report, "configuration_applied")["state"] == "done"


def test_android_without_a_connected_device_is_unknown_not_pending(config_root, no_device):
    report = provisioning.describe("android", "one")

    # "unknown" must not strand an app in setup forever.
    assert report["status"] == "ready"
    assert _stage(report, "configuration_applied")["state"] == "unknown"
    _assert_discloses_nothing(report)


READINESS_FIELDS = (
    "configuration_ready",
    "device_required",
    "device_ready",
    "platform_available",
    "runnable",
    "blocker_code",
    "retryable",
    "detail",
)


def test_every_report_carries_structured_readiness(config_root):
    for report in (
        provisioning.describe("ios", "never_added"),
        provisioning.describe("ios", _register_by_name("Example Wallet")),
        provisioning.describe("ios", _append_broken_app(config_root)),
    ):
        for field in READINESS_FIELDS:
            assert field in report, f"{field} missing from {sorted(report)}"
        assert isinstance(report["runnable"], bool)
        _assert_discloses_nothing(report)


def test_configuration_ready_does_not_mean_runnable_without_a_device(config_root, no_device):
    report = provisioning.describe("android", "one")

    assert report["status"] == "ready"
    assert report["configuration_ready"] is True
    assert report["device_ready"] is False
    assert report["runnable"] is False
    assert report["blocker_code"] == "no_device"
    assert report["retryable"] is True
    _assert_discloses_nothing(report)


def test_a_connected_device_with_the_app_installed_is_runnable(config_root, device_with):
    device_with(["com.example.one"])

    report = provisioning.describe("android", "one")

    assert report["configuration_ready"] is True
    assert report["device_ready"] is True
    assert report["platform_available"] is True
    assert report["runnable"] is True
    assert report["blocker_code"] is None


def test_an_unreachable_device_is_reported_apart_from_a_missing_one(config_root, monkeypatch):
    monkeypatch.setattr(provisioning.AdbClient, "is_available", lambda self: False)

    report = provisioning.describe("android", "one")

    assert report["device_ready"] is False
    assert report["blocker_code"] == "device_unreachable"
    assert report["retryable"] is True
    _assert_discloses_nothing(report)


def test_an_app_still_being_registered_is_retryable(config_root):
    """The add-app flow registers the app moments after the assessment exists."""
    report = provisioning.describe("ios", "never_added")

    assert report["configuration_ready"] is False
    assert report["runnable"] is False
    assert report["blocker_code"] == "configuration_pending"
    assert report["retryable"] is True
    _assert_discloses_nothing(report)


def test_a_broken_configuration_is_not_retryable(config_root):
    report = provisioning.describe("ios", _append_broken_app(config_root))

    assert report["configuration_ready"] is False
    assert report["runnable"] is False
    assert report["retryable"] is False
    assert report["blocker_code"] == "configuration_incomplete"


def test_an_app_with_no_enabled_tests_is_not_retryable(config_root):
    app_id = ce.add_ios_app({"name": "Example No Tests", "artifact": {"source": "intake_ipa"}, "risks": {}})["id"]
    make_ipa(
        config_root / "intake/ios/ipas/Example_No_Tests.ipa",
        bundle_id="com.example.notests",
        display_name="Example No Tests",
    )

    report = provisioning.describe("ios", app_id)

    assert report["blocker_code"] == "no_tests_enabled"
    assert report["retryable"] is False


def test_a_missing_build_is_retryable_rather_than_broken(config_root):
    report = provisioning.describe("ios", _register_by_name("Example Wallet"))

    assert report["configuration_ready"] is False
    assert report["runnable"] is False
    assert report["retryable"] is True
    assert report["blocker_code"] == "app_build_missing"


def test_a_platform_already_running_is_not_available(config_root, device_with):
    device_with(["com.example.one"])
    assert provisioning.registry.try_claim_platform("android")
    try:
        report = provisioning.describe("android", "one")
    finally:
        provisioning.registry.release_platform("android")

    assert report["platform_available"] is False
    assert report["runnable"] is False
    assert report["blocker_code"] == "platform_busy"
    assert report["retryable"] is True


def test_readiness_never_names_a_device_or_a_path(config_root, no_device):
    report = provisioning.describe("android", "one")

    text = str(report["detail"]) + str(report["blocker_code"])
    for fragment in ("/", "udid", "serial", "adb", "emulator-"):
        assert fragment not in text.lower()
