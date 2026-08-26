from __future__ import annotations

import json

from mobile_playbook.platforms.ios.burp_capture import capture_line_count, read_new_capture_entries


def test_capture_line_count_missing_file_is_zero(tmp_path):
    assert capture_line_count(tmp_path / "capture.jsonl") == 0


def test_capture_line_count_counts_existing_lines(tmp_path):
    path = tmp_path / "capture.jsonl"
    path.write_text(json.dumps({"host": "a.example.com"}) + "\n" + json.dumps({"host": "b.example.com"}) + "\n")
    assert capture_line_count(path) == 2


def test_read_new_capture_entries_skips_lines_before_start(tmp_path):
    path = tmp_path / "capture.jsonl"
    path.write_text(
        json.dumps({"host": "old.example.com"}) + "\n" + json.dumps({"host": "new.example.com"}) + "\n"
    )
    entries = read_new_capture_entries(path, start_line=1, expected_hosts=[])
    assert entries == [{"host": "new.example.com"}]


def test_read_new_capture_entries_filters_by_expected_host(tmp_path):
    path = tmp_path / "capture.jsonl"
    path.write_text(
        json.dumps({"host": "api.example.com"}) + "\n" + json.dumps({"host": "unrelated.example.com"}) + "\n"
    )
    entries = read_new_capture_entries(path, start_line=0, expected_hosts=["api.example.com"])
    assert entries == [{"host": "api.example.com"}]


def test_read_new_capture_entries_empty_expected_hosts_matches_anything(tmp_path):
    path = tmp_path / "capture.jsonl"
    path.write_text(json.dumps({"host": "anything.example.com"}) + "\n")
    entries = read_new_capture_entries(path, start_line=0, expected_hosts=[])
    assert len(entries) == 1


def test_read_new_capture_entries_ignores_malformed_lines(tmp_path):
    path = tmp_path / "capture.jsonl"
    path.write_text("not json\n" + json.dumps({"host": "api.example.com"}) + "\n")
    entries = read_new_capture_entries(path, start_line=0, expected_hosts=[])
    assert entries == [{"host": "api.example.com"}]
