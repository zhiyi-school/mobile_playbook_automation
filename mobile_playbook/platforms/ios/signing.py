from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class IosSigningConfig:
    team_id: str
    signing_id: str = "Apple Development"
    updated_wda_bundle_id: str | None = None
    allow_device_registration: bool = False
