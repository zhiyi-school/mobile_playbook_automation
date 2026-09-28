"""
Discovers plugin classes by scanning a package's modules.
"""

from __future__ import annotations

import importlib
import inspect
import logging
import pkgutil
import warnings
from typing import Sequence

logger = logging.getLogger(__name__)


# Map each subclass defined in the package's modules to its id attribute, skipping modules that fail to import.
def discover_plugins(package_name: str, package_path: Sequence[str], base_class: type, id_attr: str) -> dict[str, type]:
    registry: dict[str, type] = {}
    logger.debug("discovery: scanning %s (%s) for %s subclasses by %s", package_name, package_path, base_class.__name__, id_attr)
    for module_info in pkgutil.iter_modules(package_path):
        if module_info.ispkg:
            logger.debug("discovery: skipping subpackage %s.%s", package_name, module_info.name)
            continue
        full_name = f"{package_name}.{module_info.name}"
        try:
            module = importlib.import_module(full_name)
        except Exception as exc:
            logger.debug("discovery: import of %s failed: %s", full_name, exc, exc_info=True)
            warnings.warn(f"discover_plugins: skipping {full_name}, failed to import ({exc})", RuntimeWarning, stacklevel=2)
            continue
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if obj is base_class or not issubclass(obj, base_class):
                continue
            # Imported classes would otherwise be registered once per importing sibling module.
            if obj.__module__ != full_name:
                continue
            plugin_id = getattr(obj, id_attr, "")
            if not plugin_id:
                logger.debug("discovery: %s.%s has no %s; not registered", full_name, obj.__name__, id_attr)
                continue
            if plugin_id in registry:
                logger.debug("discovery: %s already registered by %s; ignoring %s.%s", plugin_id, registry[plugin_id].__name__, full_name, obj.__name__)
            else:
                logger.debug("discovery: registered %s -> %s.%s", plugin_id, full_name, obj.__name__)
            registry.setdefault(plugin_id, obj)
    logger.debug("discovery: %s registered %s plugins: %s", package_name, len(registry), list(registry))
    return registry
