from __future__ import annotations

import os

from mobile_playbook.core.discovery import discover_plugins
from mobile_playbook.platforms.ios.risks.base import Risk

_PACKAGE_NAME = __name__.rsplit(".", 1)[0]
_PACKAGE_PATH = [os.path.dirname(__file__)]

_cache: dict[str, type[Risk]] | None = None


def _registry() -> dict[str, type[Risk]]:
    global _cache
    if _cache is None:
        _cache = discover_plugins(_PACKAGE_NAME, _PACKAGE_PATH, Risk, "risk_id")
    return _cache


def known_risks() -> set[str]:
    return set(_registry())


def get_risk(risk_id: str) -> Risk | None:
    risk_type = _registry().get(risk_id)
    return risk_type() if risk_type else None


def list_risks() -> list[dict[str, object]]:
    risks = [risk_type() for _, risk_type in sorted(_registry().items())]
    return [
        {
            "risk_id": risk.risk_id,
            "feature_id": risk.feature_id,
            "name": risk.name,
            "requires_ipa_artifact": risk.requires_ipa_artifact,
        }
        for risk in risks
    ]
