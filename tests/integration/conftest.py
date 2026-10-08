"""Fixtures for the tests that need the compose Postgres: ``docker compose up -d db``.

They run in their own database (``tg_discovery_test``, created on first use and migrated to head), never in the
development one, so a test run cannot touch candidates that were discovered for real.
"""

import asyncio
import os
import socket
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

DEFAULT_URL = "postgresql+asyncpg://tg_discovery:tg_discovery@localhost:5433/tg_discovery_test"
ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"
TABLES = "discovery_runs, event_candidates, venue_candidates, sightings, publish_outbox, audit_log"


async def _create_database_if_missing(url: str) -> None:
    parsed = make_url(url)
    admin = create_async_engine(parsed.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as connection:
            found = await connection.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": parsed.database}
            )
            if not found:
                await connection.execute(text(f'CREATE DATABASE "{parsed.database}"'))
    finally:
        await admin.dispose()


@pytest.fixture(scope="session")
def database_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL", DEFAULT_URL)
    parsed = make_url(url)
    try:
        socket.create_connection((parsed.host or "localhost", parsed.port or 5432), timeout=1).close()
    except OSError:
        pytest.skip(f"Postgres is not reachable at {parsed.host}:{parsed.port}; run `docker compose up -d db`")
    asyncio.run(_create_database_if_missing(url))
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")
    return url


@pytest.fixture
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(database_url)
    yield engine
    await engine.dispose()


@pytest.fixture
async def clean_database(engine: AsyncEngine) -> None:
    """Start the test with empty discovery tables."""
    async with engine.begin() as connection:
        await connection.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY"))
