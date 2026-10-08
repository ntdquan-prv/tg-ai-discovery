"""Async SQLAlchemy engine and session factory."""

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

CONNECT_TIMEOUT_S = 5


def create_engine(database_url: str) -> AsyncEngine:
    """Create the async engine; a short connect timeout keeps the health probe from hanging."""
    return create_async_engine(database_url, pool_pre_ping=True, connect_args={"timeout": CONNECT_TIMEOUT_S})


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Create the session factory used by request handlers and workers."""
    return async_sessionmaker(engine, expire_on_commit=False)
