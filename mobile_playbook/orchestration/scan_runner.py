from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from mobile_playbook.orchestration.scheduler import new_run_timestamp


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
    run_timestamp = run_timestamp or new_run_timestamp(options.out_dir, extra_files=("{timestamp}-acquire-results.json",))
    writer = report_writer_factory(options.out_dir, run_timestamp)
    client = None
    try:
        if platform_runner.requires_device(config, options.selected_tests, options.selected_apps):
            client = platform_runner.connect_device(config, writer.run_dir)
        for app, test_id in platform_runner.iter_enabled_tests(config, options.selected_tests, options.selected_apps):
            if client is not None:
                # Re-checked before every test: if Appium died since the last
                # test, this restarts it and reconnects so the remaining
                # tests in this run still get a chance to pass, instead of
                # every one of them failing the same way for the rest of
                # the run.
                client = platform_runner.ensure_device_healthy(config, client, writer.run_dir)
            platform_runner.run_test(app, test_id, config, client, writer)
    finally:
        writer.write_summary()
        if client is not None:
            platform_runner.close_device(client)
    completed = writer.completed_at.isoformat() if getattr(writer, "completed_at", None) else None
    return RunOutcome(run_timestamp=run_timestamp, run_dir=writer.run_dir, completed_at=completed)
