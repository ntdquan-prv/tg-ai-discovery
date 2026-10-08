import pytest
from pydantic import ValidationError

from tg_discovery.config import ProviderSettings, load_settings


def test_plain_postgres_url_is_switched_to_asyncpg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost/db")

    assert load_settings().database_url.get_secret_value() == "postgresql+asyncpg://u:p@localhost/db"


def test_legacy_postgres_scheme_is_switched_to_asyncpg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgres://u:p@localhost/db")

    assert load_settings().database_url.get_secret_value() == "postgresql+asyncpg://u:p@localhost/db"


def test_non_postgres_url_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "mysql://u:p@localhost/db")

    with pytest.raises(ValidationError):
        load_settings()


def test_database_url_is_required() -> None:
    with pytest.raises(ValidationError):
        load_settings()


def test_secrets_do_not_leak_into_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:hunter2@localhost/db")
    monkeypatch.setenv("BRAVE_API_KEY", "brave-key")

    settings = load_settings()

    assert "hunter2" not in repr(settings)
    assert "brave-key" not in repr(settings)


def test_empty_provider_key_means_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRAVE_API_KEY", "")
    monkeypatch.setenv("EXA_API_KEY", "exa-key")

    settings = ProviderSettings()

    assert settings.brave_api_key is None
    assert settings.exa_api_key is not None
    assert settings.exa_api_key.get_secret_value() == "exa-key"


def test_provider_settings_do_not_require_a_database() -> None:
    assert ProviderSettings().extractor_model_id == "openai.gpt-oss-120b"
