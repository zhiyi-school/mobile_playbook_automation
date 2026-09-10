

def test_runner_work_dir_defaults_under_the_artifacts_root(tmp_path, monkeypatch):
    """An unset work_dir must not fall back to a repository-root relative path."""
    from mobile_playbook.platforms.ios.config import parse_config
    from mobile_playbook.storage import ios_work_dir

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
