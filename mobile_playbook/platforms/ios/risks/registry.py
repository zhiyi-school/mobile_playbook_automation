"""
Discovery registry mapping iOS risk IDs to risk classes.
"""

from __future__ import annotations

import logging
import os

from mobile_playbook.core.discovery import discover_plugins
from mobile_playbook.platforms.ios.risks.base import Risk

logger = logging.getLogger(__name__)

_PACKAGE_NAME = __name__.rsplit(".", 1)[0]
_PACKAGE_PATH = [os.path.dirname(__file__)]

_cache: dict[str, type[Risk]] | None = None


# Discover and cache the risk classes in this package, keyed by risk ID.
def _registry() -> dict[str, type[Risk]]:
    global _cache
    if _cache is None:
        logger.debug("ios risk registry: discovering risks in %s", _PACKAGE_NAME)
        _cache = discover_plugins(_PACKAGE_NAME, _PACKAGE_PATH, Risk, "risk_id")
        logger.debug("ios risk registry: discovered %d risk(s): %s", len(_cache), sorted(_cache))
    return _cache


# Return the set of registered risk IDs.
def known_risks() -> set[str]:
    return set(_registry())


# Instantiate the risk for an ID, or return None when it is unknown.
def get_risk(risk_id: str) -> Risk | None:
    risk_type = _registry().get(risk_id)
    logger.debug("ios risk registry: get_risk(%s) -> %s", risk_id, getattr(risk_type, "__name__", risk_type))
    return risk_type() if risk_type else None


# Return catalogue metadata for every registered risk, sorted by risk ID.
def list_risks() -> list[dict[str, object]]:
    risks = [risk_type() for _, risk_type in sorted(_registry().items())]
    return [
        {
            "risk_id": risk.risk_id,
            "feature_id": risk.feature_id,
            "name": risk.name,
            "description": risk.description,
            "is_blocking": risk.is_blocking,
            "tactic": risk.tactic,
            "requires_ipa_artifact": risk.requires_ipa_artifact,
            "automation_available": risk.automation_available,
        }
        for risk in risks
    ]
