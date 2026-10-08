"""Starting discovery and following it: ``POST /events/runs``, ``POST /venues/runs`` and the run endpoints."""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, status

from tg_discovery.api.auth import Runner, Viewer
from tg_discovery.api.presenters import candidate_row, run_out
from tg_discovery.api.schemas import CandidateRow, FlowName, Page, RunAccepted, RunOut, RunRequest, RunStatus
from tg_discovery.deps import SessionDep
from tg_discovery.domain.enums import Flow
from tg_discovery.pipelines.discovery import RunFilter
from tg_discovery.store import repository
from tg_discovery.store.models import DiscoveryRun

router = APIRouter(tags=["runs"])

Launcher = Callable[[UUID], Awaitable[None]]
MAX_PAGE = 100


def _accepted(run: DiscoveryRun, *, deduplicated: bool) -> RunAccepted:
    return RunAccepted(run_id=run.id, code=run.code, status=cast(RunStatus, run.status), deduplicated=deduplicated)


async def _start(flow: Flow, body: RunRequest, request: Request, session: SessionDep, admin: Runner) -> RunAccepted:
    settings = request.app.state.settings
    run_filter = RunFilter.model_validate(body.model_dump(exclude={"force"}))
    digest = run_filter.digest(flow)
    now = datetime.now(UTC)
    ttl = settings.run_ttl_event_hours if flow is Flow.EVENT else settings.run_ttl_venue_hours
    # Single flight: an identical run that is active (or, unless forced, recent) answers instead of a new one.
    existing = (
        await repository.find_active(session, flow, digest)
        if body.force
        else await repository.find_reusable(session, flow, digest, ttl_hours=ttl, now=now)
    )
    if existing is not None:
        return _accepted(existing, deduplicated=True)
    try:
        run = await repository.create_run(session, flow=flow, run_filter=run_filter, requested_by=admin.id, now=now)
    except repository.RunAlreadyActiveError as active:
        return _accepted(active.run, deduplicated=True)
    launch: Launcher = request.app.state.launch
    await launch(run.id)
    return _accepted(run, deduplicated=False)


@router.post(
    "/events/runs",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Discovery: look for new events",
    description=(
        "Searches the chosen cities for upcoming LGBTQ+ events, scores what it finds and queues the uncertain ones for "
        "review. Returns at once; follow the run with `GET /runs/{run_id}`. The same request made again while it runs, "
        "or within a few hours of finishing, returns that run (`deduplicated: true`) and costs nothing."
    ),
)
async def start_event_run(body: RunRequest, request: Request, session: SessionDep, admin: Runner) -> RunAccepted:
    return await _start(Flow.EVENT, body, request, session, admin)


@router.post(
    "/venues/runs",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Discovery: look for new venues",
    description=(
        "Like the events run, for venues: bars, clubs, saunas, cafes... Venues change slowly, so a finished run "
        "answers the same request for a week."
    ),
)
async def start_venue_run(body: RunRequest, request: Request, session: SessionDep, admin: Runner) -> RunAccepted:
    return await _start(Flow.VENUE, body, request, session, admin)


@router.get("/runs", summary="List runs, newest first")
async def list_runs(
    session: SessionDep,
    _: Viewer,
    flow: FlowName | None = None,
    run_status: Annotated[RunStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 20,
) -> list[RunOut]:
    runs = await repository.list_runs(session, flow=Flow(flow) if flow else None, status=run_status, limit=limit)
    return [run_out(run) for run in runs]


@router.get("/runs/{run_id}", summary="One run: its status, progress, cost and what it found")
async def get_run(run_id: UUID, session: SessionDep, _: Viewer) -> RunOut:
    run = await repository.get_run(session, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such run")
    return run_out(run)


@router.get("/runs/{run_id}/results", summary="The candidates a run found: the rows of the review table")
async def run_results(
    run_id: UUID,
    session: SessionDep,
    _: Viewer,
    outcome: Annotated[str | None, Query(pattern="^(new|changed|unchanged)$")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[CandidateRow]:
    run = await repository.get_run(session, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such run")
    flow = Flow(run.flow)
    ids = await repository.run_candidate_ids(session, run_id, flow, outcome=outcome)
    page = ids[offset : offset + limit]
    rows = await repository.candidates_by_id(session, flow, page)
    today = datetime.now(UTC).date()
    items = [candidate_row(rows[item], flow, today) for item in page if item in rows]
    return Page[CandidateRow](
        items=items, total=len(ids), next_offset=offset + limit if offset + limit < len(ids) else None
    )
