from __future__ import annotations

import plistlib
import zipfile

import pytest

from mobile_playbook.platforms.ios.ipa.plist_utils import read_info_plist, write_info_plist
from mobile_playbook.platforms.ios.ipa.unpacker import safe_extract_zip, unpack_ipa


def test_safe_zip_extraction_path_traversal_prevention(tmp_path):
    z = tmp_path / "bad.ipa"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("../escape.txt", "x")
    with pytest.raises(ValueError):
        safe_extract_zip(z, tmp_path / "out")


def test_info_plist_reading_and_writing(tmp_path, fake_ipa):
    app_dir = unpack_ipa(fake_ipa, tmp_path / "out")
    info = read_info_plist(app_dir)
    info["CFBundleDisplayName"] = "New Name"
    write_info_plist(app_dir, info)
    assert read_info_plist(app_dir)["CFBundleDisplayName"] == "New Name"
