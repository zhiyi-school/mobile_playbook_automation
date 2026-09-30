"""
Discovery registry mapping artifact source names to provider classes.
"""

from __future__ import annotations

import logging
import os

from mobile_playbook.common.plugin_discovery import discover_plugins
from mobile_playbook.platforms.ios.acquisition.base import ArtifactProvider

_PACKAGE_NAME = __name__.rsplit(".", 1)[0]
_PACKAGE_PATH = [os.path.dirname(__file__)]

logger = logging.getLogger(__name__)

_cache: dict[str, type[ArtifactProvider]] | None = None


# Discover and cache the provider classes in this package, keyed by source.
def _registry() -> dict[str, type[ArtifactProvider]]:
    global _cache
    if _cache is None:
        _cache = discover_plugins(_PACKAGE_NAME, _PACKAGE_PATH, ArtifactProvider, "source")
        logger.debug("ios artifacts: discovered providers %s", sorted(_cache))
    return _cache


# Return the set of registered artifact source names.
def known_sources() -> set[str]:
    return set(_registry())


# Instantiate the provider for a source, or return None when none is registered.
def get_provider(source: str) -> ArtifactProvider | None:
    provider_class = _registry().get(source)
    if provider_class is None:
        logger.debug("ios artifacts: no provider for source %s", source)
        return None
    logger.debug("ios artifacts: source %s -> %s", source, provider_class.__name__)
    return provider_class()
