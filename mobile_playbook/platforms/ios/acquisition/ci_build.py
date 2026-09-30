"""
Artifact provider for IPAs produced by a CI pipeline.
"""

from __future__ import annotations

from mobile_playbook.platforms.ios.acquisition.local_ipa import LocalIpaProvider


class CiArtifactProvider(LocalIpaProvider):
    source = "ci_artifact"
