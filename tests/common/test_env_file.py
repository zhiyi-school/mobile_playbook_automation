from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

from mobile_playbook.common import env_file


@pytest.mark.parametrize(
    ("contents", "expected"),
    [
        ('# comment\n\nINVALID\n OTHER_KEY=ignore\n WANTED = "a=b" \n', "a=b"),
        ("WANTED='quoted value'\n", "quoted value"),
        ("WANTED=first\nWANTED=second\n", "first"),
        ("WANTED=\n", ""),
        ("OTHER_KEY=value\n", None),
    ],
)
def test_reading_a_named_setting_preserves_literal_values(tmp_path, monkeypatch, contents, expected):
    monkeypatch.delenv("WANTED", raising=False)
    path = tmp_path / ".env"
    path.write_text(contents)
    before = os.environ.copy()

    assert env_file.env_value("WANTED", path) == expected
    assert os.environ == before


@pytest.mark.parametrize("override", ["override", ""])
def test_existing_process_values_remain_optional_overrides(tmp_path, monkeypatch, override):
    path = tmp_path / ".env"
    path.write_text("WANTED=file-value\n")
    monkeypatch.setenv("WANTED", override)

    assert env_file.env_value("WANTED", path) == override


def test_missing_or_unreadable_file_returns_no_value(tmp_path, monkeypatch):
    monkeypatch.delenv("WANTED", raising=False)

    assert env_file.env_value("WANTED", tmp_path / "absent.env") is None
    assert env_file.env_value("WANTED", tmp_path) is None


def test_default_file_path_is_anchored_to_the_repository(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    spec = importlib.util.spec_from_file_location("env_file_defaults", env_file.__file__)
    defaults = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(defaults)

    assert defaults.ENV_FILE == Path(env_file.__file__).resolve().parents[2] / ".env"


def test_default_file_path_is_isolated_for_every_test(tmp_path):
    assert env_file.ENV_FILE == tmp_path / ".env"
