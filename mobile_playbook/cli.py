"""
Command-line entry point for validating configs and playbooks, listing risks, and running scans.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
import uuid
from pathlib import Path

from mobile_playbook.common.storage_paths import ios_work_dir, reports_root

from mobile_playbook.orchestration.scan_runner import RunOptions, run_platform
from mobile_playbook.orchestration.selection import (
    selected_app_csv,
    selected_csv,
    validate_app_selection,
    validate_risk_selection,
)
from mobile_playbook.platforms.android.config import ConfigError as AndroidConfigError
from mobile_playbook.platforms.android.config import load_config as load_android_config
from mobile_playbook.platforms.android.results import normalize_android_result
from mobile_playbook.platforms.android.risks import known_risks as known_android_risks
from mobile_playbook.platforms.android.risks import list_risks as list_android_risks
from mobile_playbook.platforms.android.runner import AndroidPlatformRunner
from mobile_playbook.platforms.ios.config import ConfigError, load_config
from mobile_playbook.platforms.ios.mutations.mutability import inspect_main_executable
from mobile_playbook.platforms.ios.ipa.plist_utils import inspect_ipa_metadata
from mobile_playbook.platforms.ios.ipa.unpacker import unpack_ipa
from mobile_playbook.common.env_file import load_env_file
from mobile_playbook.playbook import catalogue as playbook_catalogue
from mobile_playbook.dashboard_sync.trigger import trigger_dashboard_sync
from mobile_playbook.common.logging_setup import configure_logging
from mobile_playbook.reporting.messages import clean_message
from mobile_playbook.reporting.report_writer import ReportWriter
from mobile_playbook.platforms.ios.results import normalize_ios_result
from mobile_playbook.platforms.ios.risks import known_risks as known_ios_risks
from mobile_playbook.platforms.ios.risks import list_risks
from mobile_playbook.platforms.ios.runner import IosPlatformRunner

_KNOWN_RISKS_BY_PLATFORM = {"ios": known_ios_risks, "android": known_android_risks}
logger = logging.getLogger(__name__)


# Builds the argument parser for the validate, playbook, risk listing, run, acquire and inspect-ipa commands.
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m mobile_playbook")
    parser.add_argument("--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate")
    validate.add_argument("--config", required=True)
    validate.add_argument("--platform", choices=("ios", "android"), required=True)

    validate_playbook = sub.add_parser(
        "validate-playbook",
        help="Report what the configured Markdown playbook parses to, and every catalogue warning.",
    )
    validate_playbook.add_argument("--platform", choices=("ios", "android"), required=True)

    list_risks_parser = sub.add_parser("list-risks")
    list_risks_parser.add_argument("--platform", choices=("ios", "android"), required=True)

    run = sub.add_parser("run")
    run.add_argument("--config", required=True)
    run.add_argument("--platform", choices=("ios", "android"), required=True)
    run.add_argument("--apps", default=None, help="Comma-separated app IDs or names")
    run.add_argument("--risks", default=None, help="Comma-separated risk IDs")
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--out", default=str(reports_root()))

    run_all = sub.add_parser(
        "run-all",
        help="Run iOS and Android concurrently in one command, reusing 'run' for each platform. Reports stay in separate per-platform run folders.",
    )
    run_all.add_argument("--ios-config", required=True)
    run_all.add_argument("--android-config", required=True)
    run_all.add_argument("--apps", default=None, help="Comma-separated app IDs or names, applied to both platforms")
    run_all.add_argument("--risks", default=None, help="Comma-separated risk IDs, applied to both platforms")
    run_all.add_argument("--dry-run", action="store_true")
    run_all.add_argument("--out", default=str(reports_root()))

    acquire = sub.add_parser("acquire")
    acquire.add_argument("--config", required=True)
    acquire.add_argument("--apps", default=None, help="Comma-separated app IDs")
    acquire.add_argument("--out", default=str(ios_work_dir() / "acquired"))

    inspect = sub.add_parser("inspect-ipa")
    inspect.add_argument("--ipa", required=True)

    return parser


# Prints what the configured playbook parsed to and its warnings; never prints archive contents or secrets.
def _validate_playbook(platform: str) -> int:
    from mobile_playbook.api.services import playbook as playbook_service

    report = playbook_service.status(platform)
    logger.debug("cli: playbook status for %s readable=%s.", platform, report["readable"])
    print(f"platform:   {platform}")
    print(f"configured: {report['configured_path'] or '(not configured)'}")
    if not report["readable"]:
        print(f"error:      {report['error']}")
        return 1

    index = playbook_catalogue.get(platform)
    print(f"revision:   {report['revision']}")
    print(f"risks:      {report['risk_count']}")
    print(f"controls:   {report['control_count']}")

    print("\nrisk -> controls")
    for risk_id in sorted(index["risks"]):
        risk = index["risks"][risk_id]
        demonstration = sum(len(block.get("items") or []) for block in risk.get("demonstration") or [])
        print(f"  {risk_id}  controls={len(risk['controls'])}  demonstration_steps={demonstration}")
        for control_id in risk["controls"]:
            control = index["controls"][control_id]
            archive = "archive" if control.get("source_download_url") else "no-archive"
            print(f"    {control_id}  steps={control['step_count']}  {archive}  {control['title']}")

    orphans = [c for c in sorted(index["controls"]) if index["controls"][c]["risk_id"] not in index["risks"]]
    for control_id in orphans:
        print(f"  (no risk document)  {control_id}  steps={index['controls'][control_id]['step_count']}")

    warnings = report["warnings"]
    print(f"\nwarnings:   {len(warnings)}")
    for warning in warnings:
        where = warning.get("path") or warning.get("control_id") or warning.get("risk_id") or ""
        print(f"  [{warning['code']}] {warning.get('file') or ''} {where}".rstrip())
        print(f"      {warning['message']}")
    return 0


# Loads .env files, configures logging and dispatches the selected CLI command, returning its exit code.
def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    load_env_file(Path(".env"))
    if hasattr(args, "config"):
        load_env_file(Path(args.config).parent / ".env")
    if hasattr(args, "ios_config"):
        load_env_file(Path(args.ios_config).parent / ".env")
    if hasattr(args, "android_config"):
        load_env_file(Path(args.android_config).parent / ".env")
    configure_logging(args.verbose)
    logger.debug("cli: command %s with options %s.", args.command, sorted(vars(args)))
    try:
        if args.command == "validate":
            if args.platform == "android":
                load_android_config(Path(args.config), dry_run=False)
            else:
                load_config(Path(args.config), dry_run=False)
            print("Config is valid")
            return 0
        if args.command == "validate-playbook":
            return _validate_playbook(args.platform)
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
            validate_risk_selection(_KNOWN_RISKS_BY_PLATFORM[args.platform](), selected)
            logger.debug(
                "cli: %s run selection risks=%s apps=%s dry_run=%s out=%s.",
                args.platform,
                sorted(selected) if selected else None,
                sorted(selected_apps) if selected_apps else None,
                args.dry_run,
                args.out,
            )
            if args.dry_run:
                if args.platform == "android":
                    _print_android_dry_run(config, selected, selected_apps)
                else:
                    _print_dry_run(config, selected, selected_apps)
                return 0
            out_dir = Path(args.out)
            try:
                if args.platform == "android":
                    return _run_android(config, selected, selected_apps, out_dir)
                return _run(config, selected, selected_apps, out_dir)
            finally:
                logger.debug("cli: %s run finished; triggering dashboard sync for %s.", args.platform, out_dir)
                trigger_dashboard_sync(out_dir)
        if args.command == "run-all":
            ios_config = load_config(Path(args.ios_config), dry_run=args.dry_run)
            android_config = load_android_config(Path(args.android_config), dry_run=args.dry_run)
            selected = selected_csv(args.risks)
            selected_apps = selected_app_csv(args.apps)
            validate_app_selection(ios_config.apps, selected_apps)
            validate_app_selection(android_config.apps, selected_apps)
            validate_risk_selection(known_ios_risks() | known_android_risks(), selected)
            logger.debug(
                "cli: run-all selection risks=%s apps=%s dry_run=%s out=%s.",
                sorted(selected) if selected else None,
                sorted(selected_apps) if selected_apps else None,
                args.dry_run,
                args.out,
            )
            if args.dry_run:
                _print_dry_run(ios_config, selected, selected_apps)
                _print_android_dry_run(android_config, selected, selected_apps)
                return 0
            out_dir = Path(args.out)
            try:
                return _run_all(ios_config, android_config, selected, selected_apps, out_dir)
            finally:
                logger.debug("cli: run-all finished; triggering dashboard sync for %s.", out_dir)
                trigger_dashboard_sync(out_dir)
        if args.command == "acquire":
            config = load_config(Path(args.config), dry_run=False)
            selected_apps = selected_app_csv(args.apps)
            validate_app_selection(config.apps, selected_apps)
            return _acquire(config, selected_apps, Path(args.out))
        if args.command == "inspect-ipa":
            return _inspect_ipa(Path(args.ipa))
    except (ConfigError, AndroidConfigError) as exc:
        logger.debug("cli: %s failed with %d config error(s).", args.command, len(exc.errors))
        for error in exc.errors:
            print(f"CONFIG_INVALID: {error}", file=sys.stderr)
        return 2
    except Exception as exc:
        logger.debug("cli: %s failed.", args.command, exc_info=True)
        print(f"FAILED: {clean_message(str(exc))}", file=sys.stderr)
        return 1
    return 1



# Parses a comma-separated risk selection.
def _selected_risks(risks: str | None) -> set[str] | None:
    return selected_csv(risks)


# Parses a comma-separated app selection.
def _selected_apps(apps: str | None) -> set[str] | None:
    return selected_app_csv(apps)


# Validates the selected apps against the config's apps.
def _validate_app_selection(config, selected_apps: set[str] | None) -> None:
    validate_app_selection(config.apps, selected_apps)


# Prints the iOS dry-run plan for the selected risks and apps.
def _print_dry_run(config, selected_risks: set[str] | None, selected_apps: set[str] | None = None) -> None:
    for line in IosPlatformRunner().dry_run_lines(config, selected_risks, selected_apps):
        print(line)


# Prints the Android dry-run plan for the selected risks and apps.
def _print_android_dry_run(config, selected_risks: set[str] | None, selected_apps: set[str] | None = None) -> None:
    for line in AndroidPlatformRunner().dry_run_lines(config, selected_risks, selected_apps):
        print(line)


# Reserves a new run timestamp under root that also avoids existing acquire result files.
def _new_run_timestamp(root: Path, now=None) -> str:
    from mobile_playbook.orchestration.run_timestamps import new_run_timestamp

    return new_run_timestamp(root, now=now, extra_files=("{timestamp}-acquire-results.json",))


# Runs the iOS scan for the selection and prints where the reports were written.
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


# Creates a ReportWriter that normalizes iOS results.
def _ios_report_writer(out_dir: Path, run_timestamp: str) -> ReportWriter:
    return ReportWriter(out_dir, run_timestamp, result_adapter=normalize_ios_result, platform="ios")


# Runs the Android scan for the selection and prints where the reports were written.
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


# Creates a ReportWriter that normalizes Android results.
def _android_report_writer(out_dir: Path, run_timestamp: str) -> ReportWriter:
    return ReportWriter(out_dir, run_timestamp, result_adapter=normalize_android_result, platform="android")


# Runs the iOS and Android flows concurrently in threads, each into its own run folder, and combines exit codes.
def _run_all(
    ios_config,
    android_config,
    selected_risks: set[str] | None,
    selected_apps: set[str] | None,
    out_dir: Path,
) -> int:
    outcomes: dict[str, tuple[int, Exception | None]] = {}

    # Runs one platform flow and records its exit code or exception.
    def _invoke(name: str, fn) -> None:
        logger.debug("cli: run-all %s thread starting.", name)
        try:
            outcomes[name] = (fn(), None)
        except Exception as exc:
            logger.debug("cli: run-all %s thread raised.", name, exc_info=True)
            outcomes[name] = (1, exc)
        logger.debug("cli: run-all %s thread finished with code %s.", name, outcomes[name][0])

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
    logger.debug("cli: run-all outcomes %s.", {name: code for name, (code, _) in outcomes.items()})
    for name in ("ios", "android"):
        code, exc = outcomes[name]
        if exc is not None:
            print(f"FAILED ({name}): {clean_message(str(exc))}", file=sys.stderr)
            exit_code = 1
        elif code != 0:
            exit_code = code
    return exit_code


# Reports whether the iOS run for the selection needs a connected device.
def _run_requires_device(config, selected_risks: set[str] | None, selected_apps: set[str] | None = None) -> bool:
    return IosPlatformRunner().requires_device(config, selected_risks, selected_apps)


# Acquires iOS app artifacts for the selected apps and writes the results JSON to out_dir.
def _acquire(config, selected_apps: set[str] | None, out_dir: Path) -> int:
    run_timestamp = _new_run_timestamp(out_dir)
    logger.debug("cli: acquiring artifacts for apps %s as %s into %s.", selected_apps, run_timestamp, out_dir)
    results = IosPlatformRunner().acquire_artifacts(config, selected_apps, run_timestamp, out_dir)
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    results_path = Path(out_dir) / f"{run_timestamp}-acquire-results.json"
    results_path.write_text(json.dumps(results, indent=2, sort_keys=True))
    logger.debug("cli: wrote acquire results to %s.", results_path)
    return 0


# Prints IPA metadata and main-executable inspection as JSON after unpacking to a temp dir.
def _inspect_ipa(ipa_path: Path) -> int:
    metadata = inspect_ipa_metadata(ipa_path)
    temp_dir = Path("/tmp") / f"mobile-playbook-automation-inspect-{uuid.uuid4().hex[:8]}"
    logger.debug("cli: unpacking %s into %s.", ipa_path, temp_dir)
    app_dir = unpack_ipa(ipa_path, temp_dir)
    binary = inspect_main_executable(app_dir)
    print(json.dumps({"ipa": str(ipa_path), "metadata": metadata, "binary_inspection": binary.to_dict()}, indent=2, sort_keys=True))
    return 0
