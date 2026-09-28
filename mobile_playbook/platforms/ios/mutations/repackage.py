from __future__ import annotations

import logging
import os
import plistlib
import shutil
import stat
import subprocess
import time
import zipfile
from pathlib import Path

from mobile_playbook.platforms.ios.ipa.plist_utils import get_bundle_executable
from mobile_playbook.storage import resolve_under_repository

FRIDA_DYLIB = "tools/Frida/FridaGadget.dylib"
FRIDA_CONFIG = "tools/Frida/FridaGadget.config"
INSERT_DYLIB_DIR = "tools/insert_dylib"

logger = logging.getLogger(__name__)


def frida_dylib_path(override: str | None = None) -> Path:
    return resolve_under_repository(override or FRIDA_DYLIB)


def frida_config_path(override: str | None = None) -> Path:
    return resolve_under_repository(override or FRIDA_CONFIG)


def add_frida_gadget(app_dir: Path, dylib_src: Path, config_src: Path | None) -> Path:
    """Copy the gadget into the bundle's Frameworks/ and return the main executable to patch."""
    app_dir = Path(app_dir)
    executable = get_bundle_executable(app_dir)
    logger.debug("ios repackage: adding gadget %s (config %s) to %s; executable %s", dylib_src, config_src, app_dir, executable)
    if not executable:
        raise ValueError("CFBundleExecutable is missing; cannot inject a load command")
    frameworks = app_dir / "Frameworks"
    frameworks.mkdir(parents=True, exist_ok=True)
    dylib_dest = frameworks / Path(dylib_src).name
    shutil.copy2(dylib_src, dylib_dest)
    logger.debug("ios repackage: copied gadget to %s", dylib_dest)
    if config_src is not None and Path(config_src).exists():
        shutil.copy2(config_src, frameworks / f"{Path(dylib_src).stem}.config")
        logger.debug("ios repackage: copied gadget config to %s", frameworks / f"{Path(dylib_src).stem}.config")
    else:
        logger.debug("ios repackage: no gadget config copied (config_src=%s)", config_src)
    return app_dir / executable


def resolve_insert_dylib(configured: str | None = None) -> str:
    """Return an insert_dylib path without relying on the process PATH.

    An explicit config value other than the bare default wins (an absolute path,
    or a name to resolve on PATH). Otherwise the repository-vendored binary under
    tools/insert_dylib/ is used, compiled from vendored source on first use when
    only the source is present.
    """
    if configured and configured != "insert_dylib":
        logger.debug("ios repackage: using configured insert_dylib %s", configured)
        return configured
    tool_dir = resolve_under_repository(INSERT_DYLIB_DIR)
    binary = tool_dir / "insert_dylib"
    if binary.exists():
        logger.debug("ios repackage: using vendored insert_dylib %s", binary)
        return str(binary)
    sources = sorted(tool_dir.glob("*.c"))
    logger.debug("ios repackage: vendored insert_dylib missing at %s; %s source files found", binary, len(sources))
    if sources:
        _build_insert_dylib(sources, binary)
    if binary.exists():
        return str(binary)
    logger.debug("ios repackage: falling back to insert_dylib %s", configured or "insert_dylib")
    return configured or "insert_dylib"


def _build_insert_dylib(sources: list[Path], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    command = ["clang", "-O2", "-o", str(out), *[str(s) for s in sources]]
    logger.debug("ios repackage: building insert_dylib: %s", command)
    started = time.monotonic()
    subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
    )
    logger.debug("ios repackage: built %s in %.2fs", out, time.monotonic() - started)
    out.chmod(0o755)


def inject_load_command(exe: Path, load_path: str, insert_dylib_path: str = "insert_dylib") -> None:
    tool = resolve_insert_dylib(insert_dylib_path)
    command = [tool, "--strip-codesig", "--inplace", load_path, str(exe)]
    logger.debug("ios repackage: running %s", command)
    started = time.monotonic()
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
    )
    logger.debug("ios repackage: insert_dylib exited %s in %.2fs", completed.returncode, time.monotonic() - started)
    if completed.returncode != 0:
        logger.debug("ios repackage: insert_dylib stderr head: %s", (completed.stderr or completed.stdout or "")[:200])
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "insert_dylib failed")


def set_bundle_identifier(app_dir: Path, new_bundle_id: str) -> str:
    """Rewrite CFBundleIdentifier in the app's Info.plist; return the previous id.

    Device Info.plist files are usually binary plists, so it is rewritten in binary
    form to stay valid for codesign and installd.
    """
    info = Path(app_dir) / "Info.plist"
    with info.open("rb") as handle:
        plist = plistlib.load(handle)
    previous = plist.get("CFBundleIdentifier", "")
    plist["CFBundleIdentifier"] = new_bundle_id
    logger.debug("ios repackage: rewriting CFBundleIdentifier %s -> %s in %s", previous, new_bundle_id, info)
    with info.open("wb") as handle:
        plistlib.dump(plist, handle, fmt=plistlib.FMT_BINARY)
    return previous


def repack_ipa(unpacked_root: Path, out_path: Path) -> Path:
    unpacked_root = Path(unpacked_root)
    payload = unpacked_root / "Payload"
    if not payload.is_dir():
        logger.debug("ios repackage: no Payload/ under %s", unpacked_root)
        raise ValueError(f"No Payload/ directory under {unpacked_root}")
    out_path = Path(out_path)
    logger.debug("ios repackage: repacking %s -> %s", payload, out_path)
    files = links = 0
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.unlink(missing_ok=True)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(payload.rglob("*")):
            arcname = path.relative_to(unpacked_root).as_posix()
            if path.is_symlink():
                info = zipfile.ZipInfo(arcname)
                info.create_system = 3
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                zf.writestr(info, os.readlink(path))
                links += 1
            elif path.is_file():
                st = path.stat()
                info = zipfile.ZipInfo(arcname)
                info.create_system = 3
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = stat.S_IMODE(st.st_mode) << 16
                zf.writestr(info, path.read_bytes())
                files += 1
    logger.debug("ios repackage: wrote %s with %s files and %s symlinks", out_path, files, links)
    return out_path
