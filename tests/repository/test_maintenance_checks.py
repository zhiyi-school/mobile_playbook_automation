import shutil
import subprocess
from pathlib import Path

import pytest

from scripts.check_docs import validate
from scripts.check_requirements import HEADER, synchronized


def test_documentation_check_reports_broken_links_and_machine_paths(tmp_path: Path):
    (tmp_path / "README.md").write_text(
        "# Example\n\n[missing](docs/missing.md)\n\nUse `/Users/example/private/file`.\n",
        encoding="utf-8",
    )

    errors = validate(tmp_path)

    assert any("missing local link target" in error for error in errors)
    assert any("machine-specific path" in error for error in errors)


requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def make_repository(root: Path, files: dict[str, str], tracked: list[str]) -> None:
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "--", *tracked], check=True)


@requires_git
def test_documentation_check_accepts_ignored_local_config_with_tracked_example(tmp_path: Path):
    make_repository(
        tmp_path,
        {
            ".gitignore": "configs/ios.yaml\nconfigs/split/**/*.yaml\n!configs/split/**/*.example.yaml\n",
            "README.md": "# Example\n\nEdit `configs/ios.yaml` and `configs/split/ios/risks.yaml`.\n",
            "configs/ios.example.yaml": "example: true\n",
            "configs/split/ios/risks.example.yaml": "example: true\n",
        },
        tracked=[".gitignore", "README.md", "configs/ios.example.yaml", "configs/split/ios/risks.example.yaml"],
    )

    assert validate(tmp_path) == []


@requires_git
def test_documentation_check_rejects_ignored_local_config_without_example(tmp_path: Path):
    make_repository(
        tmp_path,
        {
            ".gitignore": "configs/android.yaml\n",
            "README.md": "# Example\n\nEdit `configs/android.yaml`.\n",
            "configs/android.yaml": "local: true\n",
        },
        tracked=[".gitignore", "README.md"],
    )

    assert validate(tmp_path) == ["README.md:3: missing referenced source path: configs/android.yaml"]


@requires_git
def test_documentation_check_rejects_untracked_file_present_on_disk(tmp_path: Path):
    make_repository(
        tmp_path,
        {
            "README.md": "# Example\n\nRun `scripts/local_tool.py` or see [notes](docs/notes.md).\n",
            "scripts/local_tool.py": "",
            "docs/notes.md": "# Notes\n",
        },
        tracked=["README.md"],
    )

    assert validate(tmp_path) == [
        "README.md:3: missing local link target: docs/notes.md",
        "README.md:3: missing referenced source path: scripts/local_tool.py",
    ]


def test_documentation_check_uses_disk_outside_a_git_repository(tmp_path: Path):
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "ios.yaml").write_text("local: true\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# Example\n\nEdit `configs/ios.yaml`.\n", encoding="utf-8")

    assert validate(tmp_path, tracked=None) == []


def test_dependency_check_rejects_drift(tmp_path: Path):
    pyproject = tmp_path / "pyproject.toml"
    requirements = tmp_path / "requirements.txt"
    pyproject.write_text('[project]\ndependencies = ["example>=1"]\n', encoding="utf-8")
    requirements.write_text(HEADER + "example>=2\n", encoding="utf-8")

    assert synchronized(pyproject, requirements) is False
