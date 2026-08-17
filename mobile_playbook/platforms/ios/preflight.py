from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class IosPreflightResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def check_ios_preflight(config) -> IosPreflightResult:
    errors: list[str] = []
    if not getattr(config.device, "udid", ""):
        errors.append("device.udid is required")
    if not getattr(config.device, "team_id", ""):
        errors.append("device.team_id is required")
    if not getattr(config.device, "appium_server_url", ""):
        errors.append("device.appium_server_url is required")
    return IosPreflightResult(ok=not errors, errors=errors)
