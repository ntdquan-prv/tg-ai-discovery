"""Application settings, loaded from the environment and an optional ``.env`` file."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_ASYNC_POSTGRES_SCHEME = "postgresql+asyncpg://"
_PLAIN_POSTGRES_SCHEMES = ("postgresql://", "postgres://")


def _names(text: str) -> list[str]:
    """The names in a comma-separated setting, without blanks."""
    return [name.strip() for name in text.split(",") if name.strip()]


class ProviderSettings(BaseSettings):
    """API keys and endpoints for the LLM and the search providers; every value is optional.

    A provider whose key is missing is skipped rather than failing the run.
    """

    # An empty ``KEY=`` line in .env means "not configured", not "configured with an empty secret".
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore", env_ignore_empty=True)

    # LLM used by the shared extractor (Bedrock, OpenAI-compatible Responses API). The base URL must match the
    # model's path prefix: ``/v1`` for gpt-oss, ``/openai/v1`` for gpt-5.x.
    aws_bedrock_api_token: SecretStr | None = None
    aws_bedrock_openai_url: str | None = None
    extractor_model_id: str = "openai.gpt-oss-120b"
    # List prices, not verified on the vendor's page.
    extractor_input_usd_per_1m_tokens: float = 0.15
    extractor_output_usd_per_1m_tokens: float = 0.60
    # Reasoning models spend output tokens thinking; "none" omits the parameter if the endpoint rejects it.
    extractor_reasoning_effort: Literal["low", "medium", "high", "none"] = "low"
    extractor_max_output_tokens: int = 8192
    # Per-call timeout. A batch of pages takes 30 to 100+ s with gpt-oss on Bedrock; a timed-out call is not retried
    # and its pages are skipped.
    extractor_timeout_s: float = Field(default=240.0, ge=10, le=900)
    # Debugging: log the first N characters of each page sent to the model (0 = off). Page text is third-party content
    # that is otherwise never kept.
    log_page_text_chars: int = Field(default=0, ge=0, le=6000)
    # gpt-oss on Bedrock only accepts ``auto``; turn this on for a model that can be made to call the named function.
    extractor_forced_tool_choice: bool = False

    exa_api_key: SecretStr | None = None
    serpapi_api_key: SecretStr | None = None

    # Nominatim and the public Overpass instances require a User-Agent that identifies the application.
    nominatim_user_agent: str | None = None


class Settings(ProviderSettings):
    """Full service settings."""

    app_env: Literal["dev", "test", "prod"] = "dev"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    database_url: SecretStr

    # When set, every API call must carry it as ``Authorization: Bearer``; unset is allowed only outside production.
    service_token: SecretStr | None = None
    # Providers whose vendor terms were accepted, by name (SerpApi does not run otherwise).
    accepted_terms: str = ""
    # Which providers a run may use; empty means every provider that has a key and whose terms were accepted.
    providers_enabled: str = ""
    budget_usd_per_run: float = Field(default=1.0, gt=0, le=50)
    run_ttl_event_hours: int = Field(default=3, ge=0)  # a finished run this recent answers the same request again
    run_ttl_venue_hours: int = Field(default=168, ge=0)
    city_radius_km: float = Field(default=15.0, gt=0, le=100)  # the search area around a city centre
    # Read the pages events were found on to collect their picture URLs (robots.txt honoured, nothing downloaded).
    fetch_event_pictures: bool = True
    # Keep the phone, opening hours, description and price that Google shows (via SerpApi). Off by default: Google's
    # terms do not allow storing them and Google v. SerpApi is pending, so
    # Google-derived drafts keep only names, addresses, coordinates, URLs and ids. Needs legal sign-off before such data
    # is published. Socials, organisers, pictures and evidence quotes from Google are dropped either way.
    keep_google_details: bool = False

    @property
    def accepted_providers(self) -> frozenset[str]:
        return frozenset(_names(self.accepted_terms))

    @property
    def enabled_providers(self) -> list[str] | None:
        """The providers a run may use, or ``None`` for every available one."""
        return _names(self.providers_enabled) or None

    @field_validator("database_url", mode="before")
    @classmethod
    def _use_asyncpg_driver(cls, value: object) -> object:
        """Accept a plain ``postgresql://`` URL and switch it to the async driver."""
        if isinstance(value, str):
            for scheme in _PLAIN_POSTGRES_SCHEMES:
                if value.startswith(scheme):
                    return _ASYNC_POSTGRES_SCHEME + value.removeprefix(scheme)
        return value

    @field_validator("database_url")
    @classmethod
    def _require_asyncpg_driver(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().startswith(_ASYNC_POSTGRES_SCHEME):
            msg = f"DATABASE_URL must start with {_ASYNC_POSTGRES_SCHEME!r}"
            raise ValueError(msg)
        return value


def load_settings() -> Settings:
    """Read and validate the settings from the environment (and ``.env``)."""
    return Settings()  # pyright: ignore[reportCallIssue]  # database_url comes from the environment


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings, read once."""
    return load_settings()
