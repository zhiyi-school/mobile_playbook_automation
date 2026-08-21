from __future__ import annotations

from mobile_playbook.reporting.run_events import append_event, read_events


def test_append_and_read_events_in_order(tmp_path):
    append_event(tmp_path, "risk_started", app_id="app_one", risk_id="risk_one")
    append_event(tmp_path, "risk_completed", app_id="app_one", risk_id="risk_one", verdict="At Risk")

    events, count = read_events(tmp_path)

    assert count == 2
    assert events[0]["type"] == "risk_started"
    assert events[1]["type"] == "risk_completed"
    assert events[1]["verdict"] == "At Risk"
    assert "timestamp" in events[0]


def test_read_events_since_only_returns_new_ones(tmp_path):
    append_event(tmp_path, "risk_started", app_id="app_one", risk_id="risk_one")
    first_events, since = read_events(tmp_path)
    assert len(first_events) == 1

    append_event(tmp_path, "risk_completed", app_id="app_one", risk_id="risk_one", verdict="Inconclusive")
    new_events, since = read_events(tmp_path, since=since)

    assert len(new_events) == 1
    assert new_events[0]["type"] == "risk_completed"


def test_read_events_missing_file_returns_empty(tmp_path):
    events, count = read_events(tmp_path / "does-not-exist")
    assert events == []
    assert count == 0


def test_read_events_ignores_incomplete_trailing_line(tmp_path):
    append_event(tmp_path, "risk_started", app_id="app_one", risk_id="risk_one")
    events_path = tmp_path / "events.jsonl"
    with events_path.open("a") as handle:
        handle.write('{"type": "risk_completed", "incomple')  # simulates a write still in progress

    events, count = read_events(tmp_path)

    assert count == 1
    assert events[0]["type"] == "risk_started"


def test_append_event_creates_run_dir_if_missing(tmp_path):
    run_dir = tmp_path / "reports" / "2026-01-01_00-00-00"
    append_event(run_dir, "appium_recovery", message="Appium unreachable, restarting")

    events, count = read_events(run_dir)
    assert count == 1
    assert events[0]["message"] == "Appium unreachable, restarting"
