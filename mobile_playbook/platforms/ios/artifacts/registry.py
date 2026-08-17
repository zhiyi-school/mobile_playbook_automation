from __future__ import annotations

from mobile_playbook.platforms.ios.artifacts.ci_artifact import CiArtifactProvider
from mobile_playbook.platforms.ios.artifacts.installed_app_reference import InstalledAppReferenceProvider
from mobile_playbook.platforms.ios.artifacts.local_ipa import LocalIpaProvider
from mobile_playbook.platforms.ios.artifacts.vendor_ipa import VendorIpaProvider
from mobile_playbook.platforms.ios.artifacts.xcode_archive_export import XcodeArchiveExportProvider

PROVIDERS = {
    "local_ipa": LocalIpaProvider,
    "ci_artifact": CiArtifactProvider,
    "vendor_ipa": VendorIpaProvider,
    "xcode_archive_export": XcodeArchiveExportProvider,
    "installed_app_reference": InstalledAppReferenceProvider,
}


def known_sources() -> set[str]:
    return set(PROVIDERS)


def get_provider(source: str):
    provider_class = PROVIDERS.get(source)
    if provider_class is None:
        return None
    return provider_class()
