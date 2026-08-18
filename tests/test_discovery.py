from __future__ import annotations

import importlib
import warnings

import pytest

from mobile_playbook.core.discovery import discover_plugins


def test_discover_plugins_finds_valid_classes_and_skips_broken_or_abstract_ones(tmp_path, monkeypatch):
    pkg_dir = tmp_path / "fake_plugins"
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text("")
    (pkg_dir / "_base.py").write_text("class Plugin:\n    plugin_id: str = ''\n")
    (pkg_dir / "good_one.py").write_text(
        "from fake_plugins._base import Plugin\n\n"
        "class GoodOne(Plugin):\n"
        "    plugin_id = 'good-one'\n"
    )
    # this module fails to import entirely (bad dependency) — it must be
    # skipped, not allowed to abort discovery of the other, valid modules.
    (pkg_dir / "broken_one.py").write_text(
        "from fake_plugins._base import Plugin\n"
        "import this_module_does_not_exist_at_all\n\n"
        "class BrokenOne(Plugin):\n"
        "    plugin_id = 'broken-one'\n"
    )
    # a shared/abstract base that never sets plugin_id must not be registered.
    (pkg_dir / "no_id.py").write_text(
        "from fake_plugins._base import Plugin\n\n"
        "class AbstractHelper(Plugin):\n"
        "    pass\n"
    )
    # imports GoodOne into its own namespace too — must not cause GoodOne to
    # be registered twice, or under the wrong module.
    (pkg_dir / "reexports.py").write_text(
        "from fake_plugins._base import Plugin\n"
        "from fake_plugins.good_one import GoodOne\n\n"
        "class AnotherOne(Plugin):\n"
        "    plugin_id = 'another-one'\n"
    )

    monkeypatch.syspath_prepend(str(tmp_path))
    plugin_base = importlib.import_module("fake_plugins._base").Plugin

    with pytest.warns(RuntimeWarning, match="broken_one"):
        registry = discover_plugins("fake_plugins", [str(pkg_dir)], plugin_base, "plugin_id")

    assert set(registry) == {"good-one", "another-one"}
    assert registry["good-one"].__name__ == "GoodOne"
    assert registry["good-one"].__module__ == "fake_plugins.good_one"
    assert registry["another-one"].__name__ == "AnotherOne"


def test_discover_plugins_treats_a_deleted_module_as_simply_absent(tmp_path, monkeypatch):
    pkg_dir = tmp_path / "fake_plugins2"
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text("")
    (pkg_dir / "_base.py").write_text("class Plugin:\n    plugin_id: str = ''\n")
    module_path = pkg_dir / "only_one.py"
    module_path.write_text(
        "from fake_plugins2._base import Plugin\n\n"
        "class OnlyOne(Plugin):\n"
        "    plugin_id = 'only-one'\n"
    )

    monkeypatch.syspath_prepend(str(tmp_path))
    plugin_base = importlib.import_module("fake_plugins2._base").Plugin

    registry_before = discover_plugins("fake_plugins2", [str(pkg_dir)], plugin_base, "plugin_id")
    assert set(registry_before) == {"only-one"}

    module_path.unlink()
    importlib.invalidate_caches()

    with warnings.catch_warnings():
        warnings.simplefilter("error")  # any warning here means it wasn't "simply absent"
        registry_after = discover_plugins("fake_plugins2", [str(pkg_dir)], plugin_base, "plugin_id")

    assert set(registry_after) == set()
