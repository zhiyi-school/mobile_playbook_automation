from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass, replace
from typing import Any, Iterable

from mobile_playbook.api.settings import ENV_FILE
from mobile_playbook.artifact_store.resolver import app_icon_reference, configured_app_ids
from mobile_playbook.env_file import load_env_file

logger = logging.getLogger(__name__)

PLATFORMS = ("ios", "android")


@dataclass(frozen=True)
class BackfillCounts:
    scanned: int = 0
    linked: int = 0
    unavailable: int = 0
    skipped: int = 0
    ambiguous: int = 0
    failed: int = 0

    def plus(self, **changes: int) -> "BackfillCounts":
        return replace(self, **{key: getattr(self, key) + value for key, value in changes.items()})

    def as_dict(self) -> dict[str, int]:
        return {
            "scanned": self.scanned,
            "linked": self.linked,
            "unavailable": self.unavailable,
            "skipped": self.skipped,
            "ambiguous": self.ambiguous,
            "failed": self.failed,
        }


def _find_application(store: Any, app_id: str, platform: str) -> tuple[dict | None, str | None]:
    """Linked row first, then a single unlinked row by name. Never guesses between several."""
    linked = store.find_application_by_external_id_and_platform(app_id, platform)
    if linked is not None:
        return linked, None

    name = _configured_app_name(platform, app_id)
    finder = getattr(store, "find_unlinked_applications", None)
    if not name or finder is None:
        return None, "skipped"
    try:
        candidates = finder(name, platform)
    except Exception:
        return None, "failed"
    if len(candidates) > 1:
        return None, "ambiguous"
    return (candidates[0], None) if candidates else (None, "skipped")


def _configured_app_name(platform: str, app_id: str) -> str | None:
    from mobile_playbook.artifact_store.resolver import app_display_name

    return app_display_name(platform, app_id)


def backfill_platform(
    platform: str,
    store: Any,
    app_ids: Iterable[str] | None = None,
    force: bool = False,
    dry_run: bool = False,
) -> BackfillCounts:
    """Fill in icon references for apps that already exist in the dashboard."""
    counts = BackfillCounts()
    for app_id in app_ids if app_ids is not None else configured_app_ids(platform):
        counts = counts.plus(scanned=1)
        application, problem = _find_application(store, app_id, platform)
        if problem is not None:
            if problem == "ambiguous":
                logger.warning("%s: %s matches more than one unlinked application; skipping it.", platform, app_id)
            counts = counts.plus(**{problem: 1})
            continue

        reference = app_icon_reference(platform, app_id, force=force)
        if reference["icon_extraction_status"] == "failed":
            counts = counts.plus(failed=1)
            continue
        counts = counts.plus(**({"linked": 1} if reference["icon_ref"] else {"unavailable": 1}))
        if not dry_run:
            store.update_application(application["id"], reference)
    return counts


def _report(platform: str, counts: BackfillCounts, dry_run: bool) -> None:
    logger.info(
        "%s%s: %d configured app(s), %d linked, %d without a readable icon, "
        "%d skipped, %d ambiguous, %d failed.",
        "[dry run] " if dry_run else "",
        platform,
        counts.scanned,
        counts.linked,
        counts.unavailable,
        counts.skipped,
        counts.ambiguous,
        counts.failed,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="One-time backfill of application icon references for apps already in the dashboard."
    )
    parser.add_argument("--platform", choices=PLATFORMS, help="Only back-fill one platform.")
    parser.add_argument("--app", action="append", dest="apps", help="Only back-fill these config app ids.")
    parser.add_argument("--force", action="store_true", help="Re-extract even when an icon is already stored.")
    parser.add_argument("--dry-run", action="store_true", help="Report what would change without writing.")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    load_env_file(ENV_FILE)

    from mobile_playbook.dashboard_syncing.supabase import SupabaseRestStore

    try:
        store = SupabaseRestStore.from_env()
    except RuntimeError as exc:
        logger.error("%s", exc)
        return 2

    for platform in [args.platform] if args.platform else list(PLATFORMS):
        counts = backfill_platform(platform, store, args.apps, force=args.force, dry_run=args.dry_run)
        _report(platform, counts, args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
