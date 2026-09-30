from __future__ import annotations

from pathlib import Path

import pytest

from mobile_playbook.common.storage_paths import work_root
from types import SimpleNamespace

from mobile_playbook.dashboard_sync import trigger
from mobile_playbook.api.services import runs as runs_service
from mobile_playbook.orchestration.scan_runner import RunOptions


@pytest.fixture(autouse=True)
def isolated_work_root(tmp_path, monkeypatch):
    # The trigger opens and rotates work_root()/dashboard-sync.log; keep it off the real work folder.
    monkeypatch.setenv("WORK_DIR", str(tmp_path / "work"))


def test_post_run_trigger_starts_detached_worker(monkeypatch, tmp_path):
    repository = tmp_path / "repository"
    reports = tmp_path / "reports"
    calls = []

    def fake_popen(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(pid=4321)

    monkeypatch.delenv(trigger.AUTO_TRIGGER_ENV, raising=False)
    monkeypatch.setattr(trigger, "REPOSITORY_ROOT", repository)
    monkeypatch.setattr(trigger.subprocess, "Popen", fake_popen)

    pid = trigger.trigger_dashboard_sync(reports)

    assert pid == 4321
    command, kwargs = calls[0]
    assert command[1:] == [
        "-m",
        "mobile_playbook.dashboard_sync",
        "--reports-dir",
        str(reports.resolve()),
        "--lock-wait-seconds",
        "60",
    ]
    assert kwargs["cwd"] == repository
    assert kwargs["start_new_session"] is True
    assert kwargs["env"]["PYTHONUNBUFFERED"] == "1"
    assert Path(kwargs["stdout"].name) == work_root() / "dashboard-sync.log"


def test_post_run_trigger_can_be_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv(trigger.AUTO_TRIGGER_ENV, "false")

    def unexpected_popen(*args, **kwargs):
        raise AssertionError("disabled trigger must not launch a worker")

    monkeypatch.setattr(trigger.subprocess, "Popen", unexpected_popen)

    assert trigger.trigger_dashboard_sync(tmp_path) is None


def test_post_run_trigger_reads_only_its_flag_from_repository_env(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text(
        'SUPABASE_SERVICE_ROLE_KEY="must_not_be_loaded"\n'
        'DASHBOARD_SYNC_AUTO_TRIGGER="off"\n'
    )
    environment = {}

    assert trigger.auto_trigger_enabled(environment, env_path) is False
    assert environment == {}


def test_post_run_trigger_failure_does_not_change_run_outcome(monkeypatch, tmp_path):
    monkeypatch.delenv(trigger.AUTO_TRIGGER_ENV, raising=False)
    monkeypatch.setattr(trigger, "REPOSITORY_ROOT", tmp_path)

    def fail_to_start(*args, **kwargs):
        raise OSError("process table unavailable")

    monkeypatch.setattr(trigger.subprocess, "Popen", fail_to_start)

    assert trigger.trigger_dashboard_sync(tmp_path / "reports") is None


def test_api_completion_updates_registry_before_trigger(monkeypatch, tmp_path):
    events = []
    run_dir = tmp_path / "reports" / "run-id"
    options = RunOptions(out_dir=tmp_path / "reports")

    monkeypatch.setattr(
        runs_service,
        "run_platform",
        lambda *args, **kwargs: SimpleNamespace(run_dir=run_dir),
    )
    monkeypatch.setattr(runs_service.registry, "mark_completed", lambda *args: events.append("completed"))
    monkeypatch.setattr(runs_service.registry, "release_platform", lambda *args: events.append("released"))
    monkeypatch.setattr(runs_service, "trigger_dashboard_sync", lambda *args: events.append("triggered"))

    runs_service.execute_run("run-id", "ios", object(), options)

    assert events == ["completed", "released", "triggered"]


def test_api_failure_updates_registry_before_trigger(monkeypatch, tmp_path):
    events = []
    options = RunOptions(out_dir=tmp_path / "reports")

    def fail_run(*args, **kwargs):
        raise RuntimeError("device unavailable")

    monkeypatch.setattr(runs_service, "run_platform", fail_run)
    monkeypatch.setattr(runs_service.registry, "mark_failed", lambda *args: events.append("failed"))
    monkeypatch.setattr(runs_service.registry, "release_platform", lambda *args: events.append("released"))
    monkeypatch.setattr(runs_service, "trigger_dashboard_sync", lambda *args: events.append("triggered"))

    runs_service.execute_run("run-id", "ios", object(), options)

    assert events == ["failed", "released", "triggered"]


def test_the_sync_log_is_rotated_once_it_passes_the_size_limit(tmp_path):
    log = tmp_path / "dashboard-sync.log"
    log.write_text("new" * 10)
    (tmp_path / "dashboard-sync.log.1").write_text("older")

    assert trigger.rotate_log(log, max_bytes=16) is True

    assert not log.exists()
    assert (tmp_path / "dashboard-sync.log.1").read_text() == "new" * 10


def test_a_small_or_missing_sync_log_is_left_alone(tmp_path):
    log = tmp_path / "dashboard-sync.log"

    assert trigger.rotate_log(log, max_bytes=16) is False
    log.write_text("small")
    assert trigger.rotate_log(log, max_bytes=16) is False
    assert log.read_text() == "small"


def test_a_failed_rotation_still_starts_the_worker(monkeypatch, tmp_path):
    monkeypatch.delenv(trigger.AUTO_TRIGGER_ENV, raising=False)
    monkeypatch.setattr(trigger, "LOG_MAX_BYTES", 0)
    log = work_root() / "dashboard-sync.log"
    log.parent.mkdir(parents=True)
    log.write_text("x")

    def refuse(*args, **kwargs):
        raise PermissionError("read-only")

    monkeypatch.setattr(trigger.os, "replace", refuse)
    monkeypatch.setattr(trigger.subprocess, "Popen", lambda command, **kwargs: SimpleNamespace(pid=99))

    assert trigger.trigger_dashboard_sync(tmp_path / "reports") == 99
