"""The review side: list candidates, open the review packet of one, approve or reject it.

The two flows have the same endpoints under ``/events/candidates`` and ``/venues/candidates``.
"""

from datetime import UTC, datetime
from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from tg_discovery.api.auth import Reviewer, Viewer
from tg_discovery.api.presenters import candidate_row, review_packet
from tg_discovery.api.schemas import (
    ApproveIn,
    CandidateRow,
    CandidateStatus,
    DecisionOut,
    Page,
    Position,
    RejectIn,
    ReviewPacket,
)
from tg_discovery.deps import SessionDep
from tg_discovery.domain.enums import Flow
from tg_discovery.store import repository

MAX_PAGE = 100


def _position(ids: list[UUID], candidate_id: UUID) -> Position | None:
    if candidate_id not in ids:
        return None
    index = ids.index(candidate_id)
    return Position(
        index=index + 1,
        total=len(ids),
        prev_id=ids[index - 1] if index > 0 else None,
        next_id=ids[index + 1] if index + 1 < len(ids) else None,
    )


def _conflict(error: repository.VersionConflictError | repository.AlreadyDecidedError) -> HTTPException:
    if isinstance(error, repository.VersionConflictError):
        detail: dict[str, object] = {
            "code": "version_conflict",
            "current_version": error.current,
            "message": str(error),
        }
    else:
        detail = {"code": "already_decided", "status": error.status, "message": str(error)}
    return HTTPException(status.HTTP_409_CONFLICT, detail)


def candidate_router(flow: Flow) -> APIRouter:
    """The candidate endpoints of one flow."""
    noun = "event" if flow is Flow.EVENT else "venue"
    router = APIRouter(prefix=f"/{noun}s/candidates", tags=[f"{noun} review"])

    @router.get("", summary=f"List {noun} candidates (the review table)")
    async def list_candidates(
        session: SessionDep,
        _: Viewer,
        candidate_status: Annotated[CandidateStatus | None, Query(alias="status")] = "needs_review",
        city: Annotated[str | None, Query(description="City name, case-insensitive.")] = None,
        q: Annotated[str | None, Query(description="Part of the name.")] = None,
        limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> Page[CandidateRow]:
        rows, total = await repository.list_candidates(
            session, flow, status=candidate_status, city=city, q=q, limit=limit, offset=offset
        )
        today = datetime.now(UTC).date()
        return Page[CandidateRow](
            items=[candidate_row(row, flow, today) for row in rows],
            total=total,
            next_offset=offset + limit if offset + limit < total else None,
        )

    @router.get("/{candidate_id}", summary=f"The review packet of one {noun}: everything the review screen shows")
    async def get_candidate(
        candidate_id: UUID,
        session: SessionDep,
        _: Viewer,
        run_id: Annotated[
            UUID | None, Query(description="The run being walked through, for 'Candidate 1 of 9'.")
        ] = None,
    ) -> ReviewPacket:
        row = await repository.get_candidate(session, flow, candidate_id)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"no such {noun} candidate")
        run = position = None
        if run_id is not None:
            run = await repository.get_run(session, run_id)
            if run is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "no such run")
            position = _position(await repository.run_candidate_ids(session, run_id, flow), candidate_id)
        return review_packet(row, flow, run=run, position=position, now=datetime.now(UTC))

    async def decide(
        session: AsyncSession,
        candidate_id: UUID,
        admin_id: str,
        *,
        approve: bool,
        version: int,
        reason: str | None,
        note: str | None,
    ) -> DecisionOut:
        try:
            row = await repository.decide(
                session, flow, candidate_id, actor=admin_id, approve=approve, version=version, reason=reason, note=note
            )
        except repository.CandidateNotFoundError:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"no such {noun} candidate") from None
        except (repository.VersionConflictError, repository.AlreadyDecidedError) as error:
            raise _conflict(error) from None
        return DecisionOut(
            id=row.id,
            status=cast(CandidateStatus, row.status),
            version=row.version,
            reviewed_by=row.reviewed_by or admin_id,
            reviewed_at=row.reviewed_at or datetime.now(UTC),
            queued_for_publishing=row.status == "approved",
        )

    @router.post(
        "/{candidate_id}/approve",
        summary=f"Approve a {noun}",
        description="Marks it approved and queues it in the publishing outbox. Nothing is written to TravelGay.",
    )
    async def approve(candidate_id: UUID, body: ApproveIn, session: SessionDep, admin: Reviewer) -> DecisionOut:
        return await decide(
            session, candidate_id, admin.id, approve=True, version=body.version, reason=None, note=body.note
        )

    @router.post("/{candidate_id}/reject", summary=f"Reject a {noun}, with the reason")
    async def reject(candidate_id: UUID, body: RejectIn, session: SessionDep, admin: Reviewer) -> DecisionOut:
        return await decide(
            session, candidate_id, admin.id, approve=False, version=body.version, reason=body.reason, note=body.note
        )

    return router
