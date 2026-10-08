"""Needs the compose Postgres: ``docker compose up -d db``. Skipped when the database is unreachable."""

import asyncio
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tg_discovery.config import load_settings
from tg_discovery.main import API_PREFIX, create_app

pytestmark = pytest.mark.integration

ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


async def test_migrations_enable_pg_trgm_and_create_the_discovery_tables(
    database_url: str, engine: AsyncEngine
) -> None:
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("sqlalchemy.url", database_url)

    # env.py drives its own event loop, so it has to run outside the test's loop.
    await asyncio.to_thread(command.upgrade, config, "head")

    async with engine.connect() as connection:
        extensions = (await connection.execute(text("SELECT extname FROM pg_extension"))).scalars().all()
        tables = (
            await connection.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))
        ).scalars()
    assert "pg_trgm" in extensions
    assert {
        "discovery_runs",
        "event_candidates",
        "venue_candidates",
        "sightings",
        "publish_outbox",
        "audit_log",
    } <= set(tables)


async def test_health_is_ok_against_the_real_database(database_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("APP_ENV", "test")
    app = create_app(load_settings())

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(f"{API_PREFIX}/health")

    assert response.status_code == 200
    assert response.json()["database"] == "ok"
