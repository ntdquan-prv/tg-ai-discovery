"""Database access for runs, candidates and decisions. Every function takes the session it works in."""

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tg_discovery.domain.enums import Flow
from tg_discovery.domain.models import EventDraft, EventImage, VenueDraft, union_images
from tg_discovery.pipelines.discovery import CityTarget, Prepared, RunFilter
from tg_discovery.store.models import (
    AuditLog,
    DiscoveryRun,
    EventCandidate,
    Json,
    PublishOutbox,
    Sighting,
    VenueCandidate,
)

Candidate = EventCandidate | VenueCandidate
ACTIVE = ("queued", "running")
REUSABLE = ("succeeded", "partial")
CODE_ATTEMPTS = 5


class RunAlreadyActiveError(RuntimeError):
    """The same request is queued or running already; ``run`` is that run."""

    def __init__(self, run: DiscoveryRun) -> None:
        super().__init__(f"run {run.code} is already {run.status}")
        self.run = run


class CandidateNotFoundError(LookupError):
    """No candidate with that id."""


class VersionConflictError(RuntimeError):
    """Somebody else decided on, or changed, the candidate since it was read."""

    def __init__(self, current: int) -> None:
        super().__init__(f"the candidate is now at version {current}")
        self.current = current


class AlreadyDecidedError(RuntimeError):
    """The candidate has been approved or rejected already."""

    def __init__(self, status: str) -> None:
        super().__init__(f"the candidate is already {status}")
        self.status = status


def model_for(flow: Flow) -> type[EventCandidate] | type[VenueCandidate]:
    return EventCandidate if flow is Flow.EVENT else VenueCandidate


# -- runs ----------------------------------------------------------------------------------------------------


async def create_run(
    session: AsyncSession, *, flow: Flow, run_filter: RunFilter, requested_by: str, now: datetime
) -> DiscoveryRun:
    """Queue a run with the next human-readable code of the day (``DSC-20261008-001``)."""
    prefix = f"DSC-{now:%Y%m%d}-"
    for _ in range(CODE_ATTEMPTS):
        taken = await session.scalar(
            select(func.count()).select_from(DiscoveryRun).where(DiscoveryRun.code.like(f"{prefix}%"))
        )
        run = DiscoveryRun(
            id=uuid4(),
            code=f"{prefix}{(taken or 0) + 1:03d}",
            flow=flow.value,
            requested_by=requested_by,
            filter=run_filter.model_dump(mode="json"),
            filter_hash=run_filter.digest(flow),
            status="queued",
            stats={},
            created_at=now,
        )
        session.add(run)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            active = await find_active(session, flow, run_filter.digest(flow))
            if active is not None:
                raise RunAlreadyActiveError(active) from None  # the same request was queued a moment ago
            continue  # two runs took the same code: take the next one
        return run
    msg = "could not find a free run code"
    raise RuntimeError(msg)


async def find_active(session: AsyncSession, flow: Flow, digest: str) -> DiscoveryRun | None:
    return await session.scalar(
        select(DiscoveryRun).where(
            DiscoveryRun.flow == flow.value, DiscoveryRun.filter_hash == digest, DiscoveryRun.status.in_(ACTIVE)
        )
    )


async def find_reusable(
    session: AsyncSession, flow: Flow, digest: str, *, ttl_hours: int, now: datetime
) -> DiscoveryRun | None:
    """A queued or running run of the same request, or one that finished within the TTL: it answers again for free."""
    active = await find_active(session, flow, digest)
    if active is not None or ttl_hours <= 0:
        return active
    return await session.scalar(
        select(DiscoveryRun)
        .where(
            DiscoveryRun.flow == flow.value,
            DiscoveryRun.filter_hash == digest,
            DiscoveryRun.status.in_(REUSABLE),
            DiscoveryRun.finished_at >= now - timedelta(hours=ttl_hours),
        )
        .order_by(DiscoveryRun.finished_at.desc())
        .limit(1)
    )


async def get_run(session: AsyncSession, run_id: UUID) -> DiscoveryRun | None:
    return await session.get(DiscoveryRun, run_id)


async def list_runs(
    session: AsyncSession, *, flow: Flow | None, status: str | None, limit: int
) -> Sequence[DiscoveryRun]:
    query = select(DiscoveryRun).order_by(DiscoveryRun.created_at.desc()).limit(limit)
    if flow is not None:
        query = query.where(DiscoveryRun.flow == flow.value)
    if status is not None:
        query = query.where(DiscoveryRun.status == status)
    return (await session.scalars(query)).all()


async def mark_started(session: AsyncSession, run_id: UUID, now: datetime) -> DiscoveryRun | None:
    run = await session.get(DiscoveryRun, run_id)
    if run is not None:
        run.status = "running"
        run.started_at = now
        await session.commit()
    return run


async def fail_interrupted_runs(session_factory: async_sessionmaker[AsyncSession]) -> int:
    """Mark runs that were queued or running when the service stopped as failed, so their request can run again."""
    async with session_factory() as session:
        result = await session.execute(
            update(DiscoveryRun)
            .where(DiscoveryRun.status.in_(ACTIVE))
            .values(status="failed", error="the service stopped while the run was in progress", finished_at=func.now())
        )
        await session.commit()
        return int(result.rowcount or 0)  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType, reportUnknownArgumentType]


async def update_progress(session: AsyncSession, run_id: UUID, progress: Json) -> None:
    run = await session.get(DiscoveryRun, run_id)
    if run is not None:
        run.stats = {**run.stats, "progress": progress}
        await session.commit()


async def finish_run(
    session: AsyncSession, run_id: UUID, *, status: str, stats: Json, cost_usd: float, error: str | None, now: datetime
) -> None:
    run = await session.get(DiscoveryRun, run_id)
    if run is not None:
        run.status = status
        run.stats = stats
        run.cost_usd = Decimal(str(round(cost_usd, 4)))
        run.error = error
        run.finished_at = now
        await session.commit()


# -- candidates ----------------------------------------------------------------------------------------------


def _scores(prepared: Prepared) -> Json:
    scored = prepared.scored
    return {"total": scored.total, "parts": scored.parts, "concerns": scored.concerns}


def _event_columns(draft: EventDraft) -> Json:
    starts = [occurrence.start_date for occurrence in draft.occurrences]
    return {
        "occurrences": [occurrence.model_dump(mode="json") for occurrence in draft.occurrences],
        "first_start": min(starts) if starts else None,
        "last_end": max((occurrence.last_date for occurrence in draft.occurrences), default=None),
    }


def _venue_columns(draft: VenueDraft) -> Json:
    return {"address": draft.address, "lat": draft.lat, "lng": draft.lng, "external_ids": draft.external_ids}


def _columns(prepared: Prepared) -> Json:
    """The columns that follow the merged draft, for a new candidate and for a changed one."""
    draft = prepared.draft
    specific = _event_columns(draft) if isinstance(draft, EventDraft) else _venue_columns(draft)
    return {
        "name": draft.name[:300],
        "score": Decimal(str(prepared.scored.total)),
        "scores": _scores(prepared),
        "payload": draft.model_dump(mode="json"),
        "content_hash": prepared.content_hash,
        "fast_approve": prepared.scored.fast_approve,
        **specific,
    }


def _merged_images(payload: Json, draft: EventDraft) -> list[Json]:
    """The pictures stored for an event plus the ones a run just found, each address once: a run only adds pictures."""
    stored = payload.get("images")
    known = [EventImage.model_validate(raw) for raw in cast(list[object], stored)] if isinstance(stored, list) else []
    return [cast(Json, image.model_dump(mode="json")) for image in union_images(known, draft.images)]


# Fields outside the content hash: a run that only adds these (or pictures) stays "unchanged" and fills the empty ones
# of a candidate awaiting review, without a new version; a stored value is never replaced.
_GAP_FIELDS = (
    "phone",
    "opening_hours",
    "description",
    "price_text",
    "organizer",
    "organizer_url",
    "organizer_email",
    "organizer_phone",
)


def _filled_gaps(payload: Json, draft: EventDraft | VenueDraft) -> Json:
    """The details a run found that the stored candidate lacks; a stored value is never replaced."""
    found = {name: getattr(draft, name, None) for name in _GAP_FIELDS}
    return {name: value for name, value in found.items() if value and not payload.get(name)}


@dataclass(slots=True)
class SaveResult:
    outcomes: Counter[str]
    candidate_ids: list[UUID]


async def save_candidates(
    session: AsyncSession, run_id: UUID, flow: Flow, city: CityTarget, prepared: Sequence[Prepared], now: datetime
) -> SaveResult:
    """Insert new candidates and refresh known ones; a decision a human took is never undone by a later run."""
    model = model_for(flow)
    result = SaveResult(Counter(), [])
    if not prepared:
        return result
    found = await session.scalars(select(model).where(model.fingerprint.in_([item.fingerprint for item in prepared])))
    existing: dict[str, Candidate] = {row.fingerprint: row for row in found}
    for item in prepared:
        row = existing.get(item.fingerprint)
        if row is None:
            row = model(
                id=uuid4(),
                fingerprint=item.fingerprint,
                status="needs_review",
                city=city.name,
                country_code=city.country,
                tg_city_id=city.tg_city_id,
                found_by=item.providers,
                first_run_id=run_id,
                first_seen_at=now,
                last_seen_at=now,
                last_changed_at=now,
                seen_count=1,
                version=1,
                **_columns(item),
            )
            session.add(row)
            outcome = "new"
        else:
            outcome = "unchanged" if row.content_hash == item.content_hash else "changed"
            row.last_seen_at = now
            row.seen_count += 1
            row.found_by = sorted({*row.found_by, *item.providers})
            draft = item.draft
            images = (
                _merged_images(row.payload, draft)
                if isinstance(draft, EventDraft) and row.status == "needs_review"
                else None
            )
            if outcome == "changed":
                for name, value in _columns(item).items():
                    setattr(row, name, value)
                row.last_changed_at = now
            quiet = _filled_gaps(row.payload, draft) if row.status == "needs_review" else {}
            if images is not None and images != row.payload.get("images", []):
                quiet["images"] = images
            if quiet:
                row.payload = {**row.payload, **quiet}
        result.outcomes[outcome] += 1
        result.candidate_ids.append(row.id)
        session.add(
            Sighting(
                run_id=run_id,
                flow=flow.value,
                candidate_id=row.id,
                outcome=outcome,
                providers=item.providers,
                score=Decimal(str(item.scored.total)),
                seen_at=now,
            )
        )
    await session.commit()
    return result


async def get_candidate(session: AsyncSession, flow: Flow, candidate_id: UUID) -> Candidate | None:
    return await session.get(model_for(flow), candidate_id)


async def list_candidates(
    session: AsyncSession,
    flow: Flow,
    *,
    status: str | None,
    city: str | None,
    q: str | None,
    limit: int,
    offset: int,
) -> tuple[Sequence[Candidate], int]:
    model = model_for(flow)
    query = select(model)
    if status:
        query = query.where(model.status == status)
    if city:
        query = query.where(model.city.ilike(city))
    if q:
        query = query.where(model.name.ilike(f"%{q}%"))
    total = await session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = (await session.scalars(query.order_by(model.score.desc(), model.name).limit(limit).offset(offset))).all()
    return rows, total


async def run_candidate_ids(
    session: AsyncSession, run_id: UUID, flow: Flow, *, outcome: str | None = None
) -> list[UUID]:
    """The candidates a run saw, best score first: the order a reviewer walks through them."""
    query = select(Sighting.candidate_id).where(Sighting.run_id == run_id, Sighting.flow == flow.value)
    if outcome:
        query = query.where(Sighting.outcome == outcome)
    return list((await session.scalars(query.order_by(Sighting.score.desc(), Sighting.id))).all())


async def candidates_by_id(session: AsyncSession, flow: Flow, ids: Sequence[UUID]) -> dict[UUID, Candidate]:
    if not ids:
        return {}
    model = model_for(flow)
    return {row.id: row for row in await session.scalars(select(model).where(model.id.in_(ids)))}


# -- decisions -----------------------------------------------------------------------------------------------


async def decide(
    session: AsyncSession,
    flow: Flow,
    candidate_id: UUID,
    *,
    actor: str,
    approve: bool,
    version: int,
    reason: str | None,
    note: str | None,
) -> Candidate:
    """Approve or reject a candidate. Approving only queues it in the outbox: nothing is published from here."""
    row = await session.get(model_for(flow), candidate_id, with_for_update=True)
    if row is None:
        raise CandidateNotFoundError(str(candidate_id))
    if row.version != version:
        raise VersionConflictError(row.version)
    if row.status != "needs_review":
        raise AlreadyDecidedError(row.status)
    now = datetime.now(UTC)
    row.status = "approved" if approve else "rejected"
    row.reviewed_by = actor
    row.reviewed_at = now
    row.review_note = note
    row.rejection_reason = None if approve else reason
    row.version += 1
    if approve:
        session.add(PublishOutbox(flow=flow.value, candidate_id=row.id, payload=row.payload))
    session.add(
        AuditLog(
            actor=actor,
            action="approve" if approve else "reject",
            target_type=flow.value,
            target_id=str(row.id),
            details={"version": version, "reason": reason, "note": note},
        )
    )
    await session.commit()
    return row
