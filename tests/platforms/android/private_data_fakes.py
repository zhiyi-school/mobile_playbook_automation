"""
Synthetic private data for tests: a package, its data directory, a secret, plain and encrypted shared preferences, random
ciphertext and a tar archive builder.
"""

from __future__ import annotations

import io
import random
import tarfile
from pathlib import Path

PACKAGE = "com.example.app"
DATA = f"data/data/{PACKAGE}"
TOKEN = "s3cr3t-session-token-value-0123456789"
PLAIN_PREFS = b'<?xml version="1.0" encoding="utf-8"?>\n<map>\n    <boolean name="onboarded" value="true" />\n    <string name="theme">dark</string>\n</map>\n'
CIPHERTEXT = random.Random(7).randbytes(1024)
ENCRYPTED_PREFS = (
    b"<?xml version='1.0' encoding='utf-8'?>\n<map>\n"
    b'    <string name="__androidx_security_crypto_encrypted_prefs_key_keyset__">12a901c2b3</string>\n'
    b'    <string name="ATn8d2C1kZ9tO0vF">AWxR0pL3cQ9e</string>\n</map>\n'
)


def make_archive(path: Path, files: dict[str, bytes], symlinks: dict[str, str] | None = None) -> Path:
    with tarfile.open(path, "w") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        for name, target in (symlinks or {}).items():
            info = tarfile.TarInfo(name)
            info.type = tarfile.SYMTYPE
            info.linkname = target
            tar.addfile(info)
    return path
