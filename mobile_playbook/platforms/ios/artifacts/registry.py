from __future__ import annotations

import os

from mobile_playbook.core.discovery import discover_plugins
from mobile_playbook.platforms.ios.artifacts.base import ArtifactProvider

_PACKAGE_NAME = __name__.rsplit(".", 1)[0]
_PACKAGE_PATH = [os.path.dirname(__file__)]

_cache: dict[str, type[ArtifactProvider]] | None = None


def _registry() -> dict[str, type[ArtifactProvider]]:
    global _cache
    if _cache is None:
        _cache = discover_plugins(_PACKAGE_NAME, _PACKAGE_PATH, ArtifactProvider, "source")
    return _cache


def known_sources() -> set[str]:
    return set(_registry())


def get_provider(source: str) -> ArtifactProvider | None:
    provider_class = _registry().get(source)
    if provider_class is None:
        return None
    return provider_class()
