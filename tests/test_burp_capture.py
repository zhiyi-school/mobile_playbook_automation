from __future__ import annotations

import json
import os

import pytest

from mobile_playbook.platforms.ios.burp_capture import poll_capture, snapshot_capture


def _line(entry: dict) -> bytes:
    return json.dumps(entry).encode() + b"\n"


def test_existing_content_is_skipped(tmp_path):
    path = tmp_path / "capture.jsonl"
    path.write_bytes(_line({"host": "old.example.com"}))
    cursor = snapshot_capture(path)
    with path.open("ab") as handle:
        handle.write(_line({"schema_version": 1, "scheme": "https", "host": "new.example.com"}))

    cursor, observed = poll_capture(cursor, [])

    assert cursor.path == path.resolve(strict=False)
    assert observed.matched_entries == [{"schema_version": 1, "scheme": "https", "host": "new.example.com"}]
    assert observed.new_line_count == 1


def test_missing_file_can_be_created_during_window(tmp_path):
    path = tmp_path / "capture.jsonl"
    cursor = snapshot_capture(path)
    path.write_bytes(_line({"schema_version": 1, "scheme": "https", "host": "api.example.com"}))

    cursor, observed = poll_capture(cursor, ["api.example.com"])

    assert cursor.existed is True
    assert observed.created_during_window is True
    assert observed.matched_entries == [{"schema_version": 1, "scheme": "https", "host": "api.example.com"}]


def test_missing_file_remains_silent(tmp_path):
    cursor = snapshot_capture(tmp_path / "missing.jsonl")

    _, observed = poll_capture(cursor, ["api.example.com"])

    assert observed.new_line_count == 0
    assert observed.source_unavailable is False
    assert observed.matched_entries == []


def test_valid_and_malformed_lines_are_counted_together(tmp_path):
    path = tmp_path / "capture.jsonl"
    cursor = snapshot_capture(path)
    path.write_bytes(
        b"invalid-json\n"
        + _line(
            {
                "schema_version": 1,
                "scheme": "https",
                "host": "api.example.com",
            }
        )
    )

    _, observed = poll_capture(cursor, ["api.example.com"])

    assert observed.new_line_count == 2
    assert observed.valid_entry_count == 1
    assert observed.malformed_entry_count == 1
    assert len(observed.matched_entries) == 1


def test_matching_and_unmatched_entries_are_counted_separately(tmp_path):
    path = tmp_path / "capture.jsonl"
    cursor = snapshot_capture(path)
    path.write_bytes(
        _line({"schema_version": 1, "scheme": "https", "host": "api.example.com", "path": "/matched"})
        + _line({"schema_version": 1, "scheme": "https", "host": "unrelated.example.com", "path": "/private"})
    )

    _, observed = poll_capture(cursor, ["api.example.com"])

    assert observed.valid_entry_count == 2
    assert observed.https_entry_count == 2
    assert observed.non_https_entry_count == 0
    assert observed.unmatched_entry_count == 1
    assert observed.matched_entries == [
        {"schema_version": 1, "scheme": "https", "host": "api.example.com", "path": "/matched"}
    ]


def test_malformed_json_and_invalid_utf8_are_counted(tmp_path):
    path = tmp_path / "capture.jsonl"
    cursor = snapshot_capture(path)
    path.write_bytes(b"not json\n\xff\n")

    _, observed = poll_capture(cursor, [])

    assert observed.new_line_count == 2
    assert observed.malformed_entry_count == 2
    assert observed.valid_entry_count == 0


def test_partial_line_is_completed_on_later_poll(tmp_path):
    path = tmp_path / "capture.jsonl"
    cursor = snapshot_capture(path)
    encoded = json.dumps({"schema_version": 1, "scheme": "https", "host": "api.example.com"}).encode()
    split = len(encoded) // 2
    path.write_bytes(encoded[:split])

    cursor, first = poll_capture(cursor, ["api.example.com"])

    assert first.new_line_count == 0
    assert first.trailing_partial_line is True
    assert cursor.pending == encoded[:split]

    with path.open("ab") as handle:
        handle.write(encoded[split:] + b"\n")
    cursor, second = poll_capture(cursor, ["api.example.com"])

    assert second.matched_entries == [{"schema_version": 1, "scheme": "https", "host": "api.example.com"}]
    assert second.trailing_partial_line is False
    assert cursor.pending == b""


def test_truncation_is_detected(tmp_path):
    path = tmp_path / "capture.jsonl"
    path.write_bytes(_line({"host": "api.example.com"}))
    cursor = snapshot_capture(path)
    path.write_bytes(b"x")

    same_cursor, observed = poll_capture(cursor, [])

    assert same_cursor == cursor
    assert observed.source_changed is True


def test_file_replacement_is_detected(tmp_path):
    path = tmp_path / "capture.jsonl"
    path.write_bytes(_line({"host": "old.example.com"}))
    cursor = snapshot_capture(path)
    replacement = tmp_path / "replacement.jsonl"
    replacement.write_bytes(_line({"host": "api.example.com"}))
    os.replace(replacement, path)
    if cursor.inode is None or path.stat().st_ino == cursor.inode:
        pytest.skip("filesystem does not expose distinct inode identities")

    _, observed = poll_capture(cursor, [])

    assert observed.source_changed is True


def test_exact_and_subdomain_hosts_match_normalized_expected_host(tmp_path):
    path = tmp_path / "capture.jsonl"
    cursor = snapshot_capture(path)
    path.write_bytes(
        _line({"schema_version": 1, "scheme": "https", "host": "API.EXAMPLE.COM."})
        + _line({"schema_version": 1, "scheme": "HTTPS", "host": "child.api.example.com"})
    )

    _, observed = poll_capture(cursor, ["https://API.EXAMPLE.COM.:443/path"])

    assert observed.matched_entries == [
        {"schema_version": 1, "scheme": "https", "host": "API.EXAMPLE.COM."},
        {"schema_version": 1, "scheme": "HTTPS", "host": "child.api.example.com"},
    ]


def test_hostname_suffix_attacks_do_not_match(tmp_path):
    path = tmp_path / "capture.jsonl"
    cursor = snapshot_capture(path)
    path.write_bytes(
        _line({"schema_version": 1, "scheme": "https", "host": "api.example.com.evil.test"})
        + _line({"schema_version": 1, "scheme": "https", "host": "notapi.example.com"})
    )

    _, observed = poll_capture(cursor, ["api.example.com"])

    assert observed.matched_entries == []
    assert observed.valid_entry_count == 2
    assert observed.unmatched_entry_count == 2


def test_empty_expected_hosts_matches_any_valid_new_entry(tmp_path):
    path = tmp_path / "capture.jsonl"
    cursor = snapshot_capture(path)
    path.write_bytes(_line({"schema_version": 1, "scheme": "https", "host": "anything.example.com"}))

    _, observed = poll_capture(cursor, [])

    assert observed.matched_entries == [
        {"schema_version": 1, "scheme": "https", "host": "anything.example.com"}
    ]


def test_http_and_missing_scheme_do_not_match(tmp_path):
    path = tmp_path / "capture.jsonl"
    cursor = snapshot_capture(path)
    path.write_bytes(
        _line({"schema_version": 1, "scheme": "http", "host": "api.example.com"})
        + _line({"schema_version": 1, "host": "api.example.com"})
    )

    _, observed = poll_capture(cursor, ["api.example.com"])

    assert observed.matched_entries == []
    assert observed.valid_entry_count == 1
    assert observed.https_entry_count == 0
    assert observed.non_https_entry_count == 1
    assert observed.malformed_entry_count == 1
    assert observed.unmatched_entry_count == 1


def test_unsupported_schema_version_is_malformed(tmp_path):
    path = tmp_path / "capture.jsonl"
    cursor = snapshot_capture(path)
    path.write_bytes(_line({"schema_version": 2, "scheme": "https", "host": "api.example.com"}))

    _, observed = poll_capture(cursor, ["api.example.com"])

    assert observed.matched_entries == []
    assert observed.valid_entry_count == 0
    assert observed.malformed_entry_count == 1
    assert observed.https_entry_count == 0


def test_large_preexisting_file_is_read_only_from_captured_offset(tmp_path):
    path = tmp_path / "capture.jsonl"
    path.write_bytes(b"x" * (2 * 1024 * 1024))
    cursor = snapshot_capture(path)
    with path.open("ab") as handle:
        handle.write(_line({"schema_version": 1, "scheme": "https", "host": "api.example.com"}))

    _, observed = poll_capture(cursor, ["api.example.com"])

    assert observed.new_line_count == 1
    assert observed.malformed_entry_count == 0
    assert observed.matched_entries == [{"schema_version": 1, "scheme": "https", "host": "api.example.com"}]


def test_existing_file_that_disappears_is_unavailable(tmp_path):
    path = tmp_path / "capture.jsonl"
    path.write_bytes(b"")
    cursor = snapshot_capture(path)
    path.unlink()

    _, observed = poll_capture(cursor, [])

    assert observed.source_unavailable is True
