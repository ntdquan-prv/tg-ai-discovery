"""Health probe: reports the service version and whether the database answers."""

import asyncio
from typing import Literal

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from tg_discovery import __version__
from tg_discovery.deps import SessionDep, SettingsDep

DB_PING_TIMEOUT_S = 5

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    """Body returned by ``GET /health``."""

    status: Literal["ok", "degraded"]
    database: Literal["ok", "down"]
    version: str
    environment: str


async def _database_is_up(session: AsyncSession) -> bool:
    try:
        async with asyncio.timeout(DB_PING_TIMEOUT_S):
            await session.execute(text("SELECT 1"))
    except (SQLAlchemyError, OSError):  # TimeoutError is an OSError
        return False
    return True


@router.get("/health", response_model=HealthResponse, responses={503: {"model": HealthResponse}})
async def health(session: SessionDep, settings: SettingsDep) -> JSONResponse:
    """Return 200 when the database answers and 503 otherwise."""
    database_up = await _database_is_up(session)
    body = HealthResponse(
        status="ok" if database_up else "degraded",
        database="ok" if database_up else "down",
        version=__version__,
        environment=settings.app_env,
    )
    return JSONResponse(status_code=200 if database_up else 503, content=body.model_dump())
