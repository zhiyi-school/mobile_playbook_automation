"""
SHA-256 hashing of IPA and binary files.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


# Return the hex SHA-256 digest of a file, reading it in 1 MiB chunks.
def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    logger.debug("ios hashing: sha256 %s = %s", path, digest.hexdigest())
    return digest.hexdigest()
