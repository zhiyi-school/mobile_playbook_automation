from __future__ import annotations

import io
import json
import random
import shlex
import shutil
import sqlite3
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from mobile_playbook.common.storage_paths import android_work_dir
from mobile_playbook.platforms.android.risks import feature_07_risk_01_private_data as private_data
from mobile_playbook.platforms.android.risks.feature_07_risk_01_private_data import AndroidPrivateDataRisk, scan_archive, scan_file

PACKAGE = "com.example.app"
DATA = f"data/data/{PACKAGE}"
TOKEN = "s3cr3t-session-token-value-0123456789"
PLAIN_PREFS = b'<?xml version="1.0" encoding="utf-8"?>\n<map>\n    <boolean name="onboarded" value="true" />\n    <string name="theme">dark</string>\n</map>\n'
CIPHERTEXT = random.Random(7).randbytes(1024)
ENCRYPTED_PREFS = (
    b"<?xml version='1.0' encoding='utf-8'?>\n<map>\n"
    b'    <string name="__androidx_security_crypto_encrypted_prefs_key_keyset__">12a901c2b3</string>\n'
    b'    <string name="ATn8d2C1kZ9tO0vF">AWxR0pL3cQ9e</string>\n</map>\n'
)


class FakeReportWriter:
    run_timestamp = "2026-10-19_00-00-00"

    def __init__(self, root: Path):
        self.root = root
        self.results = []

    def test_report_dir(self, app_id, risk_id, test_case_id, platform):
        return self.root

    def write_result(self, result, report_dir):
        self.results.append(result)


class RootAdb:
    """A rooted device whose su runs the risk's commands; `tar -cf` then `pull` hands over the prepared archive."""

    def __init__(self, archive: Path | None, *, grants_root=True, du_kb=8, user_de=False, pull_code=0, tar_code=0, monkey_out="Events injected: 1"):
        self.archive = archive
        self.grants_root = grants_root
        self.du_kb = du_kb
        self.user_de = user_de
        self.pull_code = pull_code
        self.tar_code = tar_code
        self.monkey_out = monkey_out
        self.calls: list[list[str]] = []
        self.root_commands: list[str] = []

    def run(self, args, timeout=None):
        self.calls.append(args)
        if args == ["shell", "su", "0", "id", "-u"]:
            return (0, "0\n", "") if self.grants_root else (1, "", "su: not found")
        if args[:4] == ["shell", "su", "0", "sh"]:
            return self._root(shlex.split(args[5])[0])
        if args[:2] == ["shell", "monkey"]:
            return 0, self.monkey_out, ""
        if args[:1] == ["pull"]:
            if self.pull_code == 0:
                shutil.copy(self.archive, args[2])
                return 0, "1 file pulled", ""
            return self.pull_code, "", "adb: error: failed to stat remote object"
        if args[:2] == ["shell", "id"] or args[:3] == ["shell", "su", "-c"]:
            return 0, "2000\n", ""
        return 1, "", ""

    def _root(self, command: str):
        self.root_commands.append(command)
        if command == f"test -d /data/data/{PACKAGE}":
            return 0, "", ""
        if command == f"test -d /data/user_de/0/{PACKAGE}":
            return (0, "", "") if self.user_de else (1, "", "")
        if command.startswith("ls -laR"):
            return 0, f"/data/data/{PACKAGE}:\ntotal 8\ndrwx------ 4 u0_a200 u0_a200 4096 shared_prefs\n", ""
        if command.startswith("du -sk"):
            return 0, f"{self.du_kb}\t/data/data/{PACKAGE}\n", ""
        if command.startswith("tar -cf"):
            return self.tar_code, "", "tar: socket ignored" if self.tar_code else ""
        if command.startswith("test -s"):
            return 0, "", ""
        if command.startswith("rm -f"):
            return 0, "", ""
        return 1, "", "unexpected"


def make_archive(path: Path, files: dict[str, bytes], symlinks: dict[str, str] | None = None) -> Path:
    with tarfile.open(path, "w") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        for name, target in (symlinks or {}).items():
            info = tarfile.TarInfo(name)
            info.type = tarfile.SYMTYPE
            info.linkname = target
            tar.addfile(info)
    return path


def sqlite_bytes(tmp_path: Path, rows: list[tuple[str, str]]) -> bytes:
    database = tmp_path / "users.db"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE users (name TEXT, email TEXT)")
    connection.executemany("INSERT INTO users VALUES (?, ?)", rows)
    connection.commit()
    connection.close()
    return database.read_bytes()


def run_risk(tmp_path: Path, adb: RootAdb, risks: dict | None = None, private_data_config: dict | None = None):
    report_dir = tmp_path / "report"
    report_dir.mkdir()
    app_config = SimpleNamespace(id="example", name="Example", package_name=PACKAGE, risks=risks or {})
    global_config = SimpleNamespace(private_data={"launch_wait": 0, **(private_data_config or {})})
    writer = FakeReportWriter(report_dir)
    result = AndroidPrivateDataRisk().run(app_config, global_config, SimpleNamespace(adb=adb), writer)
    assert writer.results == [result]
    return result


def pulled_archive() -> Path:
    return android_work_dir() / "private_data" / PACKAGE.replace(".", "_") / f"{FakeReportWriter.run_timestamp}.tar"


def test_plain_text_secrets_and_personal_data_put_the_app_at_risk(tmp_path):
    archive = make_archive(
        tmp_path / "device.tar",
        {
            f"{DATA}/shared_prefs/session.xml": f'<map><string name="auth_token">{TOKEN}</string></map>'.encode(),
            f"{DATA}/files/pii.json": json.dumps({"name": "Tan Ah Kow", "nric": "S1234567D", "contact": "ah.kow@example.com"}).encode(),
            f"{DATA}/databases/users.db": sqlite_bytes(tmp_path, [("Lim", "lim@example.com")]),
        },
    )
    adb = RootAdb(archive)

    result = run_risk(tmp_path, adb)

    assert (result.final_status, result.verdict) == ("PRIVATE_DATA_EXPOSED", "At Risk")
    assert result.errors == []
    assert result.metadata["root_method"] == "su_0"
    assert result.metadata["directories"] == [f"/data/data/{PACKAGE}"]
    assert result.metadata["unencrypted_databases"] == [f"/{DATA}/databases/users.db"]
    report = json.loads((tmp_path / "report" / "private_data_findings.json").read_text())
    found = {(item["path"].rsplit("/", 1)[-1], match_type) for item in report["findings"] for match_type in item["match_types"]}
    assert ("session.xml", "SENSITIVE_KEY_NAME") in found
    assert ("pii.json", "NRIC_FIN") in found
    assert ("pii.json", "EMAIL_ADDRESS") in found
    assert ("users.db", "EMAIL_ADDRESS") in found
    assert TOKEN not in json.dumps(report)
    assert TOKEN not in json.dumps(result.to_dict())
    critical = json.loads((tmp_path / "report" / "critical_findings.json").read_text())
    assert {flag["id"] for flag in critical["flags"]} == {"PRIVATE_DATA_READABLE_WITH_ROOT", "SENSITIVE_INFORMATION_EXPOSURE", "UNENCRYPTED_DATABASES"}
    assert {item["kind"] for item in result.evidence} == {"private_data_listing", "private_data_findings", "critical_findings"}
    assert "monkey aborted" not in str(result.metadata["launch"])
    assert not pulled_archive().exists()
    assert "archive" not in result.metadata
    assert any(command.startswith("rm -f /data/local/tmp/") for command in adb.root_commands)


def test_encrypted_stores_with_nothing_sensitive_in_plain_text_reduce_the_risk(tmp_path):
    archive = make_archive(
        tmp_path / "device.tar",
        {
            f"{DATA}/shared_prefs/secure_prefs.xml": ENCRYPTED_PREFS,
            f"{DATA}/shared_prefs/settings.xml": PLAIN_PREFS,
            f"{DATA}/files/pii.json": CIPHERTEXT,
        },
    )

    result = run_risk(tmp_path, RootAdb(archive))

    assert (result.final_status, result.verdict) == ("PRIVATE_DATA_PROTECTED", "Reduced Risk")
    assert result.metadata["protected_files"] == [f"/{DATA}/shared_prefs/secure_prefs.xml", f"/{DATA}/files/pii.json"]
    assert result.metadata["categories"] == {"encrypted_prefs": 1, "shared_prefs": 1, "opaque": 1}


def test_tokens_in_known_sdk_files_are_reported_without_deciding_the_verdict(tmp_path):
    jwt = "eyJhbGciOiJFUzI1NiJ9.eyJhcHBJZCI6IjE6MTIzOmFuZHJvaWQ6YWJjIn0.c2lnbmF0dXJlc2lnbmF0dXJl"
    archive = make_archive(
        tmp_path / "device.tar",
        {
            f"{DATA}/files/PersistedInstallation.W0RFRkFVTFRd+MToxMjM6YW5kcm9pZDphYmM.json": json.dumps({"AuthToken": jwt, "RefreshToken": TOKEN}).encode(),
            f"{DATA}/shared_prefs/secure_prefs.xml": ENCRYPTED_PREFS,
        },
    )

    result = run_risk(tmp_path, RootAdb(archive))

    assert (result.final_status, result.verdict) == ("PRIVATE_DATA_PROTECTED", "Reduced Risk")
    assert (result.metadata["finding_count"], result.metadata["sdk_finding_count"]) == (0, 2)
    assert result.metadata["sdk_files"] == [{"path": f"/{DATA}/files/PersistedInstallation.W0RFRkFVTFRd+MToxMjM6YW5kcm9pZDphYmM.json", "sdk": "Firebase Installations"}]
    report = json.loads((tmp_path / "report" / "private_data_findings.json").read_text())
    assert report["findings"] == []
    assert {match_type for item in report["sdk_findings"] for match_type in item["match_types"]} >= {"JWT", "SENSITIVE_KEY_NAME"}
    critical = json.loads((tmp_path / "report" / "critical_findings.json").read_text())
    assert {flag["id"] for flag in critical["flags"]} == {"PRIVATE_DATA_READABLE_WITH_ROOT", "SDK_DATA_IN_PLAIN_TEXT"}


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


def test_a_secret_in_plain_text_outweighs_encrypted_stores(tmp_path):
    archive = make_archive(
        tmp_path / "device.tar",
        {
            f"{DATA}/shared_prefs/secure_prefs.xml": ENCRYPTED_PREFS,
            f"{DATA}/shared_prefs/session.xml": f'<map><string name="refresh_token">{TOKEN}</string></map>'.encode(),
        },
    )

    result = run_risk(tmp_path, RootAdb(archive))

    assert (result.final_status, result.verdict) == ("PRIVATE_DATA_EXPOSED", "At Risk")


@pytest.mark.parametrize(
    "files",
    [
        {f"{DATA}/shared_prefs/settings.xml": PLAIN_PREFS},
        {f"{DATA}/shared_prefs/settings.xml": PLAIN_PREFS, f"{DATA}/cache/image_cache/0": CIPHERTEXT},
        {},
    ],
    ids=["plain-text-without-findings", "opaque-file-only-in-cache", "empty"],
)
def test_without_findings_or_encrypted_stores_the_result_is_inconclusive(tmp_path, files):
    result = run_risk(tmp_path, RootAdb(make_archive(tmp_path / "device.tar", files)))

    assert (result.final_status, result.verdict) == ("PRIVATE_DATA_NO_FINDINGS", "Inconclusive")
    assert result.metadata["protected_files"] == []


def test_a_device_without_root_fails_before_touching_the_app(tmp_path):
    adb = RootAdb(None, grants_root=False)

    result = run_risk(tmp_path, adb)

    assert (result.final_status, result.verdict) == ("FAILED", "Inconclusive")
    assert "does not grant root" in result.errors[0]
    assert not any(args[:2] == ["shell", "monkey"] or args[:1] == ["pull"] for args in adb.calls)


def test_private_data_above_the_size_limit_is_not_copied(tmp_path):
    adb = RootAdb(None, du_kb=3 * 1024)

    result = run_risk(tmp_path, adb, private_data_config={"max_archive_mb": 2})

    assert result.final_status == "FAILED"
    assert "max_archive_mb" in result.errors[0]
    assert not any(command.startswith("tar") for command in adb.root_commands)


def test_the_app_settings_override_the_global_settings(tmp_path):
    adb = RootAdb(make_archive(tmp_path / "device.tar", {}), du_kb=3 * 1024)

    result = run_risk(tmp_path, adb, risks={"android-feature-07-risk-01": {"enabled": True, "max_archive_mb": 4}}, private_data_config={"max_archive_mb": 2})

    assert (result.final_status, result.errors) == ("PRIVATE_DATA_NO_FINDINGS", [])


def test_a_failed_pull_still_removes_the_archive_from_the_device(tmp_path):
    adb = RootAdb(make_archive(tmp_path / "device.tar", {}), pull_code=1)

    result = run_risk(tmp_path, adb)

    assert result.final_status == "FAILED"
    assert "Pulling the private data archive failed" in result.errors[0]
    assert adb.root_commands[-1] == f"rm -f /data/local/tmp/mobile-playbook-private-data-{PACKAGE}.tar"


def test_tar_warnings_about_skipped_files_do_not_stop_the_scan(tmp_path):
    archive = make_archive(tmp_path / "device.tar", {f"{DATA}/shared_prefs/settings.xml": PLAIN_PREFS})

    result = run_risk(tmp_path, RootAdb(archive, tar_code=1, user_de=True))

    assert result.final_status == "PRIVATE_DATA_NO_FINDINGS"
    assert result.metadata["tar_warnings"] == ["tar: socket ignored"]
    assert result.metadata["directories"] == [f"/data/data/{PACKAGE}", f"/data/user_de/0/{PACKAGE}"]


def test_the_archive_is_kept_in_the_work_directory_only_when_asked(tmp_path):
    archive = make_archive(tmp_path / "device.tar", {f"{DATA}/shared_prefs/settings.xml": PLAIN_PREFS})

    result = run_risk(tmp_path, RootAdb(archive), private_data_config={"keep_archive": True})

    assert result.metadata["archive"] == str(pulled_archive())
    assert pulled_archive().is_file()
    assert not any(path.suffix == ".tar" for path in (tmp_path / "report").iterdir())


def test_a_launch_that_monkey_aborts_is_recorded_and_the_data_is_still_read(tmp_path):
    archive = make_archive(tmp_path / "device.tar", {f"{DATA}/shared_prefs/settings.xml": PLAIN_PREFS})

    result = run_risk(tmp_path, RootAdb(archive, monkey_out="** No activities found to run, monkey aborted."))

    assert result.metadata["launch"]["launched"] is False
    assert result.final_status == "PRIVATE_DATA_NO_FINDINGS"


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
    import base64

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


def test_the_risk_runs_on_the_rooted_role():
    from mobile_playbook.platforms.android.device_roles import ROOTED, risk_role

    assert risk_role(AndroidPrivateDataRisk()) == ROOTED
    assert private_data.AndroidPrivateDataRisk.risk_id == "android-feature-07-risk-01"
