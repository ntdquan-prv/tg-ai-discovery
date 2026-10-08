"""FastAPI application factory. Run with ``uvicorn tg_discovery.main:create_app --factory``."""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from uuid import UUID

import structlog
from fastapi import FastAPI

from tg_discovery import __version__
from tg_discovery.api import candidates, health, runs
from tg_discovery.config import Settings, get_settings
from tg_discovery.domain.enums import Flow
from tg_discovery.logging import configure_logging
from tg_discovery.pipelines.live import live_environment
from tg_discovery.pipelines.runs import OpenEnvironment, execute_run
from tg_discovery.store import repository
from tg_discovery.store.db import create_engine, create_session_factory

API_PREFIX = "/api/v1"
DESCRIPTION = """
Finds new LGBTQ+ **venues** and **events** in the cities an admin picks, scores them, drops the junk and queues the
uncertain ones for review. Nothing is published and nothing is written to TravelGay: approving a candidate only
queues it in an outbox.

**Try it:** `POST /events/runs` (or `/venues/runs`) with a country and a city, follow it with
`GET /runs/{run_id}`, read the table with `GET /runs/{run_id}/results`, open one with
`GET /events/candidates/{id}` and decide with `approve` or `reject`.

Searches cost money and are capped per run (`BUDGET_USD_PER_RUN`); an identical request is answered by the
earlier run.
"""
TAGS = [
    {"name": "runs", "description": "Start a discovery and follow it."},
    {"name": "event review", "description": "Review the events a run found."},
    {"name": "venue review", "description": "Review the venues a run found."},
    {"name": "health"},
]


def create_app(
    settings: Settings | None = None, *, open_environment: OpenEnvironment | None = None, inline_runs: bool = False
) -> FastAPI:
    """Build the application; the database engine is created on startup and disposed on shutdown.

    ``open_environment`` supplies what a run needs from the outside (the live providers by default); ``inline_runs``
    makes a run finish before its request returns, which tests use instead of waiting for a background task.
    """
    settings = settings or get_settings()
    configure_logging(settings.log_level, json_logs=settings.app_env == "prod")
    log = structlog.get_logger()
    opener: OpenEnvironment = open_environment or (lambda: live_environment(settings))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        engine = create_engine(settings.database_url.get_secret_value())
        factory = create_session_factory(engine)
        app.state.session_factory = factory
        tasks: set[asyncio.Task[None]] = set()

        async def launch(run_id: UUID) -> None:
            work = execute_run(run_id, session_factory=factory, open_environment=opener)
            if inline_runs:
                await work
                return
            task = asyncio.create_task(work)
            tasks.add(task)
            task.add_done_callback(tasks.discard)

        app.state.launch = launch
        try:
            # A run that was in progress when the service stopped will never finish: free its slot.
            await repository.fail_interrupted_runs(factory)
        except Exception:  # the database may be down: the health endpoint says so, the service still starts
            log.warning("could not clean up interrupted runs")
        log.info("startup", environment=settings.app_env, version=__version__, flows=[flow.value for flow in Flow])
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await engine.dispose()
            log.info("shutdown")

    app = FastAPI(
        title="TG Discovery", version=__version__, description=DESCRIPTION, openapi_tags=TAGS, lifespan=lifespan
    )
    app.state.settings = settings
    app.include_router(health.router, prefix=API_PREFIX)
    app.include_router(runs.router, prefix=API_PREFIX)
    app.include_router(candidates.candidate_router(Flow.EVENT), prefix=API_PREFIX)
    app.include_router(candidates.candidate_router(Flow.VENUE), prefix=API_PREFIX)
    return app
