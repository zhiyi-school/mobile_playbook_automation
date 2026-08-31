from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from mobile_playbook.orchestration.scheduler import reserve_run_timestamp
from mobile_playbook.reporting.messages import clean_message
from mobile_playbook.reporting.run_events import append_event
from mobile_playbook.reporting.run_manifest import COMPLETED, FAILED, write_manifest
from mobile_playbook.reporting.sarif_writer import write_sarif

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RunOptions:
    out_dir: Path
    selected_tests: set[str] | None = None
    selected_apps: set[str] | None = None


@dataclass(frozen=True)
class RunOutcome:
    run_timestamp: str
    run_dir: Path
    completed_at: str | None


class PlatformRunner(Protocol):
    platform: str

    def requires_device(self, config: Any, selected_tests: set[str] | None, selected_apps: set[str] | None) -> bool:
        ...

    def connect_device(self, config: Any, run_dir: Path | None = None) -> Any:
        ...

    def close_device(self, device_client: Any) -> None:
        ...

    def ensure_device_healthy(self, config: Any, device_client: Any, run_dir: Path | None = None) -> Any:
        ...

    def iter_enabled_tests(self, config: Any, selected_tests: set[str] | None, selected_apps: set[str] | None):
        ...

    def run_test(self, app: Any, test_id: str, config: Any, device_client: Any, report_writer: Any) -> None:
        ...


def run_platform(
    config: Any,
    platform_runner: PlatformRunner,
    options: RunOptions,
    report_writer_factory: Callable[[Path, str], Any],
    run_timestamp: str | None = None,
) -> RunOutcome:
    run_timestamp = run_timestamp or reserve_run_timestamp(
        options.out_dir, extra_files=("{timestamp}-acquire-results.json",)
    )
    writer = report_writer_factory(options.out_dir, run_timestamp)
    client = None
    attempted: list[dict[str, str]] = []
    artifacts: dict[str, str] = {}
    failure: BaseException | None = None
    try:
        if platform_runner.requires_device(config, options.selected_tests, options.selected_apps):
            client = platform_runner.connect_device(config, writer.run_dir)
        for app, test_id in platform_runner.iter_enabled_tests(config, options.selected_tests, options.selected_apps):
            if client is not None:
                client = platform_runner.ensure_device_healthy(config, client, writer.run_dir)
            append_event(writer.run_dir, "risk_started", app_id=getattr(app, "id", app), risk_id=test_id)
            app_id = str(getattr(app, "id", app))
            attempted.append({"app_id": app_id, "risk_id": str(test_id)})
            if app_id not in artifacts:
                _record_artifact(artifacts, app_id, getattr(platform_runner, "platform", ""), app)
            platform_runner.run_test(app, test_id, config, client, writer)
    except BaseException as exc:
        failure = exc
        raise
    finally:
        _best_effort(writer.write_summary, "write the run summary")
        if client is not None:
            _best_effort(lambda: platform_runner.close_device(client), "close the device session")
        _best_effort(
            lambda: write_manifest(
                writer.run_dir,
                run_timestamp=run_timestamp,
                platform=getattr(platform_runner, "platform", ""),
                attempted=attempted,
                artifacts=artifacts,
                status=FAILED if failure is not None else COMPLETED,
                started_at=_isoformat(getattr(writer, "started_at", None)),
                completed_at=_isoformat(getattr(writer, "completed_at", None)),
                error=clean_message(str(failure)) if failure is not None else None,
            ),
            "write the run manifest",
        )
        _best_effort(lambda: write_sarif(writer.run_dir), "write the SARIF export")
    completed = _isoformat(getattr(writer, "completed_at", None))
    return RunOutcome(run_timestamp=run_timestamp, run_dir=writer.run_dir, completed_at=completed)


def _record_artifact(artifacts: dict[str, str], app_id: str, platform: str, app: Any) -> None:
    """Pin the build under test now; a later upload must not change what the dashboard shows."""
    try:
        from mobile_playbook.artifact_store.resolver import prepare_icon_for_app_config

        digest = prepare_icon_for_app_config(platform, app)
    except Exception as exc:
        logger.warning("Could not record the artifact for %s: %s", app_id, type(exc).__name__)
        return
    if digest:
        artifacts[app_id] = digest


def _isoformat(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _best_effort(action: Callable[[], Any], description: str) -> None:
    """Cleanup must not replace the run failure being propagated."""
    try:
        action()
    except Exception as exc:
        logger.error("Could not %s: %s", description, exc)
