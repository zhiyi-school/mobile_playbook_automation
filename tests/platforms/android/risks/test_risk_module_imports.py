from __future__ import annotations

import ast
from pathlib import Path

import mobile_playbook.platforms.android.risks as risks_package

RISKS_DIR = Path(risks_package.__file__).parent
RISK_MODULE_PREFIX = "mobile_playbook.platforms.android.risks.feature_"


def imported_modules(path: Path) -> set[str]:
    modules = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
    return modules


def test_risk_modules_do_not_import_each_other():
    offenders = {
        path.name: sorted(module for module in imported_modules(path) if module.startswith(RISK_MODULE_PREFIX))
        for path in sorted(RISKS_DIR.glob("feature_*.py"))
    }

    assert {name: modules for name, modules in offenders.items() if modules} == {}
