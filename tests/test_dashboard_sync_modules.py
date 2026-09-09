from __future__ import annotations

from mobile_playbook.dashboard_syncing.contracts import SyncSummary
from mobile_playbook.dashboard_syncing.identity import finding_status, is_older_run, rows_by_app, sync_key
from mobile_playbook.dashboard_syncing import worker


def test_identity_mapping_needs_no_store_or_filesystem():
    rows = [{"app_id": "one"}, {"app_id": "two"}, {"app_id": "one"}]

    assert list(rows_by_app(rows)) == ["one", "two"]
    assert finding_status("At Risk") == "at_risk"
    assert finding_status("Reduced Risk") == "reduced_risk"
    assert finding_status("unknown") == "inconclusive"
    assert is_older_run("2026-01-01_12-00-00-2", "2026-01-01_12-00-00-10") is True
    assert sync_key("app::risk", "run", "history") == "app::risk::run::history"


def test_worker_entrypoint_accepts_an_injected_store_without_credentials(monkeypatch, tmp_path):
    store = object()
    seen = []

    monkeypatch.setattr(worker, "load_env_file", lambda path: seen.append(path))
    monkeypatch.setattr(worker, "sync_reports", lambda reports_dir, actual, **kwargs: SyncSummary())

    result = worker.main(
        ["--reports-dir", str(tmp_path)],
        store_factory=lambda: store,
    )

    assert result == 0
    assert seen
