"""
Parses and validates the app and risk selections passed to a scan.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


# Split a comma-separated option into a set of trimmed values, or None when unset.
def selected_csv(value: str | None) -> set[str] | None:
    return {item.strip() for item in value.split(",") if item.strip()} if value else None


# Split a comma-separated app option into normalized selectors, or None when unset.
def selected_app_csv(value: str | None) -> set[str] | None:
    return {_normalize_selector(item) for item in value.split(",") if item.strip()} if value else None


# Report whether an app's id, name, package or bundle id matches a selector; None selects all.
def app_matches_selector(app: Any, selected_apps: set[str] | None) -> bool:
    if selected_apps is None:
        return True
    app_id = _normalize_selector(getattr(app, "id", ""))
    app_name = _normalize_selector(getattr(app, "name", ""))
    package_name = _normalize_selector(getattr(app, "package_name", ""))
    bundle_id = _normalize_selector(getattr(app, "bundle_id", ""))
    matched = app_id in selected_apps or app_name in selected_apps or package_name in selected_apps or bundle_id in selected_apps
    logger.debug("selection: app %s matched=%s (normalized id=%s name=%s package=%s bundle=%s, selected=%s)", getattr(app, "id", app), matched, app_id, app_name, package_name, bundle_id, selected_apps)
    return matched


# Raise ValueError when an app selection matches none of the configured apps.
def validate_app_selection(apps: list[Any], selected_apps: set[str] | None) -> None:
    if selected_apps is None:
        logger.debug("selection: no --apps filter; all %s apps selected", len(apps))
        return
    if any(app_matches_selector(app, selected_apps) for app in apps):
        logger.debug("selection: --apps %s matched at least one of %s apps", selected_apps, len(apps))
        return
    requested = ", ".join(sorted(selected_apps))
    available = ", ".join(str(getattr(app, "id", "")) for app in apps)
    logger.debug("selection: --apps %s matched none; available=%s", requested, available)
    raise ValueError(f"No apps matched --apps {requested}. Available app IDs: {available}")


# Raise ValueError when a risk selection names ids that are not known.
def validate_risk_selection(known_risk_ids: set[str], selected_risks: set[str] | None) -> None:
    if selected_risks is None:
        logger.debug("selection: no risk filter; %s known risks", len(known_risk_ids))
        return
    unknown = selected_risks - known_risk_ids
    if unknown:
        requested = ", ".join(sorted(unknown))
        available = ", ".join(sorted(known_risk_ids))
        logger.debug("selection: unknown risk ids %s; available=%s", requested, available)
        raise ValueError(f"Unknown risk ID(s): {requested}. Available risk IDs: {available}")
    logger.debug("selection: risk filter %s is valid", sorted(selected_risks))


# Lowercase a selector and drop every non-alphanumeric character.
def _normalize_selector(value: str) -> str:
    return "".join(ch.lower() for ch in value if ch.isalnum())
