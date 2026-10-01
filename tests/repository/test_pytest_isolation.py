from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from mobile_playbook.api import settings
from mobile_playbook.common import storage_paths


# Run the real pytest plugin in a fresh process so collection and skip policies are exercised.
def _run_child_pytest(tmp_path: Path, source: str, *arguments: str):
    test_file = tmp_path / "test_child.py"
    test_file.write_text(source)
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(storage_paths.REPOSITORY_ROOT)
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    environment.pop("PYTEST_ADDOPTS", None)
    caller_root = tmp_path / "caller-storage"
    for name in ("ARTIFACTS_DIR", "WORK_DIR", "REPORTS_DIR", "INTAKE_DIR", "ARTIFACT_STORE_DIR"):
        environment[name] = str(caller_root / name)
    return subprocess.run(
        [sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "-p", "tests.pytest_support", "-c", str(storage_paths.REPOSITORY_ROOT / "pyproject.toml"),
         *arguments, str(test_file)],
        cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=30,
    )


def test_storage_and_api_snapshots_are_isolated_per_test(tmp_path):
    from mobile_playbook.api.services import reports
    from mobile_playbook.artifact_store import resolver

    for location in (storage_paths.artifacts_root(), storage_paths.work_root(), storage_paths.reports_root(),
                     storage_paths.intake_root(), storage_paths.derived_root()):
        assert location.is_relative_to(tmp_path)
    assert reports.REPORTS_ROOT == settings.REPORTS_ROOT == storage_paths.reports_root()
    assert reports.WORK_ROOT == settings.WORK_ROOT == storage_paths.work_root()
    assert resolver.ANDROID_INTAKE_DIR == storage_paths.android_intake_dir()
    assert resolver.ANDROID_WORKFLOW_APK_DIR == storage_paths.android_work_dir() / "repackaging"


def test_test_overrides_can_still_exercise_storage_precedence(tmp_path, monkeypatch):
    custom = tmp_path / "custom-work"
    monkeypatch.setenv("WORK_DIR", str(custom))
    assert storage_paths.work_root() == custom
    monkeypatch.delenv("WORK_DIR")
    assert storage_paths.work_root() == storage_paths.artifacts_root() / "work"


def test_default_settings_do_not_read_the_developer_env(tmp_path, monkeypatch):
    original_read = Path.read_text
    developer_env = storage_paths.REPOSITORY_ROOT / ".env"

    def read(path, *args, **kwargs):
        assert path != developer_env, "test attempted to read the developer .env"
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.delenv("WORK_DIR")
    monkeypatch.delenv("CORS_ALLOWED_ORIGINS")
    assert settings.ENV_FILE == tmp_path / ".env"
    assert settings.env_setting("CORS_ALLOWED_ORIGINS") is None
    assert storage_paths.work_root().is_relative_to(tmp_path)


def test_collection_isolates_import_time_paths_despite_caller_overrides(tmp_path):
    child = _run_child_pytest(tmp_path, """
import os
from pathlib import Path
from mobile_playbook.api import settings
from mobile_playbook.api.services import reports
from mobile_playbook.api.run_registry import DEFAULT_PERSIST_PATH
from mobile_playbook.artifact_store import resolver
from mobile_playbook.common import storage_paths

assert settings.ENV_FILE != storage_paths.REPOSITORY_ROOT / '.env'
assert 'caller-storage' not in os.environ['WORK_DIR']
assert reports.REPORTS_ROOT == storage_paths.reports_root()
assert reports.WORK_ROOT == storage_paths.work_root()
assert DEFAULT_PERSIST_PATH.is_relative_to(storage_paths.reports_root())
assert resolver.ANDROID_INTAKE_DIR.is_relative_to(storage_paths.intake_root())

def test_import_time_paths_are_temporary():
    assert reports.REPORTS_ROOT.is_relative_to(Path(os.environ['ARTIFACTS_DIR']))
""")

    assert child.returncode == 0, child.stdout + child.stderr
    assert "1 passed" in child.stdout
    assert not (tmp_path / "caller-storage").exists()


@pytest.mark.parametrize("phase", ["setup", "call", "teardown"])
def test_required_native_skips_fail_in_every_execution_phase(tmp_path, phase):
    child = _run_child_pytest(tmp_path, f"""
import pytest

@pytest.fixture
def lifecycle():
    if {phase!r} == 'setup':
        pytest.skip('missing synthetic native dependency')
    yield
    if {phase!r} == 'teardown':
        pytest.skip('missing synthetic native dependency')

@pytest.mark.native_capture
def test_native(lifecycle):
    if {phase!r} == 'call':
        pytest.skip('missing synthetic native dependency')
""", "--require-native-capture")

    assert child.returncode == 1, child.stdout + child.stderr
    assert "Required native capture test was skipped" in child.stdout


def test_optional_native_skips_are_allowed_locally(tmp_path):
    child = _run_child_pytest(tmp_path, """
import pytest

@pytest.mark.native_capture
def test_native():
    pytest.skip('optional native dependency is absent')
""")

    assert child.returncode == 0, child.stdout + child.stderr
    assert "1 skipped" in child.stdout


def test_required_native_mode_allows_other_skips_when_native_test_runs(tmp_path):
    child = _run_child_pytest(tmp_path, """
import pytest

@pytest.mark.native_capture
def test_native():
    assert True

def test_optional_other_feature():
    pytest.skip('unrelated optional dependency')
""", "--require-native-capture")

    assert child.returncode == 0, child.stdout + child.stderr
    assert "1 passed, 1 skipped" in child.stdout


def test_required_native_mode_rejects_a_selection_without_native_tests(tmp_path):
    child = _run_child_pytest(tmp_path, "def test_other_feature():\n    assert True\n", "--require-native-capture")

    assert child.returncode != 0
    assert "requires at least one selected native_capture test" in child.stderr
