"""
Artifact provider for IPAs exported from an Xcode archive.
"""

from __future__ import annotations

from mobile_playbook.platforms.ios.artifacts.local_ipa import LocalIpaProvider


class XcodeArchiveExportProvider(LocalIpaProvider):
    source = "xcode_archive_export"
