"""
Derives the device role each Android risk runs on from its declared requirements and plans the roles a run needs.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from mobile_playbook.platforms.corellium.selection import ROLES

logger = logging.getLogger(__name__)

ROOT_REQUIREMENT = "root"
ROLE_FOR_ROOT = {rooted: role for role, rooted in ROLES.items()}
NON_ROOTED = ROLE_FOR_ROOT[False]
ROOTED = ROLE_FOR_ROOT[True]


class RiskDeclarationError(ValueError):
    """A risk declares its device requirements in a shape the planner cannot route."""


@dataclass(frozen=True)
class PlannedRisk:
    """One planned risk run and the device role it executes on, or None when it uses no device."""

    app: Any
    risk_id: str
    role: str | None


# Return the problems with a risk's `requires` and `requires_device` declarations.
def declaration_errors(risk: Any) -> list[str]:
    requires = getattr(risk, "requires", [])
    requires_device = getattr(risk, "requires_device", True)
    errors = []
    if not isinstance(requires_device, bool):
        errors.append("requires_device must be true or false")
    if not isinstance(requires, (list, tuple)) or any(not isinstance(name, str) or not name.strip() for name in requires):
        errors.append("requires must be a list of requirement names")
    elif len(set(requires)) != len(requires):
        errors.append("requires lists a requirement more than once")
    elif ROOT_REQUIREMENT in requires and requires_device is not True:
        errors.append("a risk that requires root must also require a device")
    return errors


# Return the role a risk runs on: rooted when it requires root, non_rooted otherwise, and None without a device.
def risk_role(risk: Any) -> str | None:
    errors = declaration_errors(risk)
    if errors:
        raise RiskDeclarationError(f"Risk {getattr(risk, 'risk_id', '') or type(risk).__name__}: {'; '.join(errors)}")
    if not getattr(risk, "requires_device", True):
        return None
    return ROLE_FOR_ROOT[ROOT_REQUIREMENT in getattr(risk, "requires", [])]


# Assign each planned (app, risk_id) pair its role, keeping the plan's order; unregistered risks get no role.
def plan_risk_roles(planned: Iterable[tuple[Any, str]], get_risk: Callable[[str], Any]) -> list[PlannedRisk]:
    plan = []
    for app, risk_id in planned:
        risk = get_risk(risk_id)
        plan.append(PlannedRisk(app, risk_id, risk_role(risk) if risk is not None else None))
    logger.debug("android roles: planned %s", [(getattr(item.app, "id", item.app), item.risk_id, item.role) for item in plan])
    return plan


# Return the distinct roles a plan needs, in the shared role order.
def required_roles(plan: Iterable[PlannedRisk]) -> tuple[str, ...]:
    needed = {item.role for item in plan if item.role is not None}
    return tuple(role for role in ROLES if role in needed)
