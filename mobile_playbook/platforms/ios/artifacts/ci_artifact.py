from __future__ import annotations

from mobile_playbook.platforms.ios.artifacts.local_ipa import LocalIpaProvider


class CiArtifactProvider(LocalIpaProvider):
    source = "ci_artifact"
