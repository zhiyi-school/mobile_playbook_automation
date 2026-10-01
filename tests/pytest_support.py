"""
Isolates pytest storage before collection and enforces required native capture coverage.
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

_SESSION_ENVIRONMENT = pytest.StashKey[tuple[TemporaryDirectory, pytest.MonkeyPatch]]()


# Point runtime storage and optional playbook discovery at one synthetic root.
def isolate_environment(root: Path, patch: pytest.MonkeyPatch) -> None:
    root = root.resolve()
    artifacts = root / "artifacts"
    locations = {
        "ARTIFACTS_DIR": artifacts,
        "WORK_DIR": artifacts / "work",
        "REPORTS_DIR": artifacts / "reports",
        "INTAKE_DIR": artifacts / "intake",
        "ARTIFACT_STORE_DIR": artifacts / "derived",
        "IOS_PLAYBOOK_DIR": root / "playbooks" / "ios",
        "ANDROID_PLAYBOOK_DIR": root / "playbooks" / "android",
    }
    for name, location in locations.items():
        patch.setenv(name, str(location))
    patch.setenv("CORS_ALLOWED_ORIGINS", "")


# Register an opt-in CI requirement that native capture tests must execute.
def pytest_addoption(parser) -> None:
    parser.addoption(
        "--require-native-capture", action="store_true", default=False,
        help="Fail if native_capture tests are skipped or none are selected.",
    )


# Isolate import-time settings before pytest imports test modules during collection.
def pytest_configure(config) -> None:
    directory = TemporaryDirectory(prefix="mobile-playbook-pytest-")
    root = Path(directory.name).resolve()
    patch = pytest.MonkeyPatch()
    config.stash[_SESSION_ENVIRONMENT] = (directory, patch)
    isolate_environment(root, patch)
    # Cover every allowlisted API setting during the initial settings import.
    from mobile_playbook.api import settings
    from mobile_playbook.common import storage_paths

    patch.setattr(settings, "ENV_FILE", root / ".env")
    patch.setattr(storage_paths, "CONFIG_ROOT", root / "configs")


# Restore the caller's settings and remove only this session's synthetic storage.
def pytest_unconfigure(config) -> None:
    state = config.stash.get(_SESSION_ENVIRONMENT, None)
    if state is not None:
        directory, patch = state
        patch.undo()
        directory.cleanup()
        del config.stash[_SESSION_ENVIRONMENT]


# Prevent a required native job from passing after native tests were deselected or removed.
def pytest_collection_finish(session) -> None:
    if session.config.getoption("--require-native-capture") and not session.config.getoption("collectonly"):
        if not any(item.get_closest_marker("native_capture") for item in session.items):
            raise pytest.UsageError("--require-native-capture requires at least one selected native_capture test")


# Convert native skips from any execution phase into failures when native coverage is required.
@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if (report.skipped and item.config.getoption("--require-native-capture")
            and item.get_closest_marker("native_capture")):
        reason = report.longrepr
        report.outcome = "failed"
        report.longrepr = f"Required native capture test was skipped: {item.nodeid}\n{reason}"
