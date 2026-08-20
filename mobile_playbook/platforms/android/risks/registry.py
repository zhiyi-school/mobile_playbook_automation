from __future__ import annotations

import os

from mobile_playbook.core.discovery import discover_plugins
from mobile_playbook.platforms.android.risks.base import AndroidRisk

_PACKAGE_NAME = __name__.rsplit(".", 1)[0]
_PACKAGE_PATH = [os.path.dirname(__file__)]

_cache: dict[str, type[AndroidRisk]] | None = None


def _registry() -> dict[str, type[AndroidRisk]]:
    global _cache
    if _cache is None:
        _cache = discover_plugins(_PACKAGE_NAME, _PACKAGE_PATH, AndroidRisk, "risk_id")
    return _cache


def get_risk(risk_id: str) -> AndroidRisk | None:
    risk_type = _registry().get(risk_id)
    return risk_type() if risk_type else None


def known_risks() -> set[str]:
    return set(_registry())


def list_risks() -> list[dict]:
    risks = []
    for risk_id, risk_type in sorted(_registry().items()):
        risk = risk_type()
        risks.append(
            {
                "risk_id": risk_id,
                "name": risk.name,
                "description": risk.description,
                "goal": risk.goal,
                "is_blocking": risk.is_blocking,
                "mitre_attack_mobile_technique_id": risk.mitre_attack_mobile_technique_id,
                "requires_device": risk.requires_device,
                "requires": list(risk.requires),
            }
        )
    return risks
