"""
Registry of Android risk checks discovered from this package.
"""

from __future__ import annotations

import logging
import os

from mobile_playbook.common.plugin_discovery import discover_plugins
from mobile_playbook.platforms.android.risks.base import AndroidRisk

_PACKAGE_NAME = __name__.rsplit(".", 1)[0]
_PACKAGE_PATH = [os.path.dirname(__file__)]

_cache: dict[str, type[AndroidRisk]] | None = None
logger = logging.getLogger(__name__)


# Return the cached risk registry, discovering risk classes on first use.
def _registry() -> dict[str, type[AndroidRisk]]:
    global _cache
    if _cache is None:
        _cache = discover_plugins(_PACKAGE_NAME, _PACKAGE_PATH, AndroidRisk, "risk_id")
        logger.debug("android risks: registry built with %s risks: %s", len(_cache), sorted(_cache))
    return _cache


# Return a new instance of the risk with this id, or None when unknown.
def get_risk(risk_id: str) -> AndroidRisk | None:
    risk_type = _registry().get(risk_id)
    logger.debug("android risks: lookup %s -> %s", risk_id, risk_type.__name__ if risk_type else None)
    return risk_type() if risk_type else None


# Return the ids of every discovered Android risk.
def known_risks() -> set[str]:
    return set(_registry())


# Describe every discovered risk, sorted by id, as plain dicts.
def list_risks() -> list[dict]:
    risks = []
    for risk_id, risk_type in sorted(_registry().items()):
        risk = risk_type()
        risks.append(
            {
                "risk_id": risk_id,
                "feature_id": risk.feature_id,
                "name": risk.name,
                "description": risk.description,
                "is_blocking": risk.is_blocking,
                "tactic": risk.tactic,
                "requires_device": risk.requires_device,
                "requires": list(risk.requires),
                "automation_available": getattr(risk, "automation_available", True),
            }
        )
    return risks
