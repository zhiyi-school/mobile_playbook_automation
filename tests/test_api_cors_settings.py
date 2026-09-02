from __future__ import annotations

from pathlib import Path

import pytest

from mobile_playbook.api import settings
from mobile_playbook.api.cors import DEFAULT_CORS_ORIGINS, cors_allowed_origins

SECRET_KEYS = ("SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_URL", "MOBSF_API_KEY")


@pytest.fixture(autouse=True)
def unexported(monkeypatch):
    monkeypatch.delenv("CORS_ALLOWED_ORIGINS", raising=False)


def _env_file(tmp_path: Path, text: str) -> Path:
    path = tmp_path / ".env"
    path.write_text(text)
    return path


def test_defaults_apply_when_nothing_is_configured_anywhere(tmp_path):
    assert cors_allowed_origins(tmp_path / "absent.env") == DEFAULT_CORS_ORIGINS


def test_defaults_apply_when_the_env_file_does_not_set_the_key(tmp_path):
    env = _env_file(tmp_path, 'MOBSF_API_KEY="abc"\nDASHBOARD_SYNC_AUTO_TRIGGER=true\n')

    assert cors_allowed_origins(env) == DEFAULT_CORS_ORIGINS


def test_the_value_is_read_from_the_repository_env_file(tmp_path):
    env = _env_file(tmp_path, 'CORS_ALLOWED_ORIGINS="https://dashboard.example.com"\n')

    assert cors_allowed_origins(env) == ["https://dashboard.example.com"]


def test_an_exported_variable_wins_over_the_env_file(tmp_path, monkeypatch):
    env = _env_file(tmp_path, "CORS_ALLOWED_ORIGINS=https://from-dotenv.example.com\n")
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "https://from-shell.example.com")

    assert cors_allowed_origins(env) == ["https://from-shell.example.com"]


def test_an_exported_empty_value_still_wins_and_falls_back_to_defaults(tmp_path, monkeypatch):
    env = _env_file(tmp_path, "CORS_ALLOWED_ORIGINS=https://from-dotenv.example.com\n")
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "")

    assert cors_allowed_origins(env) == DEFAULT_CORS_ORIGINS


def test_several_origins_are_kept_in_order(tmp_path):
    env = _env_file(tmp_path, "CORS_ALLOWED_ORIGINS=https://a.example.com,https://b.example.com,http://c:5173\n")

    assert cors_allowed_origins(env) == [
        "https://a.example.com",
        "https://b.example.com",
        "http://c:5173",
    ]


def test_surrounding_whitespace_and_empty_entries_are_trimmed(tmp_path):
    env = _env_file(tmp_path, 'CORS_ALLOWED_ORIGINS = "  https://a.example.com ,, https://b.example.com  , "\n')

    assert cors_allowed_origins(env) == ["https://a.example.com", "https://b.example.com"]


def test_origins_are_kept_exact_rather_than_normalised(tmp_path):
    env = _env_file(tmp_path, "CORS_ALLOWED_ORIGINS=https://Dashboard.Example.com:8443\n")

    assert cors_allowed_origins(env) == ["https://Dashboard.Example.com:8443"]


@pytest.mark.parametrize("value", ["*", "https://a.example.com,*", " * , https://a.example.com "])
def test_a_wildcard_is_rejected_from_the_env_file_too(tmp_path, value):
    env = _env_file(tmp_path, f"CORS_ALLOWED_ORIGINS={value}\n")

    with pytest.raises(RuntimeError, match="not allowed"):
        cors_allowed_origins(env)


def test_a_wildcard_is_still_rejected_when_exported(tmp_path, monkeypatch):
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "*")

    with pytest.raises(RuntimeError, match="not allowed"):
        cors_allowed_origins(tmp_path / "absent.env")


def test_a_missing_env_file_is_not_an_error(tmp_path):
    assert cors_allowed_origins(tmp_path / "nope" / ".env") == DEFAULT_CORS_ORIGINS


def test_a_directory_where_the_env_file_should_be_is_not_an_error(tmp_path):
    (tmp_path / ".env").mkdir()

    assert cors_allowed_origins(tmp_path / ".env") == DEFAULT_CORS_ORIGINS


def test_malformed_lines_are_skipped_rather_than_crashing(tmp_path):
    env = _env_file(
        tmp_path,
        "\n".join(
            [
                "# a comment",
                "   ",
                "NO_EQUALS_SIGN",
                "=value-with-no-key",
                "  # CORS_ALLOWED_ORIGINS=https://commented-out.example.com",
                "CORS_ALLOWED_ORIGINS=https://real.example.com",
                "TRAILING_JUNK",
            ]
        ),
    )

    assert cors_allowed_origins(env) == ["https://real.example.com"]


def test_the_first_definition_wins_over_a_later_duplicate(tmp_path):
    env = _env_file(
        tmp_path,
        "CORS_ALLOWED_ORIGINS=https://first.example.com\nCORS_ALLOWED_ORIGINS=https://second.example.com\n",
    )

    assert cors_allowed_origins(env) == ["https://first.example.com"]


def test_a_value_containing_an_equals_sign_survives(tmp_path):
    env = _env_file(tmp_path, "CORS_ALLOWED_ORIGINS=https://a.example.com/?x=1\n")

    assert cors_allowed_origins(env) == ["https://a.example.com/?x=1"]


@pytest.mark.parametrize("key", SECRET_KEYS)
def test_the_api_loader_refuses_to_read_a_credential(tmp_path, key):
    _env_file(tmp_path, f'{key}="super-secret-value"\n')

    with pytest.raises(settings.DisallowedSettingError):
        settings.env_setting(key, tmp_path / ".env")


@pytest.mark.parametrize("key", SECRET_KEYS)
def test_resolving_cors_never_imports_a_credential_into_the_process(tmp_path, monkeypatch, key):
    monkeypatch.delenv(key, raising=False)
    env = _env_file(
        tmp_path,
        f'{key}="super-secret-value"\nCORS_ALLOWED_ORIGINS=https://a.example.com\n',
    )

    assert cors_allowed_origins(env) == ["https://a.example.com"]
    import os

    assert key not in os.environ


def test_only_non_secret_keys_are_allowlisted():
    assert settings.ALLOWED_ENV_KEYS == frozenset(
        {"CORS_ALLOWED_ORIGINS", "ARTIFACT_STORE_DIR", "IOS_PLAYBOOK_DIR", "ANDROID_PLAYBOOK_DIR"}
    )
    for key in SECRET_KEYS:
        assert key not in settings.ALLOWED_ENV_KEYS


def test_the_api_package_never_loads_the_whole_env_file():
    source = Path(settings.__file__).read_text()
    assert "load_env_file" not in source

    from mobile_playbook.api import app, cors

    for module in (app, cors, settings):
        assert not hasattr(module, "load_env_file")


def test_the_env_file_is_resolved_from_the_repository_root_not_the_working_directory():
    assert settings.ENV_FILE == settings.REPOSITORY_ROOT / ".env"
    assert (settings.REPOSITORY_ROOT / "mobile_playbook" / "api" / "settings.py").is_file()


def test_the_app_is_built_with_the_resolved_origins(tmp_path, monkeypatch):
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "https://built.example.com,https://also.example.com")
    import importlib

    from mobile_playbook.api import app as app_module

    reloaded = importlib.reload(app_module)
    try:
        middleware = [m for m in reloaded.app.user_middleware if "CORSMiddleware" in str(m.cls)]
        assert middleware, "the app must still install CORS middleware"
        assert middleware[0].kwargs["allow_origins"] == [
            "https://built.example.com",
            "https://also.example.com",
        ]
    finally:
        monkeypatch.delenv("CORS_ALLOWED_ORIGINS", raising=False)
        importlib.reload(app_module)


def test_the_app_falls_back_to_the_defaults_when_nothing_is_configured(monkeypatch):
    monkeypatch.delenv("CORS_ALLOWED_ORIGINS", raising=False)
    monkeypatch.setattr(settings, "ENV_FILE", Path("/nonexistent/.env"))
    import importlib

    from mobile_playbook.api import app as app_module

    reloaded = importlib.reload(app_module)
    middleware = [m for m in reloaded.app.user_middleware if "CORSMiddleware" in str(m.cls)]

    assert middleware[0].kwargs["allow_origins"] == DEFAULT_CORS_ORIGINS
