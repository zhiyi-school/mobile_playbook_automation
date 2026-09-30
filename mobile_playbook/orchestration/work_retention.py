"""
Plans and applies the pruning of old per-run work folders. See docs/operations.md#pruning-old-run-work-folders.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from mobile_playbook.platforms.ios.ipa.store import STORE_DIR_NAME
from mobile_playbook.reporting.run_manifest import COMPLETED, FAILED, read_manifest

logger = logging.getLogger(__name__)

DEFAULT_RETENTION_DAYS = 30
REGISTRY_NAME = ".job_registry.json"
TIMESTAMP_FORMAT = "%Y-%m-%d_%H-%M-%S"
TIMESTAMP_RUN_ID = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}(-\d+)?$")
HEX_RUN_ID = re.compile(r"^[0-9a-f]{12}$")
# Captures a run folder's path under the work root from a recorded path, whether absolute or relative.
WORK_REFERENCE = re.compile(r"work/((?:ios|android)/(?:acquired/)?[^/\"\\]+)/")
TERMINAL_STATUSES = {COMPLETED, FAILED}
# A stored IPA this new may be between being stored and being linked by its run.
STORE_GRACE = timedelta(hours=1)


@dataclass(frozen=True)
class WorkFolder:
    path: Path
    run_id: str
    modified: datetime
    size_bytes: int


@dataclass
class PrunePlan:
    cutoff: datetime
    delete: list[WorkFolder] = field(default_factory=list)
    keep: list[tuple[WorkFolder, str]] = field(default_factory=list)
    stored_ipas: list[tuple[Path, int]] = field(default_factory=list)

    # Total bytes the plan would free, counting a shared stored IPA once.
    @property
    def reclaimable_bytes(self) -> int:
        return sum(folder.size_bytes for folder in self.delete) + sum(size for _, size in self.stored_ipas)


# Reports whether a folder name has the shape of a run id.
def is_run_id(name: str) -> bool:
    return bool(TIMESTAMP_RUN_ID.match(name) or HEX_RUN_ID.match(name))


# Returns the time a run id names, or None for ids that carry no timestamp.
def run_id_time(run_id: str) -> datetime | None:
    if not TIMESTAMP_RUN_ID.match(run_id):
        return None
    return datetime.strptime(run_id[:19], TIMESTAMP_FORMAT).astimezone()


# Lists the per-run folders directly under the platform work folders and under ios/acquired.
def run_folders(work_root: Path) -> list[Path]:
    parents = [work_root / "ios", work_root / "ios" / "acquired", work_root / "android"]
    folders = []
    for parent in parents:
        if not parent.is_dir():
            continue
        for child in sorted(parent.iterdir()):
            if child.is_dir() and not child.is_symlink() and is_run_id(child.name):
                folders.append(child)
    logger.debug("work retention: %d run folder(s) under %s.", len(folders), work_root)
    return folders


# Measures the bytes a folder holds alone, skipping files shared by hard link, and its newest modification time.
def describe_folder(path: Path) -> WorkFolder:
    size = 0
    newest = path.lstat().st_mtime
    for item in path.rglob("*"):
        try:
            stat = item.lstat()
        except OSError:
            continue
        newest = max(newest, stat.st_mtime)
        if item.is_file() and not item.is_symlink() and stat.st_nlink == 1:
            size += stat.st_size
    named = run_id_time(path.name)
    modified = named or datetime.fromtimestamp(newest).astimezone()
    return WorkFolder(path=path, run_id=path.name, modified=modified, size_bytes=size)


# Returns the run ids that are still running according to the API registry file and the run manifests.
def in_progress_run_ids(reports_root: Path) -> set[str]:
    running: set[str] = set()
    registry = reports_root / REGISTRY_NAME
    try:
        records = json.loads(registry.read_text()) if registry.is_file() else {}
    except (OSError, ValueError):
        logger.debug("work retention: run registry %s unreadable.", registry, exc_info=True)
        records = {}
    for record in records.values() if isinstance(records, dict) else []:
        if isinstance(record, dict) and record.get("status") == "running" and record.get("run_timestamp"):
            running.add(str(record["run_timestamp"]))
    for report_dir in _report_dirs(reports_root):
        manifest = read_manifest(report_dir)
        if manifest is not None and manifest.get("status") not in TERMINAL_STATUSES:
            running.add(report_dir.name)
    logger.debug("work retention: in-progress runs %s.", sorted(running))
    return running


# Returns the work-root-relative run folders referenced by reports inside the retention window.
def referenced_folders(reports_root: Path, cutoff: datetime) -> set[str]:
    referenced: set[str] = set()
    for report_dir in _report_dirs(reports_root):
        report_time = run_id_time(report_dir.name) or datetime.fromtimestamp(report_dir.stat().st_mtime).astimezone()
        if report_time < cutoff:
            continue
        for document in report_dir.rglob("*.json"):
            try:
                referenced.update(WORK_REFERENCE.findall(document.read_text(errors="replace")))
            except OSError:
                logger.debug("work retention: could not read %s.", document, exc_info=True)
    logger.debug("work retention: %d run folder(s) referenced by retained reports.", len(referenced))
    return referenced


# Decides which run folders to delete: older than the cutoff, unreferenced by retained reports, and not running.
def plan_prune(work_root: Path, reports_root: Path, older_than: timedelta, now: datetime | None = None) -> PrunePlan:
    cutoff = (now or datetime.now().astimezone()) - older_than
    plan = PrunePlan(cutoff=cutoff)
    running = in_progress_run_ids(reports_root)
    referenced = referenced_folders(reports_root, cutoff)
    for path in run_folders(work_root):
        folder = describe_folder(path)
        if folder.run_id in running:
            plan.keep.append((folder, "run in progress"))
        elif folder.modified >= cutoff:
            plan.keep.append((folder, "newer than the cutoff"))
        elif path.relative_to(work_root).as_posix() in referenced:
            plan.keep.append((folder, "referenced by a retained report"))
        else:
            plan.delete.append(folder)
    plan.stored_ipas = orphaned_stored_ipas(work_root, plan.delete, now or datetime.now().astimezone())
    logger.debug(
        "work retention: delete %d folder(s) and %d stored IPA(s), keep %d, cutoff %s.",
        len(plan.delete), len(plan.stored_ipas), len(plan.keep), cutoff,
    )
    return plan


# Returns the stored IPAs that no run folder will link to once the given folders are deleted.
def orphaned_stored_ipas(work_root: Path, deleting: list[WorkFolder], now: datetime) -> list[tuple[Path, int]]:
    store = work_root / "ios" / "acquired" / STORE_DIR_NAME
    if not store.is_dir():
        return []
    removed_links: Counter[tuple[int, int]] = Counter()
    for folder in deleting:
        for item in folder.path.rglob("*"):
            if item.is_file() and not item.is_symlink():
                stat = item.lstat()
                if stat.st_nlink > 1:
                    removed_links[(stat.st_dev, stat.st_ino)] += 1
    orphans = []
    for entry in sorted(store.glob("*.ipa")):
        stat = entry.lstat()
        if now.timestamp() - stat.st_ctime < STORE_GRACE.total_seconds():
            continue
        if stat.st_nlink - removed_links[(stat.st_dev, stat.st_ino)] <= 1:
            orphans.append((entry, stat.st_size))
    return orphans


# Deletes the planned folders, refusing any path that does not resolve inside the work root.
def apply_prune(plan: PrunePlan, work_root: Path) -> list[Path]:
    root = work_root.resolve()
    # Check every target before deleting any, so one bad entry leaves the whole plan unapplied.
    for path in [*(folder.path for folder in plan.delete), *(entry for entry, _ in plan.stored_ipas)]:
        if path.is_symlink() or root not in path.resolve().parents:
            raise ValueError(f"refusing to delete {path}: not inside {root}")
    deleted = []
    for folder in plan.delete:
        target = folder.path.resolve()
        shutil.rmtree(target)
        deleted.append(target)
        logger.info("work retention: deleted %s (%d bytes).", target, folder.size_bytes)
    # Stored IPAs go last: they were planned on the assumption that their run folders are already gone.
    for entry, size in plan.stored_ipas:
        entry.unlink(missing_ok=True)
        deleted.append(entry)
        logger.info("work retention: deleted stored IPA %s (%d bytes).", entry.name, size)
    return deleted


# Lists the report run folders directly under the reports root.
def _report_dirs(reports_root: Path) -> list[Path]:
    if not reports_root.is_dir():
        return []
    return [child for child in sorted(reports_root.iterdir()) if child.is_dir() and not child.name.startswith(".")]
