import ast
from pathlib import Path

import pytest

import mobile_playbook.platforms.corellium as shared

PACKAGE = shared.__name__
FORBIDDEN = ("mobile_playbook.platforms.android", "mobile_playbook.platforms.ios", "appium", "selenium")
DYNAMIC_IMPORTS = {"import_module", "__import__"}


def imported_modules(source: str, package: str) -> list[str]:
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.rsplit(".", node.level - 1)[0] if node.level > 1 else package
                module = f"{base}.{node.module}" if node.module else base
            else:
                module = node.module or ""
            found += [module] + [f"{module}.{alias.name}" for alias in node.names]
        elif isinstance(node, ast.Call):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            if name in DYNAMIC_IMPORTS and node.args and isinstance(node.args[0], ast.Constant):
                found.append(str(node.args[0].value))
    return found


def violations(source: str, package: str = PACKAGE) -> list[str]:
    return [module for module in imported_modules(source, package)
            if any(module == prefix or module.startswith(prefix + ".") for prefix in FORBIDDEN)]


@pytest.mark.parametrize("path", sorted(Path(shared.__file__).parent.glob("*.py")), ids=lambda path: path.name)
def test_shared_corellium_modules_never_import_platform_connection_code(path):
    assert violations(path.read_text()) == []


@pytest.mark.parametrize("source", [
    "def connect():\n    from mobile_playbook.platforms.android.device.adb import AdbClient\n",
    "from ..android.config import AndroidConfig\n",
    "from .. import android\n",
    "import importlib\nimportlib.import_module('appium.webdriver')\n",
    "__import__('selenium')\n",
    "import mobile_playbook.platforms.ios.device\n",
])
def test_the_boundary_check_catches_nested_relative_and_dynamic_imports(source):
    assert violations(source)


def test_the_boundary_check_allows_shared_and_common_imports():
    source = ("from .client import CorelliumClient\nfrom mobile_playbook.common.storage_paths import corellium_state_dir\n"
              "import json\nimport mobile_playbook.platforms.corellium.claims\n")
    assert violations(source) == []
