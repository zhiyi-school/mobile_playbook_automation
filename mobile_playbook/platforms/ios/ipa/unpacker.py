from __future__ import annotations

import os
import shutil
import stat
import zipfile
from pathlib import Path, PurePosixPath


IGNORED_NAMES = {"__MACOSX", ".DS_Store"}


def _entry_mode(info: zipfile.ZipInfo) -> int:
    return (info.external_attr >> 16) & 0xFFFF


def _symlink_stays_within(dest_dir: Path, link_path: Path, link_target: str) -> bool:
    if os.path.isabs(link_target):
        return False
    resolved = os.path.normpath(link_path.parent / link_target)
    base = str(dest_dir.resolve())
    return resolved == base or resolved.startswith(base + os.sep)


def is_safe_member_name(member_name: str) -> bool:
    pure = PurePosixPath(member_name)
    return bool(pure.parts) and not pure.is_absolute() and ".." not in pure.parts


def _safe_target(root: Path, member_name: str) -> Path:
    pure = PurePosixPath(member_name)
    if not is_safe_member_name(member_name):
        raise ValueError(f"Unsafe zip entry path: {member_name}")
    target = (root / Path(*pure.parts)).resolve()
    root_resolved = root.resolve()
    if target != root_resolved and root_resolved not in target.parents:
        raise ValueError(f"Zip entry escapes extraction directory: {member_name}")
    return target


def safe_extract_zip(zip_path: Path, dest_dir: Path) -> None:
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            parts = PurePosixPath(info.filename).parts
            if not parts or parts[0] in IGNORED_NAMES or parts[-1] in IGNORED_NAMES:
                continue
            target = _safe_target(dest_dir, info.filename)
            mode = _entry_mode(info)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if stat.S_ISLNK(mode):
                link_target = zf.read(info).decode()
                if not _symlink_stays_within(dest_dir, target, link_target):
                    raise ValueError(f"Unsafe symlink target: {info.filename} -> {link_target}")
                if target.is_symlink() or target.exists():
                    target.unlink()
                os.symlink(link_target, target)
                continue
            with zf.open(info) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            if mode:
                os.chmod(target, stat.S_IMODE(mode))


def locate_payload_app(extract_dir: Path) -> Path:
    payload = Path(extract_dir) / "Payload"
    apps = sorted(p for p in payload.glob("*.app") if p.is_dir())
    if len(apps) != 1:
        raise ValueError(f"Expected exactly one Payload/*.app directory, found {len(apps)}")
    return apps[0]


def unpack_ipa(ipa_path: Path, dest_dir: Path) -> Path:
    safe_extract_zip(ipa_path, dest_dir)
    return locate_payload_app(dest_dir)
