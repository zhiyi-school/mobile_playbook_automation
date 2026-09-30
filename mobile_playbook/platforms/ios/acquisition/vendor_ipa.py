"""
Artifact provider for IPAs supplied by the app vendor.
"""

from __future__ import annotations

from mobile_playbook.platforms.ios.acquisition.local_ipa import LocalIpaProvider


class VendorIpaProvider(LocalIpaProvider):
    source = "vendor_ipa"
