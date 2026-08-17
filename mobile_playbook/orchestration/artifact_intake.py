from __future__ import annotations

from typing import Any


def selected_csv(value: str | None) -> set[str] | None:
    return {item.strip() for item in value.split(",") if item.strip()} if value else None


def selected_app_csv(value: str | None) -> set[str] | None:
    return {_normalize_selector(item) for item in value.split(",") if item.strip()} if value else None


def app_matches_selector(app: Any, selected_apps: set[str] | None) -> bool:
    if selected_apps is None:
        return True
    app_id = _normalize_selector(getattr(app, "id", ""))
    app_name = _normalize_selector(getattr(app, "name", ""))
    package_name = _normalize_selector(getattr(app, "package_name", ""))
    bundle_id = _normalize_selector(getattr(app, "bundle_id", ""))
    return app_id in selected_apps or app_name in selected_apps or package_name in selected_apps or bundle_id in selected_apps


def validate_app_selection(apps: list[Any], selected_apps: set[str] | None) -> None:
    if selected_apps is None:
        return
    if any(app_matches_selector(app, selected_apps) for app in apps):
        return
    requested = ", ".join(sorted(selected_apps))
    available = ", ".join(str(getattr(app, "id", "")) for app in apps)
    raise ValueError(f"No apps matched --apps {requested}. Available app IDs: {available}")


def _normalize_selector(value: str) -> str:
    return "".join(ch.lower() for ch in value if ch.isalnum())
