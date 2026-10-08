"""Running one discovery run from start to finish and recording how it went.

A run walks its cities one at a time: place the city, ask the providers, merge and score what they found, store the
candidates, report progress. A city that cannot be placed or a provider that fails makes the run ``partial``, not lost.
"""

from collections import Counter
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tg_discovery.domain.enums import Flow
from tg_discovery.domain.geo import BoundingBox
from tg_discovery.pipelines.discovery import (
    AreaError,
    CityRequest,
    CityTarget,
    Geocoder,
    RunEnvironment,
    RunFilter,
    prepare_events,
    prepare_venues,
)
from tg_discovery.providers.base import ProviderError
from tg_discovery.store import repository
from tg_discovery.store.models import DiscoveryRun, Json

log = structlog.get_logger()

SessionFactory = async_sessionmaker[AsyncSession]
OpenEnvironment = Callable[[], AbstractAsyncContextManager[RunEnvironment]]
MAX_ERRORS_REPORTED = 5
MAX_ERROR_CHARS = 500


async def execute_run(
    run_id: UUID,
    *,
    session_factory: SessionFactory,
    open_environment: OpenEnvironment,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    """Run a queued run to the end. It never raises: a failure is written onto the run."""
    async with session_factory() as session:
        run = await repository.mark_started(session, run_id, clock())
    if run is None:
        return
    status, stats, error, cost = "failed", {}, None, 0.0
    try:
        async with open_environment() as environment:
            status, stats, error = await _discover(environment, session_factory, run, clock)
            cost = environment.budget.spent_usd
    except Exception as exc:  # the run is the unit of failure: record it and stay alive
        log.exception("run failed", run=run.code)
        status, error = "failed", f"{type(exc).__name__}: {exc}"[:MAX_ERROR_CHARS]
    async with session_factory() as session:
        await repository.finish_run(
            session, run_id, status=status, stats=stats, cost_usd=cost, error=error, now=clock()
        )
    log.info("run finished", run=run.code, status=status, cost_usd=round(cost, 4))


async def _locate(geocoder: Geocoder, request: CityRequest, country: str, problems: list[str]) -> BoundingBox | None:
    try:
        area = await geocoder.locate(request, country)
    except (AreaError, ProviderError, ValueError) as error:
        problems.append(f"{request.name}: {error}"[:MAX_ERROR_CHARS])
        return None
    if area is None:
        problems.append(f"{request.name}: no such city in {country}")
    return area


async def _discover(
    environment: RunEnvironment, session_factory: SessionFactory, run: DiscoveryRun, clock: Callable[[], datetime]
) -> tuple[str, Json, str | None]:
    flow = Flow(run.flow)
    run_filter = RunFilter.model_validate(run.filter)
    today = clock().date()
    stats: Json = {"providers": environment.providers, "skipped": environment.skipped}
    if not environment.providers:
        reasons = "; ".join(f"{name}: {why}" for name, why in environment.skipped.items()) or "none is configured"
        return "failed", stats, f"no provider can run ({reasons})"[:MAX_ERROR_CHARS]
    calls = failed = 0
    stopped = False
    outcomes: Counter[str] = Counter()
    rejected: Counter[str] = Counter()
    by_provider: Counter[str] = Counter()
    errors: Counter[str] = Counter()
    problems: list[str] = []
    done = 0
    for request in run_filter.cities:
        area = await _locate(environment.geocoder, request, run_filter.country, problems)
        if area is None:
            continue
        city = CityTarget(
            name=request.name,
            country=run_filter.country.upper(),
            country_name=run_filter.country_name or run_filter.country.upper(),
            timezone=request.timezone,
            area=area,
            tg_city_id=request.tg_city_id,
        )
        batch = await environment.collector.collect(flow, city, window_days=run_filter.window_days, today=today)
        prepared = (
            prepare_venues(batch)
            if flow is Flow.VENUE
            else prepare_events(batch, today=today, window_days=run_filter.window_days)
        )
        async with session_factory() as session:
            saved = await repository.save_candidates(session, run.id, flow, city, prepared.candidates, clock())
        outcomes.update(saved.outcomes)
        rejected.update(prepared.rejected)
        calls += batch.stats.calls
        failed += batch.stats.failed_calls
        stopped = stopped or batch.stats.stopped_by_budget
        by_provider.update(batch.stats.drafts_by_provider)
        errors.update(batch.stats.errors)
        done += 1
        async with session_factory() as session:
            progress: Json = {"stage": "searching", "cities_done": done, "cities_total": len(run_filter.cities)}
            await repository.update_progress(session, run.id, progress)
    top_errors = [message for message, _ in errors.most_common(MAX_ERRORS_REPORTED)]
    stats.update(
        {
            "progress": {"stage": "done", "cities_done": done, "cities_total": len(run_filter.cities)},
            "calls": {"ok": calls, "failed": failed},
            "candidates": {
                "new": outcomes["new"],
                "changed": outcomes["changed"],
                "unchanged": outcomes["unchanged"],
                "rejected": dict(rejected),
            },
            "drafts_by_provider": dict(by_provider),
            "errors": top_errors,
            "problems": problems,
            "stopped_by_budget": stopped,
        }
    )
    if done == 0:
        return "failed", stats, "; ".join(problems)[:MAX_ERROR_CHARS] or "no city could be searched"
    if calls == 0 and failed > 0:
        return "failed", stats, f"every provider call failed: {'; '.join(top_errors)}"[:MAX_ERROR_CHARS]
    partial = bool(problems) or failed > 0 or stopped
    return ("partial" if partial else "succeeded"), stats, None
