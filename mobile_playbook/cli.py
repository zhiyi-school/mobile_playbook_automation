from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import uuid
from pathlib import Path

from mobile_playbook.orchestration.scan_runner import RunOptions, run_platform
from mobile_playbook.orchestration.artifact_intake import selected_app_csv, selected_csv, validate_app_selection
from mobile_playbook.platforms.android.config import ConfigError as AndroidConfigError
from mobile_playbook.platforms.android.config import load_config as load_android_config
from mobile_playbook.platforms.android.results import normalize_android_result
from mobile_playbook.platforms.android.risks import list_risks as list_android_risks
from mobile_playbook.platforms.android.runner import AndroidPlatformRunner
from mobile_playbook.platforms.ios.config import ConfigError, load_config
from mobile_playbook.platforms.ios.mutations.mutability import inspect_main_executable
from mobile_playbook.platforms.ios.ipa.plist_utils import inspect_ipa_metadata
from mobile_playbook.platforms.ios.ipa.unpacker import unpack_ipa
from mobile_playbook.logging_setup import configure_logging
from mobile_playbook.reporting.report_writer import ReportWriter
from mobile_playbook.platforms.ios.results import normalize_ios_result
from mobile_playbook.platforms.ios.risks import list_risks
from mobile_playbook.platforms.ios.runner import IosPlatformRunner


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m mobile_playbook")
    parser.add_argument("--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate")
    validate.add_argument("--config", required=True)
    validate.add_argument("--platform", choices=("ios", "android"), required=True)

    list_risks_parser = sub.add_parser("list-risks")
    list_risks_parser.add_argument("--platform", choices=("ios", "android"), required=True)

    run = sub.add_parser("run")
    run.add_argument("--config", required=True)
    run.add_argument("--platform", choices=("ios", "android"), required=True)
    run.add_argument("--apps", default=None, help="Comma-separated app IDs or names")
    run.add_argument("--risks", default=None, help="Comma-separated risk IDs")
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--out", default="reports")

    run_all = sub.add_parser(
        "run-all",
        help="Run iOS and Android concurrently in one command, reusing 'run' for each platform. Reports stay in separate per-platform run folders.",
    )
    run_all.add_argument("--ios-config", required=True)
    run_all.add_argument("--android-config", required=True)
    run_all.add_argument("--apps", default=None, help="Comma-separated app IDs or names, applied to both platforms")
    run_all.add_argument("--risks", default=None, help="Comma-separated risk IDs, applied to both platforms")
    run_all.add_argument("--dry-run", action="store_true")
    run_all.add_argument("--out", default="reports")

    acquire = sub.add_parser("acquire")
    acquire.add_argument("--config", required=True)
    acquire.add_argument("--apps", default=None, help="Comma-separated app IDs")
    acquire.add_argument("--out", default="work/ios/acquired")

    inspect = sub.add_parser("inspect-ipa")
    inspect.add_argument("--ipa", required=True)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _load_env_file(Path(".env"))
    if hasattr(args, "config"):
        _load_env_file(Path(args.config).parent / ".env")
    if hasattr(args, "ios_config"):
        _load_env_file(Path(args.ios_config).parent / ".env")
    if hasattr(args, "android_config"):
        _load_env_file(Path(args.android_config).parent / ".env")
    configure_logging(args.verbose)
    try:
        if args.command == "validate":
            if args.platform == "android":
                load_android_config(Path(args.config), dry_run=False)
            else:
                load_config(Path(args.config), dry_run=False)
            print("Config is valid")
            return 0
        if args.command == "list-risks":
            risks = list_android_risks() if args.platform == "android" else list_risks()
            for risk in risks:
                if args.platform == "android":
                    print(f"{risk['risk_id']}: {risk['name']} (requires: {', '.join(risk['requires'])})")
                else:
                    print(f"{risk['risk_id']}: {risk['name']} (requires IPA: {risk['requires_ipa_artifact']})")
            return 0
        if args.command == "run":
            if args.platform == "android":
                config = load_android_config(Path(args.config), dry_run=args.dry_run)
            else:
                config = load_config(Path(args.config), dry_run=args.dry_run)
            selected = selected_csv(args.risks)
            selected_apps = selected_app_csv(args.apps)
            validate_app_selection(config.apps, selected_apps)
            if args.dry_run:
                if args.platform == "android":
                    _print_android_dry_run(config, selected, selected_apps)
                else:
                    _print_dry_run(config, selected, selected_apps)
                return 0
            if args.platform == "android":
                return _run_android(config, selected, selected_apps, Path(args.out))
            return _run(config, selected, selected_apps, Path(args.out))
        if args.command == "run-all":
            ios_config = load_config(Path(args.ios_config), dry_run=args.dry_run)
            android_config = load_android_config(Path(args.android_config), dry_run=args.dry_run)
            selected = selected_csv(args.risks)
            selected_apps = selected_app_csv(args.apps)
            validate_app_selection(ios_config.apps, selected_apps)
            validate_app_selection(android_config.apps, selected_apps)
            if args.dry_run:
                _print_dry_run(ios_config, selected, selected_apps)
                _print_android_dry_run(android_config, selected, selected_apps)
                return 0
            return _run_all(ios_config, android_config, selected, selected_apps, Path(args.out))
        if args.command == "acquire":
            config = load_config(Path(args.config), dry_run=False)
            selected_apps = selected_app_csv(args.apps)
            validate_app_selection(config.apps, selected_apps)
            return _acquire(config, selected_apps, Path(args.out))
        if args.command == "inspect-ipa":
            return _inspect_ipa(Path(args.ipa))
    except (ConfigError, AndroidConfigError) as exc:
        for error in exc.errors:
            print(f"CONFIG_INVALID: {error}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    return 1


def _load_env_file(path: Path) -> None:
    if not path.exists() or not path.is_file():
        return
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ[key] = value


def _selected_risks(risks: str | None) -> set[str] | None:
    return selected_csv(risks)


def _selected_apps(apps: str | None) -> set[str] | None:
    return selected_app_csv(apps)


def _validate_app_selection(config, selected_apps: set[str] | None) -> None:
    validate_app_selection(config.apps, selected_apps)


def _print_dry_run(config, selected_risks: set[str] | None, selected_apps: set[str] | None = None) -> None:
    for line in IosPlatformRunner().dry_run_lines(config, selected_risks, selected_apps):
        print(line)


def _print_android_dry_run(config, selected_risks: set[str] | None, selected_apps: set[str] | None = None) -> None:
    for line in AndroidPlatformRunner().dry_run_lines(config, selected_risks, selected_apps):
        print(line)


def _new_run_timestamp(root: Path, now=None) -> str:
    from mobile_playbook.orchestration.scheduler import new_run_timestamp

    return new_run_timestamp(root, now=now, extra_files=("{timestamp}-acquire-results.json",))


def _run(config, selected_risks: set[str] | None, selected_apps: set[str] | None, out_dir: Path) -> int:
    outcome = run_platform(
        config,
        IosPlatformRunner(),
        RunOptions(out_dir=out_dir, selected_tests=selected_risks, selected_apps=selected_apps),
        _ios_report_writer,
    )
    print(f"Reports completed at {outcome.completed_at or 'unknown'}")
    print(f"Reports written to {outcome.run_dir}")
    return 0


def _ios_report_writer(out_dir: Path, run_timestamp: str) -> ReportWriter:
    return ReportWriter(out_dir, run_timestamp, result_adapter=normalize_ios_result, platform="ios")


def _run_android(config, selected_risks: set[str] | None, selected_apps: set[str] | None, out_dir: Path) -> int:
    outcome = run_platform(
        config,
        AndroidPlatformRunner(),
        RunOptions(out_dir=out_dir, selected_tests=selected_risks, selected_apps=selected_apps),
        _android_report_writer,
    )
    print(f"Reports completed at {outcome.completed_at or 'unknown'}")
    print(f"Reports written to {outcome.run_dir}")
    return 0


def _android_report_writer(out_dir: Path, run_timestamp: str) -> ReportWriter:
    return ReportWriter(out_dir, run_timestamp, result_adapter=normalize_android_result, platform="android")


def _run_all(
    ios_config,
    android_config,
    selected_risks: set[str] | None,
    selected_apps: set[str] | None,
    out_dir: Path,
) -> int:
    """Run the existing iOS and Android `run` flows concurrently, unchanged.

    Each platform still picks its own run_timestamp and writes its own
    reports/<run_timestamp>/ folder via _run / _run_android, so results stay
    fully separate. Threads (not processes) are enough here because the work
    is I/O-bound: Appium/network calls and adb/apktool/xcodebuild subprocesses.
    """
    outcomes: dict[str, tuple[int, Exception | None]] = {}

    def _invoke(name: str, fn) -> None:
        try:
            outcomes[name] = (fn(), None)
        except Exception as exc:
            outcomes[name] = (1, exc)

    threads = [
        threading.Thread(
            target=_invoke,
            args=("ios", lambda: _run(ios_config, selected_risks, selected_apps, out_dir)),
        ),
        threading.Thread(
            target=_invoke,
            args=("android", lambda: _run_android(android_config, selected_risks, selected_apps, out_dir)),
        ),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    exit_code = 0
    for name in ("ios", "android"):
        code, exc = outcomes[name]
        if exc is not None:
            print(f"FAILED ({name}): {exc}", file=sys.stderr)
            exit_code = 1
        elif code != 0:
            exit_code = code
    return exit_code


def _run_requires_device(config, selected_risks: set[str] | None, selected_apps: set[str] | None = None) -> bool:
    return IosPlatformRunner().requires_device(config, selected_risks, selected_apps)


def _acquire(config, selected_apps: set[str] | None, out_dir: Path) -> int:
    run_timestamp = _new_run_timestamp(out_dir)
    results = IosPlatformRunner().acquire_artifacts(config, selected_apps, run_timestamp, out_dir)
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    (Path(out_dir) / f"{run_timestamp}-acquire-results.json").write_text(json.dumps(results, indent=2, sort_keys=True))
    return 0


def _inspect_ipa(ipa_path: Path) -> int:
    metadata = inspect_ipa_metadata(ipa_path)
    temp_dir = Path("/tmp") / f"mobile-playbook-automation-inspect-{uuid.uuid4().hex[:8]}"
    app_dir = unpack_ipa(ipa_path, temp_dir)
    binary = inspect_main_executable(app_dir)
    print(json.dumps({"ipa": str(ipa_path), "metadata": metadata, "binary_inspection": binary.to_dict()}, indent=2, sort_keys=True))
    return 0
