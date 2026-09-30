from __future__ import annotations

import shutil

import pytest
import yaml

from mobile_playbook.api import config_editing
from mobile_playbook.api.config_editing import shared
from mobile_playbook.common import storage_paths
from mobile_playbook.platforms.android.config import load_config as load_android_config
from mobile_playbook.platforms.android.risks import list_risks as list_android_risks
from mobile_playbook.platforms.ios.config import load_config as load_ios_config
from mobile_playbook.platforms.ios.risks import list_risks as list_ios_risks
from mobile_playbook.playbook import catalogue, source

EXAMPLES_ROOT = storage_paths.REPOSITORY_ROOT / "configs"
LOADERS = {"ios": load_ios_config, "android": load_android_config}
LIST_RISKS = {"ios": list_ios_risks, "android": list_android_risks}
PLATFORMS = sorted(LOADERS)


@pytest.fixture
def copied_examples(tmp_path):
    # The conftest fixture already points CONFIG_ROOT here, so the editor reads these copies.
    root = tmp_path / "configs"
    for example in EXAMPLES_ROOT.rglob("*.example.yaml"):
        target = root / example.relative_to(EXAMPLES_ROOT).with_name(example.name.replace(".example.yaml", ".yaml"))
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(example, target)
    return root


def _read_config_files():
    for mapping in (
        shared.ENTRY_FILES,
        shared.APPS_FILES,
        shared.FEATURES_FILES,
        shared.RISK_FILES,
        shared.TEMPLATE_FILES,
        catalogue.CONTROL_OVERRIDE_FILES,
        source.RISK_CONFIG_FILES,
    ):
        yield from mapping.values()
    for settings in shared.RISK_SETTINGS.values():
        yield from (path for _, path in settings.values())


def test_every_config_file_the_backend_reads_has_a_tracked_example():
    missing = sorted(
        str(path)
        for path in set(_read_config_files())
        if not (EXAMPLES_ROOT / path).with_name(path.name.replace(".yaml", ".example.yaml")).is_file()
    )

    assert missing == []


@pytest.mark.parametrize("platform", PLATFORMS)
def test_the_entry_example_includes_every_file_the_editor_writes(platform):
    entry = yaml.safe_load((EXAMPLES_ROOT / f"{platform}.example.yaml").read_text())
    included = set()
    for value in entry["include"].values():
        included.update(value if isinstance(value, list) else [value])
    expected = {str(path) for _, path in shared.RISK_SETTINGS[platform].values()}
    expected |= {str(shared.APPS_FILES[platform])}
    if platform in shared.TEMPLATE_FILES:
        expected.add(str(shared.TEMPLATE_FILES[platform]))

    assert expected - included == set()


@pytest.mark.parametrize("platform", PLATFORMS)
def test_the_copied_examples_load_as_a_complete_config(copied_examples, platform):
    config = LOADERS[platform](copied_examples / f"{platform}.yaml", dry_run=True)

    assert config.apps


@pytest.mark.parametrize("platform", PLATFORMS)
def test_the_config_editor_validates_the_copied_examples_cleanly(copied_examples, platform):
    config, errors = shared.load_with_errors(platform)

    assert config is not None
    assert errors == []
    assert config_editing.list_features(platform)


@pytest.mark.parametrize("platform", PLATFORMS)
def test_the_risk_and_feature_examples_cover_every_registered_risk(platform):
    risks = yaml.safe_load((EXAMPLES_ROOT / f"split/{platform}/risks.example.yaml").read_text())
    features = yaml.safe_load((EXAMPLES_ROOT / f"split/{platform}/features.example.yaml").read_text())
    registered = LIST_RISKS[platform]()

    assert {risk["risk_id"] for risk in registered} <= set(risks)
    assert {risk["feature_id"] for risk in registered} <= set(features)
