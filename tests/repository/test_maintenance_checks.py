from pathlib import Path

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


def test_dependency_check_rejects_drift(tmp_path: Path):
    pyproject = tmp_path / "pyproject.toml"
    requirements = tmp_path / "requirements.txt"
    pyproject.write_text('[project]\ndependencies = ["example>=1"]\n', encoding="utf-8")
    requirements.write_text(HEADER + "example>=2\n", encoding="utf-8")

    assert synchronized(pyproject, requirements) is False
