"""
Runs every planned risk for one platform and writes the run's summary, manifest and SARIF export.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Protocol

from mobile_playbook.orchestration.run_timestamps import reserve_run_timestamp
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
    """Interface each platform implements so run_platform can plan, connect and run its risks."""

    platform: str
    # True when the risks themselves pull the build under test, so its digest is only known after they run.
    acquires_builds_during_tests: bool

    # Report whether any planned risk needs a connected device.
    def requires_device(self, config: Any, selected_tests: set[str] | None, selected_apps: set[str] | None) -> bool:
        ...

    # Connect to the device and return a client for the risks.
    def connect_device(self, config: Any, run_dir: Path | None = None) -> Any:
        ...

    # Release the device client.
    def close_device(self, device_client: Any) -> None:
        ...

    # Return a usable device client, reconnecting when the session has broken.
    def ensure_device_healthy(self, config: Any, device_client: Any, run_dir: Path | None = None) -> Any:
        ...

    # Yield the (app, risk_id) pairs to run.
    def iter_enabled_tests(self, config: Any, selected_tests: set[str] | None, selected_apps: set[str] | None):
        ...

    # Return run-level warnings about the planned risks.
    def preflight_warnings(self, config: Any, planned_tests: list[tuple[Any, str]]) -> list[IosPreflightWarning]:
        ...

    # Run one risk for one app and write its result.
    def run_test(self, app: Any, test_id: str, config: Any, device_client: Any, report_writer: Any) -> None:
        ...


# Reserve a run timestamp, run each planned risk and always write the summary, manifest and SARIF.
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
    artifact_versions: dict[str, str] = {}
    start_versions: dict[str, tuple[str, str | None] | None] = {}
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
        last_risk = {str(getattr(app, "id", app)): index for index, (app, _) in enumerate(planned_tests, start=1)}
        for index, (app, test_id) in enumerate(planned_tests, start=1):
            if client is not None:
                client = platform_runner.ensure_device_healthy(config, client, writer.run_dir)
            app_id = str(getattr(app, "id", app))
            if client is not None and app_id not in start_versions:
                start_versions[app_id] = _record_installed_version(
                    platform_runner, app, app_id, client, writer.run_dir, artifact_versions
                )
            append_event(writer.run_dir, "risk_started", app_id=getattr(app, "id", app), risk_id=test_id)
            attempted.append({"app_id": app_id, "risk_id": str(test_id)})
            acquires_build = getattr(platform_runner, "acquires_builds_during_tests", False)
            if not acquires_build and app_id not in artifacts:
                _record_artifact(artifacts, artifact_versions, app_id, getattr(platform_runner, "platform", ""), app)
            logger.debug("%s: risk %s/%s starting %s for app %s", platform_name, index, len(planned_tests), test_id, app_id)
            risk_started = time.monotonic()
            platform_runner.run_test(app, test_id, config, client, writer)
            logger.debug("%s: risk %s for app %s finished in %.2fs", platform_name, test_id, app_id, time.monotonic() - risk_started)
            if acquires_build:
                # A build left by an earlier run must not stand in for the one this risk just pulled.
                _record_artifact(artifacts, artifact_versions, app_id, getattr(platform_runner, "platform", ""), app)
            started_version = start_versions.get(app_id)
            if index == last_risk[app_id] and started_version is not None and client is not None:
                _check_installed_version(platform_runner, app, app_id, client, writer.run_dir, started_version)
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
                artifact_versions=artifact_versions,
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


# Pin the digest and version of the app build under test so a later upload cannot change what the dashboard shows.
def _record_artifact(
    artifacts: dict[str, str], artifact_versions: dict[str, str], app_id: str, platform: str, app: Any
) -> None:
    try:
        from mobile_playbook.artifact_store.resolver import artifact_version_for_app_config, prepare_icon_for_app_config

        digest = prepare_icon_for_app_config(platform, app)
        version = artifact_version_for_app_config(platform, app)
    except Exception as exc:
        logger.debug("%s: recording artifact for %s failed: %s", platform, app_id, exc, exc_info=True)
        logger.warning("Could not record the artifact for %s: %s", app_id, type(exc).__name__)
        return
    logger.debug("%s: artifact digest for %s is %s (version %s)", platform, app_id, digest, version)
    if digest:
        artifacts[app_id] = digest
    if version:
        artifact_versions[app_id] = version


# Return the (version, build number) a platform that tests the installed app reads from the device, or None when the
# platform has no reader or the read fails; a failure is logged and never stops the run.
def _read_installed_version(platform_runner: Any, app: Any, app_id: str, client: Any) -> tuple[str, str | None] | None:
    reader = getattr(platform_runner, "installed_app_version", None)
    if reader is None:
        return None
    platform = getattr(platform_runner, "platform", "")
    try:
        installed = reader(app, client)
        if installed is None:
            logger.warning("%s: the device reported no installed version for %s.", platform, app_id)
            return None
        version, build_number = installed
    except Exception as exc:
        logger.debug("%s: reading the installed version of %s failed: %s", platform, app_id, exc, exc_info=True)
        logger.warning("Could not read the installed version of %s: %s", app_id, type(exc).__name__)
        return None
    return version, build_number


# Record the installed version read before an app's first risk as the version the run tests, and return it.
def _record_installed_version(
    platform_runner: Any, app: Any, app_id: str, client: Any, run_dir: Path, artifact_versions: dict[str, str]
) -> tuple[str, str | None] | None:
    installed = _read_installed_version(platform_runner, app, app_id, client)
    if installed is None:
        return None
    version, build_number = installed
    platform = getattr(platform_runner, "platform", "")
    artifact_versions[app_id] = version
    logger.info("%s: testing %s version %s (build %s).", platform, app_id, version, build_number or "unknown")
    append_event(
        run_dir,
        "app_version",
        app_id=app_id,
        version=version,
        build_number=build_number,
        message=f"Testing {app_id} version {version} (build {build_number or 'unknown'}).",
    )
    return installed


# Read the installed version again after an app's last risk and record a warning event when it differs from the
# version the run started with, such as after a store reinstall; the recorded tested version is left unchanged.
def _check_installed_version(
    platform_runner: Any, app: Any, app_id: str, client: Any, run_dir: Path, started: tuple[str, str | None]
) -> None:
    finished = _read_installed_version(platform_runner, app, app_id, client)
    if finished is None or finished == started:
        return
    platform = getattr(platform_runner, "platform", "")
    message = (
        f"{app_id} changed from version {started[0]} (build {started[1] or 'unknown'}) to {finished[0]} "
        f"(build {finished[1] or 'unknown'}) during the run; its results may cover both versions."
    )
    logger.warning("%s: %s", platform, message)
    append_event(
        run_dir,
        "app_version_changed",
        app_id=app_id,
        version=finished[0],
        build_number=finished[1],
        previous_version=started[0],
        previous_build_number=started[1],
        message=message,
    )


# Return the value's ISO format, or None.
def _isoformat(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


# Run a cleanup action, logging its errors so they never replace the failure being propagated.
def _best_effort(action: Callable[[], Any], description: str) -> None:
    logger.debug("cleanup: attempting to %s", description)
    try:
        action()
    except Exception as exc:
        logger.debug("cleanup: could not %s: %s", description, exc, exc_info=True)
        logger.error("Could not %s: %s", description, exc)
