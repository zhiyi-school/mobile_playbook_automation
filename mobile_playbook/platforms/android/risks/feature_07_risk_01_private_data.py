"""
Android risk check for whether an app's private data directory, read as root on a rooted device, holds sensitive data
in plain text, as the playbook reads it with `su -c ls` and `su -c cat`.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import re
import sqlite3
import tarfile
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any
from xml.etree import ElementTree

from mobile_playbook.common.logging_setup import redacted
from mobile_playbook.common.storage_paths import android_work_dir
from mobile_playbook.platforms.android.device.fingerprint import installed_base_apk_sha256
from mobile_playbook.platforms.android.device.private_data import PrivateDataError, copy_private_data
from mobile_playbook.platforms.android.device.root import find_root_shell
from mobile_playbook.platforms.android.models import AndroidRiskRunResult
from mobile_playbook.platforms.android.risks.base import AndroidRisk
from mobile_playbook.platforms.static_analysis.findings import critical_markdown, critical_report, sensitive_information_flag, severity_rank
from mobile_playbook.platforms.static_analysis.sensitive_findings import (
    MAX_TEXT_SCAN_BYTES,
    classify_sensitive_string,
    dedupe_sensitive_findings,
    key_looks_sensitive,
    flatten_value,
    public_sensitive_findings,
    scan_text_content,
    sensitive_finding,
    sensitive_key_severity,
    value_looks_secret,
)

logger = logging.getLogger(__name__)

OWASP_REFERENCE = "https://mas.owasp.org/MASTG/tests/android/MASVS-STORAGE/MASTG-TEST-0001/"
MAX_DATABASE_SCAN_BYTES = 50 * 1024 * 1024
MAX_ROWS_PER_TABLE = 1000
MAX_FINDINGS_PER_FILE = 50
MAX_FINDINGS = 200
SQLITE_HEADER = b"SQLite format 3\x00"
SQLITE_SIDECAR_SUFFIXES = ("-wal", "-journal", "-shm")
ENCRYPTED_PREFS_KEYS = {
    "__androidx_security_crypto_encrypted_prefs_key_keyset__",
    "__androidx_security_crypto_encrypted_prefs_value_keyset__",
}
MEDIA_SIGNATURES = {
    b"\x1f\x8b": "gzip",
    b"PK\x03\x04": "zip",
    b"\x89PNG": "png",
    b"\xff\xd8\xff": "jpeg",
    b"GIF8": "gif",
    b"RIFF": "riff",
    b"dex\n": "dex",
    b"\x7fELF": "elf",
    b"vdex": "vdex",
    b"art\n": "art",
}
# Uniformly random bytes, as encryption produces, give a byte chi-square near 255 at any length; text and records give
# far more, so 330 leaves random data three standard deviations of room.
OPAQUE_MAX_CHI_SQUARE = 330
OPAQUE_MIN_BYTES = 256
CACHE_DIRS = {"cache", "code_cache", "app_webview"}
PII_KEY_TERMS_HIGH = ("nric", "passport", "creditcard", "cardnumber", "iban", "bankaccount", "accountnumber", "dateofbirth", "passcode")
PII_KEY_EXACT_HIGH = {"dob", "ssn", "cvv", "cvc", "pin"}
PII_KEY_TERMS_MEDIUM = ("address", "birthdate", "birthday", "fullname", "firstname", "lastname", "surname", "postalcode", "postcode", "zipcode")
PHONE_VALUE_RE = re.compile(r"^\+?[\d\s().-]{7,20}$")
PII_KEY_EXCLUSIONS = ("ipaddress", "macaddress", "serveraddress", "hostaddress", "bluetoothaddress")
NOT_PII_VALUES = {"true", "false", "null", "none", "0", "1"}
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
NRIC_FIN_RE = re.compile(r"\b[STFGM]\d{7}[A-Z]\b")
CARD_RE = re.compile(r"\b[3-6](?:[ -]?\d){12,18}\b")
BASE64_TEXT_RE = re.compile(r"^[A-Za-z0-9+/=_\-\s]+$")
PRINTABLE_RUN_RE = re.compile(rb"[\x20-\x7e]{4,}")
APP_RELATIVE_RE = re.compile(r"^/data/(?:data|user_de/\d+)/[^/]+/(.+)$")
SDK_FILES = (
    ("Firebase Installations", re.compile(r"^files/PersistedInstallation\..+\.json$")),
    ("Google Data Transport", re.compile(r"^databases/com\.google\.android\.datatransport\.events(?:-wal|-journal|-shm)?$")),
    ("Google Analytics for Firebase", re.compile(r"^databases/google_app_measurement(?:_local)?\.db(?:-wal|-journal|-shm)?$")),
)
CODE_SUFFIXES = (".js", ".mjs", ".bundle", ".hbc", ".html", ".htm", ".css")


@dataclass
class ScannedFile:
    path: str
    size: int
    category: str
    findings: list[dict[str, Any]] = field(default_factory=list)
    note: str | None = None
    sdk: str | None = None

    # Report whether the file counts as an encrypted store: opaque or encrypted content outside the cache folders.
    @property
    def protected(self) -> bool:
        return self.category in {"encrypted_prefs", "opaque"} and not CACHE_DIRS.intersection(PurePosixPath(self.path).parts)

    # Describe the file for the findings report without raw values.
    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "size": self.size,
            "category": self.category,
            "protected": self.protected,
            "findings": len(self.findings),
            "note": self.note,
            "sdk": self.sdk,
        }


class AndroidPrivateDataRisk(AndroidRisk):
    risk_id = "android-feature-07-risk-01"
    feature_id = "feature-07"
    name = "Android Private Data Directory Test"
    test_case_id = "private_data_access"
    test_case_type = "adb_root_file_access"
    requires = ["adb", "root"]

    # Launch the app, copy its private data directories off the device as root, scan them and write the result.
    def run(self, app_config, global_config, device_client, report_writer):
        started_at = datetime.now().astimezone()
        package = app_config.package_name
        report_dir = report_writer.test_report_dir(app_config.id, self.risk_id, self.test_case_id, platform="android")
        cfg = {**(getattr(global_config, "private_data", None) or {}), **(app_config.risks.get(self.risk_id) or {})}
        result = AndroidRiskRunResult(
            run_timestamp=report_writer.run_timestamp,
            timestamp_start=started_at.isoformat(),
            timestamp_end=None,
            app_id=app_config.id,
            app_name=app_config.name,
            package_name=package,
            risk_id=self.risk_id,
            test_case_id=self.test_case_id,
            test_case_type=self.test_case_type,
        )
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("%s[%s]: setup package=%s report_dir=%s config=%s", self.risk_id, app_config.id, package, report_dir, redacted(cfg))
        adb = device_client.adb
        keep_archive = bool(cfg.get("keep_archive", False))
        archive = android_work_dir() / "private_data" / package.replace(".", "_") / f"{report_writer.run_timestamp}.tar"
        try:
            result.metadata["installed_base_apk_sha256"] = installed_base_apk_sha256(adb, package)
            root = find_root_shell(adb)
            if root is None:
                raise PrivateDataError("The device does not grant root, so the app's private data directory cannot be read.")
            result.metadata["root_method"] = root.method
            result.metadata["launch"] = self._launch_and_stop(adb, package, float(cfg.get("launch_wait", 5)))
            copied = copy_private_data(root, package, archive, int(float(cfg.get("max_archive_mb", 200)) * 1024 * 1024))
            result.metadata["directories"] = copied.directories
            result.metadata["archive_bytes"] = copied.size_bytes
            if copied.tar_warnings:
                result.metadata["tar_warnings"] = copied.tar_warnings[:20]
            listing_path = report_dir / "private_data_listing.txt"
            listing_path.write_text(copied.listing)
            result.evidence.append({"kind": "private_data_listing", "path": str(listing_path), "label": "Private data directories listed as root"})
            files = scan_archive(archive, bool(cfg.get("reveal_values", False)))
            self._record_scan(files, copied.directories, app_config, report_dir, result)
        except Exception as exc:
            logger.debug("%s[%s]: run failed: %s", self.risk_id, app_config.id, exc, exc_info=True)
            result.final_status = "FAILED"
            result.verdict = "Inconclusive"
            result.errors.append(str(exc))
        finally:
            if archive.exists():
                if keep_archive:
                    result.metadata["archive"] = str(archive)
                else:
                    archive.unlink()
            result.timestamp_end = datetime.now().astimezone().isoformat()
            logger.debug("%s[%s]: writing result final_status=%s verdict=%s errors=%s", self.risk_id, app_config.id, result.final_status, result.verdict, result.errors)
            report_writer.write_result(result, report_dir)
        return result

    # Open the app so it writes its data, then stop it so its files do not change while they are archived.
    def _launch_and_stop(self, adb, package: str, launch_wait: float) -> dict[str, Any]:
        code, out, err = adb.run(["shell", "monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1"])
        launched = code == 0 and "monkey aborted" not in (out or "").lower()
        logger.debug("%s[%s]: monkey launch exit %s launched=%s", self.risk_id, package, code, launched)
        if launched:
            time.sleep(launch_wait)
        adb.run(["shell", "am", "force-stop", package])
        if launched:
            return {"launched": True}
        return {"launched": False, "detail": (err or out or f"monkey exited {code}").strip()[:500]}

    # Decide the verdict from the scanned files and write the findings and critical findings reports.
    def _record_scan(self, files: list[ScannedFile], directories: list[str], app_config, report_dir: Path, result: AndroidRiskRunResult) -> None:
        findings = dedupe_sensitive_findings([item for scanned in files if not scanned.sdk for item in scanned.findings])[:MAX_FINDINGS]
        sdk_findings = dedupe_sensitive_findings([item for scanned in files if scanned.sdk for item in scanned.findings])[:MAX_FINDINGS]
        sdk_files = [scanned for scanned in files if scanned.sdk]
        protected = [scanned.path for scanned in files if scanned.protected]
        databases = [scanned.path for scanned in files if scanned.category == "sqlite"]
        if findings:
            result.final_status, result.verdict = "PRIVATE_DATA_EXPOSED", "At Risk"
        elif protected:
            result.final_status, result.verdict = "PRIVATE_DATA_PROTECTED", "Reduced Risk"
        else:
            result.final_status, result.verdict = "PRIVATE_DATA_NO_FINDINGS", "Inconclusive"
        result.metadata["file_count"] = len(files)
        result.metadata["categories"] = dict(Counter(scanned.category for scanned in files))
        result.metadata["finding_count"] = len(findings)
        result.metadata["sdk_finding_count"] = len(sdk_findings)
        result.metadata["sdk_files"] = [{"path": scanned.path, "sdk": scanned.sdk} for scanned in sdk_files][:50]
        result.metadata["protected_files"] = protected[:50]
        result.metadata["unencrypted_databases"] = databases[:50]
        logger.debug("%s[%s]: %s files, %s findings, %s protected -> %s", self.risk_id, app_config.id, len(files), len(findings), len(protected), result.final_status)

        findings_path = report_dir / "private_data_findings.json"
        findings_path.write_text(json.dumps({"files": [scanned.to_dict() for scanned in files], "findings": public_sensitive_findings(findings), "sdk_findings": public_sensitive_findings(sdk_findings)}, indent=2, sort_keys=True))
        result.evidence.append({"kind": "private_data_findings", "path": str(findings_path), "label": "Files read from the private data directories and their findings"})

        flags = [
            {
                "id": "PRIVATE_DATA_READABLE_WITH_ROOT",
                "severity": "LOW",
                "title": "The app's private data directory can be read with root",
                "evidence": [f"{directory}" for directory in directories] + [f"{len(files)} file(s) read"],
                "recommendation": "Assume files in the private data directory can be read on a rooted device. Encrypt sensitive data at rest with a key held in the Android Keystore.",
            }
        ]
        if findings:
            flags.append(sensitive_information_flag(findings, "the app's private data directory"))
        if sdk_findings:
            flags.append(
                {
                    "id": "SDK_DATA_IN_PLAIN_TEXT",
                    "severity": "LOW",
                    "title": "Third-party SDKs keep tokens or identifiers in plain text in the private data directory",
                    "evidence": [f"{scanned.sdk}: {scanned.path} ({len(scanned.findings)} finding(s))" for scanned in sdk_files if scanned.findings][:15],
                    "recommendation": "These values are written by third-party SDKs rather than the app's own code and do not decide the verdict. Check that they grant no access beyond the SDK's own service.",
                }
            )
        if databases:
            flags.append(
                {
                    "id": "UNENCRYPTED_DATABASES",
                    "severity": "LOW",
                    "title": "SQLite databases in the private data directory are not encrypted",
                    "evidence": databases[:15],
                    "recommendation": "Encrypt databases that hold sensitive data, for example with SQLCipher and a Keystore-protected key.",
                }
            )
        app = {"id": app_config.id, "name": app_config.name, "package_name": app_config.package_name}
        report = critical_report(app, flags, OWASP_REFERENCE)
        critical_path = report_dir / "critical_findings.json"
        critical_path.write_text(json.dumps(report, indent=2, sort_keys=True))
        markdown_path = report_dir / "critical_findings.md"
        markdown_path.write_text(critical_markdown(report, "Android Private Data Critical Findings", [f"- App: {app_config.name}", f"- Package: {app_config.package_name}"]))
        result.evidence.append({"kind": "critical_findings", "path": str(critical_path), "label": "Critical findings"})
        result.evidence.append({"kind": "critical_findings", "path": str(markdown_path), "label": "Critical findings (Markdown)"})


# Classify and scan every regular file in the pulled archive in memory, without extracting it.
def scan_archive(archive: Path, reveal_values: bool) -> list[ScannedFile]:
    files = []
    with tarfile.open(archive) as tar:
        for member in tar:
            if not member.isfile():
                continue
            name = PurePosixPath(member.name)
            if name.is_absolute() or ".." in name.parts:
                files.append(ScannedFile(member.name, member.size, "skipped", note="unsafe archive path"))
                continue
            path = "/" + str(name)
            if member.size > MAX_DATABASE_SCAN_BYTES:
                files.append(ScannedFile(path, member.size, "too_large", note=f"larger than {MAX_DATABASE_SCAN_BYTES} bytes, not scanned"))
                continue
            handle = tar.extractfile(member)
            data = handle.read() if handle is not None else b""
            files.append(scan_file(path, data, reveal_values))
    logger.debug("android private data: scanned %s files from %s", len(files), archive)
    return files


# Classify one file's content and collect the sensitive values readable in it.
def scan_file(path: str, data: bytes, reveal_values: bool) -> ScannedFile:
    scanned = ScannedFile(path, len(data), "empty", sdk=_sdk_of(path))
    if not data:
        return scanned
    personal_data = not path.lower().endswith(CODE_SUFFIXES)
    try:
        if data.startswith(SQLITE_HEADER):
            scanned.category = "sqlite"
            scanned.findings = _scan_database(path, data, reveal_values)
        elif path.endswith(SQLITE_SIDECAR_SUFFIXES):
            scanned.category = "sqlite_sidecar"
            scanned.findings = _scan_strings(path, data, reveal_values, personal_data)
        elif len(data) > MAX_TEXT_SCAN_BYTES:
            scanned.category, scanned.note = "too_large", f"larger than {MAX_TEXT_SCAN_BYTES} bytes, not scanned"
        elif "/shared_prefs/" in path and path.endswith(".xml"):
            scanned.category, scanned.findings = _scan_shared_prefs(path, data, reveal_values)
        elif (text := _as_text(data)) is not None:
            scanned.category, scanned.findings = _scan_text(path, data, text, reveal_values, personal_data)
        elif (media := _media_type(data)) is not None:
            scanned.category, scanned.note = "media", media
        elif _looks_opaque(data):
            scanned.category = "opaque"
        else:
            scanned.category = "binary"
            scanned.findings = _scan_strings(path, data, reveal_values, personal_data)
    except Exception as exc:
        logger.debug("android private data: scanning %s failed: %s", path, exc, exc_info=True)
        scanned.category, scanned.findings, scanned.note = "unreadable", [], f"scan failed: {exc}"
    scanned.findings = _merge_by_value(scanned.findings)[:MAX_FINDINGS_PER_FILE]
    return scanned


# Return the third-party SDK known to write this file, or None for the app's own files.
def _sdk_of(path: str) -> str | None:
    match = APP_RELATIVE_RE.match(path)
    if match is None:
        return None
    return next((name for name, pattern in SDK_FILES if pattern.match(match.group(1))), None)


# Keep one finding per value in a file, the most severe and most specific, listing every rule that matched it.
def _merge_by_value(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in findings:
        groups.setdefault((str(item.get("path")), str(item.get("_raw_value"))), []).append(item)
    merged = []
    for group in groups.values():
        chosen = dict(max(group, key=lambda item: (severity_rank(str(item.get("severity", ""))), bool(item.get("key_path")), bool(item.get("context")))))
        chosen["match_types"] = sorted({item["match_type"] for item in group})
        merged.append(chosen)
    return merged


# Scan a shared preferences file's entries, or recognise it as androidx EncryptedSharedPreferences.
def _scan_shared_prefs(path: str, data: bytes, reveal_values: bool) -> tuple[str, list[dict[str, Any]]]:
    root = ElementTree.fromstring(data)
    pairs = []
    for entry in root:
        key = entry.get("name") or ""
        if key in ENCRYPTED_PREFS_KEYS:
            return "encrypted_prefs", []
        if entry.tag == "set":
            pairs.extend((key, item.text or "") for item in entry)
        else:
            pairs.append((key, entry.text if entry.tag == "string" else entry.get("value")))
    return "shared_prefs", _key_value_findings(path, pairs, reveal_values)


# Scan a text file, reading JSON key names as well when it parses as JSON; base64 of opaque bytes counts as opaque, and
# app code is checked for secrets only.
def _scan_text(path: str, data: bytes, text: str, reveal_values: bool, personal_data: bool = True) -> tuple[str, list[dict[str, Any]]]:
    stripped = text.strip()
    if BASE64_TEXT_RE.match(stripped) and _looks_opaque(_decode_base64(stripped)):
        return "opaque", []
    findings = scan_text_content(data, path, reveal_values)
    if not personal_data:
        return "code", findings
    findings += _pii_value_findings(path, text, reveal_values)
    try:
        parsed = json.loads(text)
    except ValueError:
        return "text", findings
    return "json", findings + _key_value_findings(path, flatten_value(parsed), reveal_values)


# Scan the rows of every table in an unencrypted SQLite database, opened read-only from a temporary copy.
def _scan_database(path: str, data: bytes, reveal_values: bool) -> list[dict[str, Any]]:
    pairs = []
    with tempfile.TemporaryDirectory() as folder:
        copy = Path(folder) / "database.sqlite"
        copy.write_bytes(data)
        connection = sqlite3.connect(f"{copy.as_uri()}?immutable=1", uri=True)
        try:
            connection.execute("PRAGMA trusted_schema = OFF")
            tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
            for table in tables:
                try:
                    cursor = connection.execute(f'SELECT * FROM "{table.replace(chr(34), chr(34) * 2)}" LIMIT {MAX_ROWS_PER_TABLE}')
                    columns = [column[0] for column in cursor.description]
                    for row in cursor:
                        pairs.extend((f"{table}.{column}", _cell_text(value)) for column, value in zip(columns, row))
                except sqlite3.Error as exc:
                    logger.debug("android private data: reading table %s of %s failed: %s", table, path, exc)
        finally:
            connection.close()
    return _key_value_findings(path, pairs, reveal_values)


# Scan the printable strings in a binary file, such as a DataStore protobuf or a database journal.
def _scan_strings(path: str, data: bytes, reveal_values: bool, personal_data: bool = True) -> list[dict[str, Any]]:
    text = "\n".join(match.decode("ascii") for match in PRINTABLE_RUN_RE.findall(data[:MAX_TEXT_SCAN_BYTES]))
    if not text:
        return []
    findings = scan_text_content(text.encode(), path, reveal_values)
    return findings + _pii_value_findings(path, text, reveal_values) if personal_data else findings


# Flag values stored under secret or personal-data key names, and secret or personal-data values under any key.
def _key_value_findings(path: str, pairs: list[tuple[str, Any]], reveal_values: bool) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for key, value in pairs:
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            continue
        text = str(value).strip()
        leaf = key.rsplit(".", 1)[-1]
        pii_severity = _pii_key_severity(leaf, text)
        if isinstance(value, str) and key_looks_sensitive(leaf) and value_looks_secret(text):
            findings.append(sensitive_finding(path, "SENSITIVE_KEY_NAME", text, sensitive_key_severity(leaf), key_path=key, reveal_values=reveal_values))
        elif pii_severity and text.lower() not in NOT_PII_VALUES and len(text) >= 3:
            findings.append(sensitive_finding(path, "PERSONAL_DATA_KEY_NAME", text, pii_severity, key_path=key, reveal_values=reveal_values))
        if isinstance(value, str) and text:
            findings.extend(classify_sensitive_string(path, text, reveal_values, key_path=key))
            findings.extend(_pii_value_findings(path, text, reveal_values, key_path=key))
        if len(findings) >= MAX_FINDINGS_PER_FILE:
            break
    return findings


# Match email addresses, Singapore NRIC/FIN numbers and Luhn-valid payment card numbers in text.
def _pii_value_findings(path: str, text: str, reveal_values: bool, key_path: str | None = None) -> list[dict[str, Any]]:
    findings = []
    for match_type, severity, pattern in (("EMAIL_ADDRESS", "MEDIUM", EMAIL_RE), ("NRIC_FIN", "HIGH", NRIC_FIN_RE), ("PAYMENT_CARD_NUMBER", "HIGH", CARD_RE)):
        for match in pattern.finditer(text):
            if match_type == "PAYMENT_CARD_NUMBER" and not _luhn_valid(re.sub(r"\D", "", match.group(0))):
                continue
            findings.append(sensitive_finding(path, match_type, match.group(0), severity, key_path=key_path, reveal_values=reveal_values))
            if len(findings) >= 10:
                return findings
    return findings


# Return HIGH or MEDIUM when a key name names personal data, checking that email and phone values look like one.
def _pii_key_severity(key: str, value: str) -> str | None:
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    if not normalized or any(term in normalized for term in PII_KEY_EXCLUSIONS):
        return None
    if normalized in PII_KEY_EXACT_HIGH or any(term in normalized for term in PII_KEY_TERMS_HIGH):
        return "HIGH"
    if "email" in normalized:
        return "MEDIUM" if "@" in value else None
    if "phone" in normalized or "mobile" in normalized:
        digits = sum(char.isdigit() for char in value)
        return "MEDIUM" if PHONE_VALUE_RE.match(value) and 7 <= digits <= 15 else None
    if any(term in normalized for term in PII_KEY_TERMS_MEDIUM):
        return "MEDIUM"
    return None


# Report whether a digit string passes the Luhn checksum payment card numbers carry.
def _luhn_valid(digits: str) -> bool:
    total = 0
    for index, digit in enumerate(reversed(digits)):
        value = int(digit) * (2 if index % 2 else 1)
        total += value - 9 if value > 9 else value
    return total % 10 == 0


# Return the content as text when it is UTF-8 without NUL bytes and almost entirely printable, else None.
def _as_text(data: bytes) -> str | None:
    if b"\x00" in data[:4096]:
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    printable = sum(1 for char in text if char.isprintable() or char in "\r\n\t")
    return text if printable >= 0.95 * len(text) else None


# Return a cell value as a string to scan, decoding text stored as bytes.
def _cell_text(value: Any) -> Any:
    if isinstance(value, bytes):
        return _as_text(value)
    return value


# Return the name of a known media, archive or code format the content starts with, or None.
def _media_type(data: bytes) -> str | None:
    return next((name for signature, name in MEDIA_SIGNATURES.items() if data.startswith(signature)), None)


# Report whether the content's bytes are as evenly spread as random data, as encrypted data is.
def _looks_opaque(data: bytes) -> bool:
    sample = data[: 64 * 1024]
    if len(sample) < OPAQUE_MIN_BYTES:
        return False
    expected = len(sample) / 256
    counts = Counter(sample)
    chi_square = sum((counts.get(value, 0) - expected) ** 2 for value in range(256)) / expected
    return chi_square <= OPAQUE_MAX_CHI_SQUARE


# Decode standard or URL-safe base64 text, returning no bytes when it is not base64.
def _decode_base64(text: str) -> bytes:
    compact = re.sub(r"\s", "", text)
    try:
        return base64.b64decode(compact + "=" * (-len(compact) % 4), altchars=b"-_" if ("-" in compact or "_" in compact) else None, validate=True)
    except (binascii.Error, ValueError):
        return b""
