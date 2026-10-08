from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.exc import OperationalError

from tg_discovery import __version__
from tg_discovery.config import load_settings
from tg_discovery.deps import get_session
from tg_discovery.main import API_PREFIX, create_app


class _HealthySession:
    async def execute(self, *_args: object, **_kwargs: object) -> None:
        return None


class _BrokenSession:
    async def execute(self, *_args: object, **_kwargs: object) -> None:
        raise OperationalError("SELECT 1", {}, ConnectionRefusedError("db is down"))


class _RefusedSession:
    async def execute(self, *_args: object, **_kwargs: object) -> None:
        raise ConnectionRefusedError("connection refused")


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost/db")
    monkeypatch.setenv("APP_ENV", "test")
    return create_app(load_settings())


def _use_session(app: FastAPI, session: object) -> None:
    async def _override() -> AsyncIterator[object]:
        yield session

    app.dependency_overrides[get_session] = _override


async def _get_health(app: FastAPI) -> httpx.Response:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        return await client.get(f"{API_PREFIX}/health")


async def test_health_is_ok_when_the_database_answers(app: FastAPI) -> None:
    _use_session(app, _HealthySession())

    response = await _get_health(app)

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok", "version": __version__, "environment": "test"}


@pytest.mark.parametrize("session", [_BrokenSession(), _RefusedSession()])
async def test_health_is_degraded_when_the_database_is_unreachable(app: FastAPI, session: object) -> None:
    _use_session(app, session)

    response = await _get_health(app)

    assert response.status_code == 503
    assert response.json()["status"] == "degraded"
    assert response.json()["database"] == "down"
