"""
Content-addressed IPA store that lets every run's acquired copy share one file per distinct IPA.
"""

from __future__ import annotations

import logging
import os
import shutil
import stat
import tempfile
from pathlib import Path

from mobile_playbook.platforms.ios.ipa.hashing import sha256_file

logger = logging.getLogger(__name__)

STORE_DIR_NAME = ".by-sha256"
READ_ONLY = stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH


# Places source at destination as a hard link to its stored copy and returns the SHA-256 of the placed file.
def place_ipa(source: Path, destination: Path, store_dir: Path) -> str:
    digest = sha256_file(source)
    stored = store_dir / f"{digest}.ipa"
    try:
        if not stored.is_file():
            _store(source, stored, digest)
        destination.unlink(missing_ok=True)
        os.link(stored, destination)
    except (OSError, ValueError):
        # Linking is only a space optimization; fall back to the plain per-run copy it replaces.
        logger.debug("ios ipa store: could not link %s to %s; copying instead.", destination, stored, exc_info=True)
        destination.unlink(missing_ok=True)
        shutil.copy2(source, destination)
        return sha256_file(destination)
    logger.debug("ios ipa store: %s linked to stored %s.", destination, stored.name)
    return digest


# Copies source into the store under its digest, refusing a copy whose content changed during the copy.
def _store(source: Path, stored: Path, digest: str) -> None:
    stored.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=stored.parent, prefix=".incoming-", suffix=".ipa")
    os.close(handle)
    try:
        shutil.copy2(source, temporary)
        if sha256_file(Path(temporary)) != digest:
            raise ValueError(f"{source} changed while it was being stored")
        # Stored copies are shared evidence across runs, so nothing may modify them in place.
        os.chmod(temporary, READ_ONLY)
        os.replace(temporary, stored)
    finally:
        Path(temporary).unlink(missing_ok=True)
