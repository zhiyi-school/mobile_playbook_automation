from __future__ import annotations

import base64
import json

import pytest

from mobile_playbook.platforms.android.private_data.scan import scan_archive, scan_file
from tests.platforms.android.private_data_fakes import CIPHERTEXT, DATA, PACKAGE, TOKEN, make_archive


@pytest.mark.parametrize(
    ("relative", "sdk"),
    [
        ("databases/com.google.android.datatransport.events", "Google Data Transport"),
        ("databases/google_app_measurement_local.db-wal", "Google Analytics for Firebase"),
        ("databases/google_app_measurement.db", "Google Analytics for Firebase"),
        ("files/PersistedInstallationNotes.json", None),
        ("databases/app.db", None),
    ],
)
def test_known_sdk_files_are_recognised_by_their_path(relative, sdk):
    assert scan_file(f"/{DATA}/{relative}", b"", False).sdk == sdk
    assert scan_file(f"/data/user_de/0/{PACKAGE}/{relative}", b"", False).sdk == sdk


def test_app_code_is_checked_for_secrets_but_not_personal_data():
    code = f'var support = "help@example.com"; var n = 4111111111111111; var apiKey = "{TOKEN}";'.encode()

    for name in ("bundle-1.js", "index.android.bundle", "page.html"):
        scanned = scan_file(f"/{DATA}/files/.expo-internal/{name}", code, False)
        assert scanned.category == "code"
        assert {match_type for item in scanned.findings for match_type in item["match_types"]} == {"SENSITIVE_KEY_VALUE"}
    hermes = scan_file(f"/{DATA}/files/app.hbc", b"\xc6\x1f\xbc\x03\x00" + code, False)
    assert {match_type for item in hermes.findings for match_type in item["match_types"]} == {"SENSITIVE_KEY_VALUE"}


def test_a_value_matched_by_several_rules_is_one_finding():
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJlc2lnbmF0dXJl"

    scanned = scan_file(f"/{DATA}/files/session.json", json.dumps({"AuthToken": jwt}).encode(), False)

    [finding] = scanned.findings
    assert finding["match_types"] == ["JWT", "SENSITIVE_KEY_NAME", "SENSITIVE_KEY_VALUE"]
    assert finding["key_path"] == "$.AuthToken"
    assert finding["severity"] == "HIGH"


def test_unsafe_archive_paths_and_symlinks_are_never_read(tmp_path):
    archive = make_archive(
        tmp_path / "device.tar",
        {"../escape.json": json.dumps({"password": TOKEN}).encode()},
        symlinks={f"{DATA}/files/link": "/etc/passwd"},
    )

    files = scan_archive(archive, reveal_values=False)

    assert [(item.path, item.category, item.findings) for item in files] == [("../escape.json", "skipped", [])]


@pytest.mark.parametrize(
    ("data", "match_types"),
    [
        (b'{"card": "4111 1111 1111 1111"}', {"PAYMENT_CARD_NUMBER"}),
        (b'{"note": "4111 1111 1111 1112"}', set()),
        (b'{"created_at": 1729000000000}', set()),
        (b'{"mobile_sdk_version": "1.2.3", "phone_model": "SM-A346E"}', set()),
        (b'{"mobile_number": "+65 9123 4567"}', {"PERSONAL_DATA_KEY_NAME"}),
        (b'{"user_email": "not-an-email"}', set()),
    ],
    ids=["luhn-valid-card", "luhn-invalid-card", "epoch-millis", "phone-and-mobile-words", "phone-number", "email-key-without-address"],
)
def test_personal_data_rules_avoid_common_false_positives(data, match_types):
    scanned = scan_file(f"/{DATA}/files/profile.json", data, reveal_values=False)

    assert {match_type for item in scanned.findings for match_type in item["match_types"]} == match_types


def test_base64_of_encrypted_bytes_counts_as_opaque_but_a_short_token_does_not():
    assert scan_file(f"/{DATA}/files/blob.txt", base64.b64encode(CIPHERTEXT), False).category == "opaque"
    assert scan_file(f"/{DATA}/files/id.txt", b"3f2a9c1e5b7d", False).category == "text"


def test_strings_in_binary_files_are_scanned(tmp_path):
    datastore = b"\x0a\x14\x0a\x0auser_email\x12\x06\x2a" + b"\x2a\x10jo@example.com\x00\x01"

    scanned = scan_file(f"/{DATA}/files/datastore/user.preferences_pb", datastore, False)

    assert scanned.category == "binary"
    assert {match_type for item in scanned.findings for match_type in item["match_types"]} == {"EMAIL_ADDRESS"}


def test_malformed_shared_preferences_are_reported_as_unreadable():
    scanned = scan_file(f"/{DATA}/shared_prefs/broken.xml", b"<map><string name=", False)

    assert scanned.category == "unreadable"
    assert scanned.note.startswith("scan failed")


def test_a_database_journal_is_scanned_for_strings():
    scanned = scan_file(f"/{DATA}/databases/users.db-wal", b"\x00\x01" + f"'password': '{TOKEN}'".encode() + b"\x00", False)

    assert scanned.category == "sqlite_sidecar"
    assert {match_type for item in scanned.findings for match_type in item["match_types"]} == {"SENSITIVE_KEY_VALUE"}
