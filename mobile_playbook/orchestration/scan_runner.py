from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Protocol

from mobile_playbook.orchestration.scheduler import reserve_run_timestamp
from mobile_playbook.reporting.messages import clean_message
from mobile_playbook.reporting.run_events import append_event
from mobile_playbook.reporting.run_manifest import COMPLETED, FAILED, write_manifest
from mobile_playbook.reporting.sarif_writer import write_sarif

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from mobile_playbook.platforms.ios.preflight import IosPreflightWarning


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

    def preflight_warnings(self, config: Any, planned_tests: list[tuple[Any, str]]) -> list[IosPreflightWarning]:
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
    platform_name = getattr(platform_runner, "platform", "")
    logger.debug("%s: run %s starting (out_dir=%s, selected_tests=%s, selected_apps=%s)", platform_name, run_timestamp, options.out_dir, options.selected_tests, options.selected_apps)
    run_started = time.monotonic()
    writer = report_writer_factory(options.out_dir, run_timestamp)
    logger.debug("%s: run %s writing to %s", platform_name, run_timestamp, getattr(writer, "run_dir", None))
    client = None
    attempted: list[dict[str, str]] = []
    artifacts: dict[str, str] = {}
    failure: BaseException | None = None
    try:
        planned_tests = list(
            platform_runner.iter_enabled_tests(config, options.selected_tests, options.selected_apps)
        )
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("%s: planned %s risk runs in order: %s", platform_name, len(planned_tests), [(str(getattr(item[0], "id", item[0])), item[-1]) for item in planned_tests])
        warning_checker = getattr(platform_runner, "preflight_warnings", None)
        warnings = warning_checker(config, planned_tests) if warning_checker is not None else []
        seen_warnings: set[tuple[str, str, tuple[str, ...]]] = set()
        for warning in warnings:
            key = (warning.code, warning.risk_id, tuple(warning.app_ids))
            if key in seen_warnings:
                logger.debug("%s: skipping duplicate preflight warning %s", platform_name, key)
                continue
            seen_warnings.add(key)
            logger.warning("%s: %s", warning.code, warning.message)
            append_event(
                writer.run_dir,
                "preflight_warning",
                code=warning.code,
                risk_id=warning.risk_id,
                message=warning.message,
                app_ids=list(warning.app_ids),
            )
        logger.debug("%s: preflight emitted %s distinct warnings (checker present=%s)", platform_name, len(seen_warnings), warning_checker is not None)
        if platform_runner.requires_device(config, options.selected_tests, options.selected_apps):
            logger.debug("%s: connecting device", platform_name)
            connect_started = time.monotonic()
            client = platform_runner.connect_device(config, writer.run_dir)
            logger.debug("%s: device connected in %.2fs (%s)", platform_name, time.monotonic() - connect_started, type(client).__name__)
        else:
            logger.debug("%s: no device needed for this run", platform_name)
        for index, (app, test_id) in enumerate(planned_tests, start=1):
            if client is not None:
                client = platform_runner.ensure_device_healthy(config, client, writer.run_dir)
            append_event(writer.run_dir, "risk_started", app_id=getattr(app, "id", app), risk_id=test_id)
            app_id = str(getattr(app, "id", app))
            attempted.append({"app_id": app_id, "risk_id": str(test_id)})
            if app_id not in artifacts:
                _record_artifact(artifacts, app_id, getattr(platform_runner, "platform", ""), app)
            logger.debug("%s: risk %s/%s starting %s for app %s", platform_name, index, len(planned_tests), test_id, app_id)
            risk_started = time.monotonic()
            platform_runner.run_test(app, test_id, config, client, writer)
            logger.debug("%s: risk %s for app %s finished in %.2fs", platform_name, test_id, app_id, time.monotonic() - risk_started)
    except BaseException as exc:
        logger.debug("%s: run %s aborted after %s attempted risks: %s", platform_name, run_timestamp, len(attempted), exc, exc_info=True)
        failure = exc
        raise
    finally:
        logger.debug("%s: run %s cleanup (failure=%s, device=%s)", platform_name, run_timestamp, type(failure).__name__ if failure is not None else None, client is not None)
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
        logger.debug("%s: run %s finished in %.2fs (attempted=%s, artifacts=%s)", platform_name, run_timestamp, time.monotonic() - run_started, len(attempted), artifacts)
    completed = _isoformat(getattr(writer, "completed_at", None))
    return RunOutcome(run_timestamp=run_timestamp, run_dir=writer.run_dir, completed_at=completed)


def _record_artifact(artifacts: dict[str, str], app_id: str, platform: str, app: Any) -> None:
    """Pin the build under test now; a later upload must not change what the dashboard shows."""
    try:
        from mobile_playbook.artifact_store.resolver import prepare_icon_for_app_config

        digest = prepare_icon_for_app_config(platform, app)
    except Exception as exc:
        logger.debug("%s: recording artifact for %s failed: %s", platform, app_id, exc, exc_info=True)
        logger.warning("Could not record the artifact for %s: %s", app_id, type(exc).__name__)
        return
    logger.debug("%s: artifact digest for %s is %s", platform, app_id, digest)
    if digest:
        artifacts[app_id] = digest


def _isoformat(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _best_effort(action: Callable[[], Any], description: str) -> None:
    """Cleanup must not replace the run failure being propagated."""
    logger.debug("cleanup: attempting to %s", description)
    try:
        action()
    except Exception as exc:
        logger.debug("cleanup: could not %s: %s", description, exc, exc_info=True)
        logger.error("Could not %s: %s", description, exc)
