from __future__ import annotations

import os

import pytest

from mobile_playbook.platforms.ios.acquisition.intake_ipa import (
    IntakeIpaProvider,
    find_intake_ipa,
    resolve_intake_ipa,
    same_version,
    version_sort_key,
)
from mobile_playbook.platforms.ios.acquisition.registry import get_provider, known_sources
from tests.conftest import make_ipa


@pytest.fixture
def intake(tmp_path):
    directory = tmp_path / "intake/ios/ipas"
    directory.mkdir(parents=True)
    return directory


def _age(path, seconds: float) -> None:
    stamp = os.path.getmtime(path) - seconds
    os.utime(path, (stamp, stamp))


def test_source_is_discovered_by_the_plugin_registry():
    assert "intake_ipa" in known_sources()
    assert get_provider("intake_ipa").source == "intake_ipa"


def test_finds_the_ipa_matching_the_bundle_id(intake):
    make_ipa(intake / "Other_1.0.ipa", bundle_id="com.other.app")
    wanted = make_ipa(intake / "Parking_2.25.14.ipa", bundle_id="sg.parking")

    match = find_intake_ipa("sg.parking", intake)

    assert match is not None
    assert match.path == wanted
    assert match.candidate_count == 1


def test_returns_none_until_the_ipa_is_dropped_in(intake):
    assert find_intake_ipa("sg.parking", intake) is None

    make_ipa(intake / "Parking_2.25.14.ipa", bundle_id="sg.parking")

    assert find_intake_ipa("sg.parking", intake) is not None


def test_the_highest_version_wins_so_a_version_bump_needs_no_config_change(intake):
    old = make_ipa(intake / "Parking_2.25.14.ipa", bundle_id="sg.parking", version="2.25.14")
    new = make_ipa(intake / "Parking_2.26.0.ipa", bundle_id="sg.parking", version="2.26.0")
    _age(old, 3600)

    match = find_intake_ipa("sg.parking", intake)

    assert match.path == new
    assert match.candidate_count == 2  # caller can say a tie-break happened


def test_unreadable_files_are_skipped_not_raised_on(intake):
    (intake / "half-copied.ipa").write_bytes(b"not a zip")
    wanted = make_ipa(intake / "Parking.ipa", bundle_id="sg.parking")

    assert find_intake_ipa("sg.parking", intake).path == wanted


def test_missing_intake_directory_is_not_an_error(tmp_path):
    assert find_intake_ipa("sg.parking", tmp_path / "nope") is None


def test_resolves_by_app_name_when_no_bundle_id_is_known(intake):
    make_ipa(intake / "Other.ipa", bundle_id="com.other.app", display_name="Other App")
    wanted = make_ipa(intake / "Example_Wallet_6.28.1.ipa", bundle_id="com.example.wallet", display_name="Example Wallet")

    resolution = resolve_intake_ipa(app_name="Example Wallet", intake_dir=intake)

    assert resolution.match.path == wanted
    assert resolution.match.bundle_id == "com.example.wallet"
    assert resolution.matched_on == "name"


def test_name_matching_ignores_case_spacing_and_punctuation(intake):
    make_ipa(intake / "App.ipa", bundle_id="com.example.wallet", display_name="Example Wallet")

    for typed in ["example wallet", "Example-Wallet", "  Example   Wallet "]:
        assert resolve_intake_ipa(app_name=typed, intake_dir=intake).match is not None


def test_a_different_name_does_not_match(intake):
    make_ipa(intake / "App.ipa", bundle_id="com.example.wallet", display_name="Example Wallet")

    resolution = resolve_intake_ipa(app_name="CPF", intake_dir=intake)

    assert resolution.match is None
    assert resolution.ambiguous is False


def test_two_different_apps_sharing_a_name_are_ambiguous_not_guessed(intake):
    make_ipa(intake / "WalletA.ipa", bundle_id="com.a.wallet", display_name="Wallet")
    make_ipa(intake / "WalletB.ipa", bundle_id="com.b.wallet", display_name="Wallet")

    resolution = resolve_intake_ipa(app_name="Wallet", intake_dir=intake)

    assert resolution.match is None
    assert resolution.ambiguous is True
    assert len(resolution.candidates) == 2


def test_two_versions_of_the_same_app_are_not_ambiguous(intake):
    old = make_ipa(intake / "ExampleHealth_6.0.12.ipa", bundle_id="com.example.health", display_name="ExampleHealth")
    new = make_ipa(intake / "ExampleHealth_6.0.13.ipa", bundle_id="com.example.health", display_name="ExampleHealth")
    _age(old, 3600)

    resolution = resolve_intake_ipa(app_name="ExampleHealth", intake_dir=intake)

    assert resolution.ambiguous is False
    assert resolution.match.path == new


def test_an_explicit_bundle_id_wins_over_the_name(intake):
    make_ipa(intake / "Wallet.ipa", bundle_id="com.a.wallet", display_name="Wallet")
    other = make_ipa(intake / "Other.ipa", bundle_id="com.b.other", display_name="Other")

    resolution = resolve_intake_ipa(bundle_id="com.b.other", app_name="Wallet", intake_dir=intake)

    assert resolution.match.path == other
    assert resolution.matched_on == "bundle_id"


def test_acquire_reports_not_found_rather_than_a_missing_path_error(intake, tmp_path):
    class FakeApp:
        id = "parking"
        name = "Parking"
        bundle_id = "sg.parking"
        test_bundle_id = "sg.parking"
        artifact = {"source": "intake_ipa", "expected_bundle_id": "sg.parking", "intake_dir": str(intake)}

    result = IntakeIpaProvider().acquire(FakeApp(), None, None, "20250101_000000", tmp_path / "out")

    assert result.status == "ARTIFACT_NOT_FOUND"
    assert "Extract it from the test device" in result.errors[0]


def test_acquire_copies_the_resolved_ipa_into_the_run_directory(intake, tmp_path):
    make_ipa(intake / "Parking_2.25.14.ipa", bundle_id="sg.parking")

    class FakeApp:
        id = "parking"
        name = "Parking"
        bundle_id = "sg.parking"
        test_bundle_id = "sg.parking"
        artifact = {"source": "intake_ipa", "expected_bundle_id": "sg.parking", "intake_dir": str(intake)}

    result = IntakeIpaProvider().acquire(FakeApp(), None, None, "20250101_000000", tmp_path / "out")

    assert result.status == "ACQUIRED"
    assert result.bundle_id == "sg.parking"
    assert result.ipa_path.exists()
    assert result.ipa_path.name == "original.ipa"


@pytest.mark.parametrize(
    "lower, higher",
    [
        ("4.9.2", "4.10.0"),
        ("4.3.4", "4.4"),
        ("4.4 beta", "4.4"),
        ("3.9.9", "4.3.4"),
        ("not a version", "0.1"),
        ("1.2.3", "1.2.3.1"),
    ],
)
def test_versions_compare_by_their_numeric_parts(lower, higher):
    assert version_sort_key(lower) < version_sort_key(higher)


def test_trailing_zeros_and_case_name_the_same_version():
    assert same_version("4.3", "4.3.0")
    assert same_version("4.4 Beta", "4.4 beta")
    assert not same_version("4.3", "4.3.1")


def test_a_lower_version_copied_in_later_does_not_become_the_default(intake):
    current = make_ipa(intake / "FWMOMCare_4.3.4.ipa", bundle_id="sg.example.care", version="4.3.4")
    older = make_ipa(intake / "FWMOMCare_3.9.9.ipa", bundle_id="sg.example.care", version="3.9.9")
    _age(current, 3600)

    resolution = resolve_intake_ipa(bundle_id="sg.example.care", intake_dir=intake)

    assert resolution.match.path == current
    assert [build.path for build in resolution.candidates] == [current, older]


def test_the_same_version_prefers_the_higher_build_number_then_the_newer_file(intake):
    low = make_ipa(intake / "a.ipa", bundle_id="sg.example.care", version="4.3.4", build="12")
    high = make_ipa(intake / "b.ipa", bundle_id="sg.example.care", version="4.3.4", build="13")
    same_build_newer = make_ipa(intake / "c.ipa", bundle_id="sg.example.care", version="4.3.4", build="12")
    _age(high, 3600)
    _age(low, 7200)

    candidates = resolve_intake_ipa(bundle_id="sg.example.care", intake_dir=intake).candidates

    assert [build.path for build in candidates] == [high, same_build_newer, low]


def test_a_requested_version_is_chosen_even_when_it_is_not_the_highest(intake):
    make_ipa(intake / "FWMOMCare_4.3.4.ipa", bundle_id="sg.example.care", version="4.3.4")
    older = make_ipa(intake / "FWMOMCare_3.9.9.ipa", bundle_id="sg.example.care", version="3.9.9")

    resolution = resolve_intake_ipa(bundle_id="sg.example.care", intake_dir=intake, version="3.9.9")

    assert resolution.match.path == older
    assert resolution.missing_version is None


def test_a_requested_version_that_is_absent_is_reported_rather_than_replaced(intake):
    make_ipa(intake / "FWMOMCare_4.3.4.ipa", bundle_id="sg.example.care", version="4.3.4")

    resolution = resolve_intake_ipa(bundle_id="sg.example.care", intake_dir=intake, version="5.0")

    assert resolution.match is None
    assert resolution.missing_version == "5.0"
    assert [build.version for build in resolution.candidates] == ["4.3.4"]


def test_acquire_names_the_missing_version_and_the_available_ones(intake, tmp_path):
    make_ipa(intake / "Parking_2.25.14.ipa", bundle_id="sg.parking", version="2.25.14")

    class FakeApp:
        id = "parking"
        name = "Parking"
        bundle_id = "sg.parking"
        test_bundle_id = "sg.parking"
        artifact = {
            "source": "intake_ipa",
            "expected_bundle_id": "sg.parking",
            "intake_dir": str(intake),
            "build_version": "2.30.0",
        }

    result = IntakeIpaProvider().acquire(FakeApp(), None, None, "20250101_000000", tmp_path / "out")

    assert result.status == "ARTIFACT_NOT_FOUND"
    assert "version 2.30.0" in result.errors[0]
    assert "Available versions: 2.25.14" in result.errors[0]


def test_acquire_takes_the_pinned_version(intake, tmp_path):
    make_ipa(intake / "Parking_2.26.0.ipa", bundle_id="sg.parking", version="2.26.0")
    pinned = make_ipa(intake / "Parking_2.25.14.ipa", bundle_id="sg.parking", version="2.25.14")

    class FakeApp:
        id = "parking"
        name = "Parking"
        bundle_id = "sg.parking"
        test_bundle_id = "sg.parking"
        artifact = {
            "source": "intake_ipa",
            "expected_bundle_id": "sg.parking",
            "intake_dir": str(intake),
            "build_version": "2.25.14",
        }

    app = FakeApp()
    result = IntakeIpaProvider().acquire(app, None, None, "20250101_000000", tmp_path / "out")

    assert result.status == "ACQUIRED"
    assert app.artifact["ipa"] == str(pinned)
