from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timedelta

import pytest

from mobile_playbook import cli
from mobile_playbook.orchestration import work_retention
from mobile_playbook.orchestration.work_retention import WorkFolder, apply_prune, plan_prune

NOW = datetime(2026, 3, 10, 12, 0, 0).astimezone()
OLD_RUN = "2026-01-01_09-00-00"
NEW_RUN = "2026-03-05_09-00-00"
RETENTION = timedelta(days=30)
PROTECTED = (
    "ios/traffic_interception/capture.jsonl",
    "ios/traffic_interception/capture.health.json",
    "ios/appium-api.log",
    "dashboard-sync.log",
    "android/repackaging/release.keystore",
    "android/repackaging/example_app/original/base.apk",
    "ios/notes/scratch.txt",
)


def _write(path, content="x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def _age(path, when):
    stamp = when.timestamp()
    for item in [path, *path.rglob("*")]:
        os.utime(item, (stamp, stamp), follow_symlinks=False)


def _report(reports, run_id, status="completed", references=()):
    run_dir = reports / run_id
    _write(run_dir / "run_manifest.json", json.dumps({"status": status}))
    _write(run_dir / "ios" / "example_app" / "report.json", json.dumps({"evidence": list(references)}))
    return run_dir


@pytest.fixture
def roots(tmp_path):
    work, reports = tmp_path / "work", tmp_path / "reports"
    _write(work / "ios" / OLD_RUN / "example_app" / "unpacked.bin", "old run")
    _write(work / "ios" / NEW_RUN / "example_app" / "unpacked.bin", "new run")
    _write(work / "ios" / "acquired" / OLD_RUN / "example_app" / "App.ipa", "old ipa")
    _write(work / "ios" / "acquired" / NEW_RUN / "example_app" / "App.ipa", "new ipa")
    _age(_write(work / "ios" / "0123456789ab" / "example_app" / "file.bin", "legacy run").parents[1], NOW - timedelta(days=90))
    for relative in PROTECTED:
        _age(_write(work / relative), NOW - timedelta(days=365))
    _age(work / "ios" / "notes", NOW - timedelta(days=365))
    _report(reports, OLD_RUN)
    _report(reports, NEW_RUN)
    return work, reports


def _deleted(plan, work):
    return sorted(str(folder.path.relative_to(work)) for folder in plan.delete)


def test_only_old_unreferenced_run_folders_are_planned_for_deletion(roots):
    work, reports = roots

    plan = plan_prune(work, reports, RETENTION, now=NOW)

    assert _deleted(plan, work) == ["ios/0123456789ab", f"ios/{OLD_RUN}", f"ios/acquired/{OLD_RUN}"]
    assert {str(folder.path.relative_to(work)): reason for folder, reason in plan.keep} == {
        f"ios/{NEW_RUN}": "newer than the cutoff",
        f"ios/acquired/{NEW_RUN}": "newer than the cutoff",
    }
    assert plan.reclaimable_bytes == len("old run") + len("old ipa") + len("legacy run")


def test_state_and_unrecognized_folders_are_never_candidates(roots):
    work, reports = roots

    plan = plan_prune(work, reports, RETENTION, now=NOW)
    candidates = {folder.path for folder in plan.delete} | {folder.path for folder, _ in plan.keep}

    for relative in PROTECTED:
        assert not any(folder == work / relative or folder in (work / relative).parents for folder in candidates)


def test_a_retained_report_keeps_the_old_work_folder_it_references(roots):
    work, reports = roots
    _report(reports, NEW_RUN, references=[f"/somewhere/artifacts/work/ios/acquired/{OLD_RUN}/example_app/App.ipa"])

    plan = plan_prune(work, reports, RETENTION, now=NOW)

    assert (work / "ios" / "acquired" / OLD_RUN, "referenced by a retained report") in [
        (folder.path, reason) for folder, reason in plan.keep
    ]
    assert f"ios/{OLD_RUN}" in _deleted(plan, work)


def test_a_reference_from_a_report_past_the_cutoff_protects_nothing(roots):
    work, reports = roots
    _report(reports, OLD_RUN, references=[f"artifacts/work/ios/acquired/{OLD_RUN}/example_app/App.ipa"])

    plan = plan_prune(work, reports, RETENTION, now=NOW)

    assert f"ios/acquired/{OLD_RUN}" in _deleted(plan, work)


def test_runs_still_in_progress_are_kept(roots):
    work, reports = roots
    _report(reports, OLD_RUN, status="running")
    _write(reports / ".job_registry.json", json.dumps({"abc": {"status": "running", "run_timestamp": "0123456789ab"}}))

    plan = plan_prune(work, reports, RETENTION, now=NOW)

    assert _deleted(plan, work) == []
    assert {reason for _, reason in plan.keep} == {"run in progress", "newer than the cutoff"}


def test_a_folder_without_a_timestamp_is_aged_by_its_newest_file(roots):
    work, reports = roots
    fresh = _write(work / "ios" / "0123456789ab" / "example_app" / "fresh.bin")
    _age(fresh, NOW - timedelta(days=1))

    plan = plan_prune(work, reports, RETENTION, now=NOW)

    assert "ios/0123456789ab" not in _deleted(plan, work)


def test_apply_deletes_exactly_the_planned_folders(roots):
    work, reports = roots
    plan = plan_prune(work, reports, RETENTION, now=NOW)

    apply_prune(plan, work)

    assert not (work / "ios" / OLD_RUN).exists()
    assert not (work / "ios" / "acquired" / OLD_RUN).exists()
    assert (work / "ios" / NEW_RUN).is_dir()
    assert (work / "ios" / "acquired" / NEW_RUN).is_dir()
    for relative in PROTECTED:
        assert (work / relative).is_file()


def test_apply_refuses_a_folder_outside_the_work_root(roots, tmp_path):
    work, reports = roots
    outside = _write(tmp_path / "elsewhere" / OLD_RUN / "keep.txt").parent
    plan = plan_prune(work, reports, RETENTION, now=NOW)
    plan.delete = [*plan.delete, WorkFolder(path=outside, run_id=OLD_RUN, modified=NOW, size_bytes=0)]

    with pytest.raises(ValueError, match="refusing to delete"):
        apply_prune(plan, work)
    assert outside.is_dir()
    assert (work / "ios" / OLD_RUN).is_dir()


def test_the_cli_lists_by_default_and_deletes_only_with_apply(roots, monkeypatch, capsys):
    work, reports = roots
    ancient = "2000-01-01_00-00-00"
    _write(work / "ios" / ancient / "example_app" / "unpacked.bin")
    monkeypatch.setenv("WORK_DIR", str(work))
    monkeypatch.setenv("REPORTS_DIR", str(reports))
    monkeypatch.setattr(cli, "load_env_file", lambda path: None)

    assert cli.main(["prune-work"]) == 0
    assert "would delete" in capsys.readouterr().out
    assert (work / "ios" / ancient).is_dir()

    assert cli.main(["prune-work", "--apply"]) == 0
    assert "Deleted" in capsys.readouterr().out
    assert not (work / "ios" / ancient).exists()
    assert (work / "ios" / "traffic_interception" / "capture.jsonl").is_file()


def test_the_cli_rejects_a_retention_under_one_day(monkeypatch):
    monkeypatch.setattr(cli, "load_env_file", lambda path: None)

    assert cli.main(["prune-work", "--older-than", "0"]) == 2


def test_run_ids_are_recognized_by_shape():
    assert work_retention.is_run_id("2026-01-01_09-00-00")
    assert work_retention.is_run_id("2026-01-01_09-00-00-2")
    assert work_retention.is_run_id("0123456789ab")
    assert not work_retention.is_run_id("traffic_interception")
    assert not work_retention.is_run_id("acquired")


def _linked_runs(tmp_path, now):
    from mobile_playbook.platforms.ios.ipa.store import STORE_DIR_NAME, place_ipa

    work, reports = tmp_path / "work", tmp_path / "reports"
    source = _write(tmp_path / "intake" / "App.ipa", "ipa bytes" * 100)
    store_dir = work / "ios" / "acquired" / STORE_DIR_NAME
    runs = {
        "old": (now - timedelta(days=60)).strftime("%Y-%m-%d_%H-%M-%S"),
        "older": (now - timedelta(days=90)).strftime("%Y-%m-%d_%H-%M-%S"),
        "new": (now - timedelta(days=1)).strftime("%Y-%m-%d_%H-%M-%S"),
    }
    for run_id in runs.values():
        placed = work / "ios" / "acquired" / run_id / "example_app" / "original.ipa"
        placed.parent.mkdir(parents=True)
        place_ipa(source, placed, store_dir)
    reports.mkdir()
    return work, reports, runs, next(store_dir.glob("*.ipa"))


def test_a_stored_ipa_is_kept_while_a_retained_run_links_it(tmp_path):
    now = datetime.now().astimezone() + timedelta(hours=2)
    work, reports, runs, stored = _linked_runs(tmp_path, now)

    plan = plan_prune(work, reports, RETENTION, now=now)

    assert _deleted(plan, work) == sorted([f"ios/acquired/{runs['old']}", f"ios/acquired/{runs['older']}"])
    assert plan.stored_ipas == []
    assert plan.reclaimable_bytes == 0


def test_a_stored_ipa_is_deleted_with_the_last_run_that_links_it(tmp_path):
    now = datetime.now().astimezone() + timedelta(hours=2)
    work, reports, runs, stored = _linked_runs(tmp_path, now)
    shutil.rmtree(work / "ios" / "acquired" / runs["new"])

    plan = plan_prune(work, reports, RETENTION, now=now)

    assert plan.stored_ipas == [(stored, stored.stat().st_size)]
    assert plan.reclaimable_bytes == stored.stat().st_size

    apply_prune(plan, work)

    assert not stored.exists()
    assert list((work / "ios" / "acquired").iterdir()) == [stored.parent]


def test_a_freshly_stored_ipa_is_never_collected(tmp_path):
    now = datetime.now().astimezone()
    work, reports, runs, stored = _linked_runs(tmp_path, now)
    for run_id in runs.values():
        shutil.rmtree(work / "ios" / "acquired" / run_id)

    assert plan_prune(work, reports, RETENTION, now=now).stored_ipas == []
