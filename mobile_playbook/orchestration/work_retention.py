"""
Plans and applies the pruning of old per-run work folders. See docs/operations.md#pruning-old-run-work-folders.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

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

    # Total bytes the plan would free.
    @property
    def reclaimable_bytes(self) -> int:
        return sum(folder.size_bytes for folder in self.delete)


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


# Measures a folder's size and newest modification time without following symlinks.
def describe_folder(path: Path) -> WorkFolder:
    size = 0
    newest = path.lstat().st_mtime
    for item in path.rglob("*"):
        try:
            stat = item.lstat()
        except OSError:
            continue
        newest = max(newest, stat.st_mtime)
        if item.is_file() and not item.is_symlink():
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
    logger.debug("work retention: delete %d, keep %d, cutoff %s.", len(plan.delete), len(plan.keep), cutoff)
    return plan


# Deletes the planned folders, refusing any path that does not resolve inside the work root.
def apply_prune(plan: PrunePlan, work_root: Path) -> list[Path]:
    root = work_root.resolve()
    # Check every target before deleting any, so one bad entry leaves the whole plan unapplied.
    for folder in plan.delete:
        if folder.path.is_symlink() or root not in folder.path.resolve().parents:
            raise ValueError(f"refusing to delete {folder.path}: not a folder inside {root}")
    deleted = []
    for folder in plan.delete:
        target = folder.path.resolve()
        shutil.rmtree(target)
        deleted.append(target)
        logger.info("work retention: deleted %s (%d bytes).", target, folder.size_bytes)
    return deleted


# Lists the report run folders directly under the reports root.
def _report_dirs(reports_root: Path) -> list[Path]:
    if not reports_root.is_dir():
        return []
    return [child for child in sorted(reports_root.iterdir()) if child.is_dir() and not child.name.startswith(".")]
