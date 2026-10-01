

def test_runner_work_dir_defaults_under_the_artifacts_root(tmp_path, monkeypatch):
    """An unset work_dir must not fall back to a repository-root relative path."""
    from mobile_playbook.platforms.ios.config import parse_config
    from mobile_playbook.common.storage_paths import ios_work_dir

    monkeypatch.setenv("ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    monkeypatch.delenv("WORK_DIR", raising=False)

    config = parse_config({"apps": []})

    assert config.runner.work_dir == ios_work_dir()
    assert config.runner.work_dir.is_absolute()


def test_an_explicit_work_dir_is_still_honoured(tmp_path, monkeypatch):
    from pathlib import Path

    from mobile_playbook.platforms.ios.config import parse_config

    monkeypatch.setenv("ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    config = parse_config({"apps": [], "runner": {"work_dir": "/somewhere/else"}})

    assert config.runner.work_dir == Path("/somewhere/else")


def test_a_relative_work_dir_resolves_against_the_repository_not_the_working_directory(tmp_path, monkeypatch):
    from mobile_playbook.common.storage_paths import REPOSITORY_ROOT
    from mobile_playbook.platforms.ios.config import parse_config

    monkeypatch.chdir(tmp_path)
    config = parse_config({"apps": [], "runner": {"work_dir": "custom/ios-work"}})

    assert config.runner.work_dir == REPOSITORY_ROOT / "custom" / "ios-work"


def test_the_acquired_folder_follows_workspace_dir_or_the_work_root(tmp_path, monkeypatch):
    from mobile_playbook.common.storage_paths import REPOSITORY_ROOT, ios_acquired_dir, ios_work_dir

    monkeypatch.setenv("WORK_DIR", str(tmp_path / "work"))
    monkeypatch.chdir(tmp_path)

    assert ios_acquired_dir(None) == ios_work_dir() / "acquired" == tmp_path / "work" / "ios" / "acquired"
    assert ios_acquired_dir("custom/acquired") == REPOSITORY_ROOT / "custom" / "acquired"
    assert ios_acquired_dir(str(tmp_path / "elsewhere")) == (tmp_path / "elsewhere").resolve()
