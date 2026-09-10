from __future__ import annotations

import argparse
import logging
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from mobile_playbook.storage import reports_root

from mobile_playbook import sync_status
from mobile_playbook.dashboard_syncing.contracts import SupabaseRestError
from mobile_playbook.dashboard_syncing.orchestrator import sync_reports
from mobile_playbook.dashboard_syncing.supabase import SupabaseRestStore
from mobile_playbook.env_file import load_env_file
from mobile_playbook.sync_state import SyncBusy, single_instance

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sync completed automation reports into the dashboard database.")
    parser.add_argument("--reports-dir", default=str(reports_root()))
    parser.add_argument("--run-timestamp", action="append", dest="run_timestamps")
    parser.add_argument("--triggered-by", default=None)
    parser.add_argument(
        "--interval-seconds",
        type=float,
        default=None,
        help="Development-only in-process loop. Unattended operation uses post-run one-shot workers and a launchd recovery sweep.",
    )
    parser.add_argument(
        "--allow-legacy-report",
        action="store_true",
        help="Import report folders that predate the run manifest. Their completion is unverified.",
    )
    parser.add_argument("--force", action="store_true", help="Re-sync reports already recorded in the processed ledger.")
    parser.add_argument(
        "--lock-wait-seconds",
        type=float,
        default=0,
        help="Wait this long for another sync pass to release the host lock.",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    store_factory: Callable[[], SupabaseRestStore] = SupabaseRestStore.from_env,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.interval_seconds is not None and args.interval_seconds <= 0:
        parser.error("--interval-seconds must be greater than 0")
    if args.lock_wait_seconds < 0:
        parser.error("--lock-wait-seconds must be greater than or equal to 0")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    load_env_file(Path(".env"))
    try:
        store = store_factory()
    except SupabaseRestError as exc:
        logger.error(
            "%s. Set it in the environment or in .env; the service-role key must never be committed "
            "or exposed to the frontend.",
            exc,
        )
        return 2
    reports_dir = Path(args.reports_dir)
    while True:
        try:
            with single_instance(reports_dir, wait_seconds=args.lock_wait_seconds):
                summary = sync_reports(
                    reports_dir,
                    store,
                    run_timestamps=args.run_timestamps,
                    triggered_by=args.triggered_by,
                    allow_legacy_report=args.allow_legacy_report,
                    force=args.force,
                )
        except SyncBusy as exc:
            logger.info("dashboard sync: skipped, %s.", exc)
            if args.interval_seconds is None:
                return 0
            time.sleep(args.interval_seconds)
            continue
        except Exception as exc:
            sync_status.record_worker_pass(reports_dir, succeeded=False, error=str(exc))
            raise
        sync_status.record_worker_pass(
            reports_dir,
            succeeded=not summary.failed_reports,
            error=f"{summary.failed_reports} report(s) failed to sync" if summary.failed_reports else None,
        )
        logger.info(
            "dashboard sync: %d report(s), %d app(s), %d assessment(s), %d finding(s), "
            "%d unchanged report(s), %d skipped report(s), %d failed report(s).",
            summary.reports,
            summary.applications,
            summary.assessments,
            summary.findings,
            summary.unchanged_reports,
            summary.skipped_reports,
            summary.failed_reports,
        )
        if args.interval_seconds is None:
            return 1 if summary.failed_reports else 0
        time.sleep(args.interval_seconds)
