from __future__ import annotations

import importlib
import inspect
import pkgutil
import warnings
from typing import Sequence


def discover_plugins(package_name: str, package_path: Sequence[str], base_class: type, id_attr: str) -> dict[str, type]:
    """Build an {id: class} registry by scanning a package's modules on disk.

    Each module in `package_path` is imported independently, inside its own
    try/except. A module that no longer exists simply isn't returned by
    `pkgutil.iter_modules` — there's nothing to catch, it's just absent from
    discovery. A module that exists but fails to import (a broken internal
    dependency, for example) is skipped with a warning rather than aborting
    discovery of the rest of the package.

    A class is registered only when it subclasses `base_class` (but is not
    `base_class` itself), is actually defined in the module being scanned
    (not merely imported into that module's namespace, which would otherwise
    register the same class repeatedly while scanning sibling modules that
    import it too), and has its own truthy `id_attr` — this is what excludes
    shared/abstract intermediate base classes that don't set it, with no
    filename-based exclusion list required.
    """
    registry: dict[str, type] = {}
    for module_info in pkgutil.iter_modules(package_path):
        if module_info.ispkg:
            continue
        full_name = f"{package_name}.{module_info.name}"
        try:
            module = importlib.import_module(full_name)
        except Exception as exc:
            warnings.warn(f"discover_plugins: skipping {full_name}, failed to import ({exc})", RuntimeWarning, stacklevel=2)
            continue
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if obj is base_class or not issubclass(obj, base_class):
                continue
            if obj.__module__ != full_name:
                continue
            plugin_id = getattr(obj, id_attr, "")
            if not plugin_id:
                continue
            registry.setdefault(plugin_id, obj)
    return registry
