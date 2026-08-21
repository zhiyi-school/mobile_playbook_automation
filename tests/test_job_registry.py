from __future__ import annotations

import json

from mobile_playbook.api.job_registry import INTERRUPTED_ERROR, JobRegistry


def test_create_and_complete_persists_to_disk(tmp_path):
    persist_path = tmp_path / "job_registry.json"
    registry = JobRegistry(persist_path=persist_path)

    registry.create("2026-01-01_00-00-00", "ios", "configs/ios.yaml")
    registry.mark_completed("2026-01-01_00-00-00", tmp_path / "reports/2026-01-01_00-00-00")

    on_disk = json.loads(persist_path.read_text())
    assert on_disk["2026-01-01_00-00-00"]["status"] == "completed"


def test_reload_recovers_completed_and_failed_records(tmp_path):
    persist_path = tmp_path / "job_registry.json"
    first = JobRegistry(persist_path=persist_path)
    first.create("run-completed", "ios", "configs/ios.yaml")
    first.mark_completed("run-completed", tmp_path / "reports/run-completed")
    first.create("run-failed", "android", "configs/android.yaml")
    first.mark_failed("run-failed", "boom")

    second = JobRegistry(persist_path=persist_path)

    completed = second.get("run-completed")
    failed = second.get("run-failed")
    assert completed.status == "completed"
    assert failed.status == "failed"
    assert failed.error == "boom"


def test_reload_marks_still_running_record_as_failed(tmp_path):
    persist_path = tmp_path / "job_registry.json"
    first = JobRegistry(persist_path=persist_path)
    first.create("run-interrupted", "ios", "configs/ios.yaml")
    # No mark_completed/mark_failed — simulates the process dying mid-run.

    second = JobRegistry(persist_path=persist_path)

    record = second.get("run-interrupted")
    assert record.status == "failed"
    assert record.error == INTERRUPTED_ERROR
    # The interruption is itself persisted, so a third restart doesn't re-flag it.
    on_disk = json.loads(persist_path.read_text())
    assert on_disk["run-interrupted"]["status"] == "failed"


def test_reload_does_not_carry_over_platform_claims(tmp_path):
    persist_path = tmp_path / "job_registry.json"
    first = JobRegistry(persist_path=persist_path)
    assert first.try_claim_platform("ios") is True
    first.create("run-interrupted", "ios", "configs/ios.yaml")

    second = JobRegistry(persist_path=persist_path)

    assert second.try_claim_platform("ios") is True


def test_missing_persist_file_starts_empty(tmp_path):
    registry = JobRegistry(persist_path=tmp_path / "does_not_exist.json")
    assert registry.list() == []


def test_corrupt_persist_file_starts_empty(tmp_path):
    persist_path = tmp_path / "job_registry.json"
    persist_path.write_text("{not valid json")

    registry = JobRegistry(persist_path=persist_path)

    assert registry.list() == []


def test_malformed_record_is_skipped_not_fatal(tmp_path):
    persist_path = tmp_path / "job_registry.json"
    persist_path.write_text(json.dumps({"bad-run": {"unexpected_field": "nonsense"}}))

    registry = JobRegistry(persist_path=persist_path)

    assert registry.list() == []


def test_no_persist_path_stays_in_memory_only(tmp_path):
    registry = JobRegistry(persist_path=None)
    registry.create("run-one", "ios", "configs/ios.yaml")
    registry.mark_completed("run-one", tmp_path / "reports/run-one")

    assert registry.get("run-one").status == "completed"
