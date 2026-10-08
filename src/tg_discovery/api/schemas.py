"""Request and response bodies of the API: what the frontend sends and reads (the contract Swagger shows)."""

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from tg_discovery.pipelines.discovery import RunFilter

FlowName = Literal["venue", "event"]
CandidateStatus = Literal["needs_review", "approved", "rejected"]
RunStatus = Literal["queued", "running", "succeeded", "partial", "failed"]
RejectReason = Literal["not_lgbtq", "wrong_place", "past_or_cancelled", "duplicate", "spam", "other"]


# -- runs ----------------------------------------------------------------------------------------------------


class RunRequest(RunFilter):
    """Start a discovery run for some cities of one country."""

    force: bool = Field(default=False, description="Run again even when the same request finished recently.")


class RunAccepted(BaseModel):
    run_id: UUID
    code: str = Field(description="Human-readable run code such as DSC-20261008-001.")
    status: RunStatus
    deduplicated: bool = Field(
        description="True when an identical run was already running or finished recently: no new run was started."
    )


class RunOut(BaseModel):
    id: UUID
    code: str
    flow: FlowName
    status: RunStatus
    filter: dict[str, object]
    stats: dict[str, object] = Field(description="Progress, calls made, candidates found and what was dropped.")
    cost_usd: float
    error: str | None
    requested_by: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class Page[T](BaseModel):
    items: list[T]
    total: int
    next_offset: int | None = Field(description="Pass this as ``offset`` to get the next page; null on the last one.")


# -- the review table ----------------------------------------------------------------------------------------


class Contact(BaseModel):
    name: str | None = None
    phone: str | None = None
    email: str | None = None


class CandidateRow(BaseModel):
    """One line of the results table."""

    id: UUID
    flow: FlowName
    name: str
    status: CandidateStatus
    city: str
    country: str
    address: str | None
    next_date: date | None = Field(description="Events: the first upcoming date.")
    time: str | None = Field(description="Events: start time of that date, HH:MM.")
    contact: Contact
    source_label: str = Field(description="Where it was found, in words: 'Organiser site', 'OpenStreetMap'...")
    found_by: list[str]
    score: float
    fast_approve: bool = Field(description="Only a label for an easy approve: nothing is approved automatically.")
    safety_flag: str | None
    version: int = Field(description="Send it back when approving or rejecting; a 409 means somebody else was first.")
    first_seen_at: datetime
    last_seen_at: datetime


# -- the review packet ---------------------------------------------------------------------------------------


class RunRef(BaseModel):
    id: UUID
    code: str


class Position(BaseModel):
    """Where the candidate sits in the run's results: the "Candidate 1 of 9" of the review screen."""

    index: int = Field(description="1-based.")
    total: int
    prev_id: UUID | None
    next_id: UUID | None


class Occurrence(BaseModel):
    start_date: date
    end_date: date | None
    start_time: str | None
    end_time: str | None
    timezone: str | None
    date_inferred: bool = Field(description="True when the year was not stated and was filled in.")


class Place(BaseModel):
    name: str | None = None
    address: str | None = None
    city: str | None = None
    country: str | None = None
    lat: float | None = None
    lng: float | None = None


class Tickets(BaseModel):
    kind: Literal["free", "paid", "unknown"]
    price_text: str | None
    note: str | None = None
    url: str | None


class Links(BaseModel):
    official: str | None = None
    tickets: str | None = None
    facebook: str | None = None
    instagram: str | None = None


class Description(BaseModel):
    text: str | None
    origin: Literal["ai_draft", "admin_edited"] = "ai_draft"


class Image(BaseModel):
    url: str
    kind: str | None = Field(default=None, description="poster, map, crowd, stage, flyer...")
    source_url: str | None = None


class EventBlock(BaseModel):
    name: str
    event_type: str
    category: str | None = Field(default=None, description="Finer type such as 'Pride march'. Not collected yet.")
    recurring_type: str
    audience: str
    occurrences: list[Occurrence]
    venue: Place
    organizer: Contact = Field(description="Only what the sources state; never a private individual's details.")
    tickets: Tickets
    links: Links
    expected_attendance: int | None = Field(default=None, description="Not collected yet.")
    topics: list[str] = Field(default_factory=list[str], description="Not collected yet.")
    description: Description
    images: list[Image] = Field(default_factory=list[Image], description="Source URLs only; not collected yet.")


class VenueBlock(BaseModel):
    name: str
    venue_type: str | None
    address: str | None
    city: str | None
    country: str | None
    lat: float | None
    lng: float | None
    website: str | None
    phone: str | None
    socials: dict[str, str]
    opening_hours: str | None
    operating_status: str
    description: Description
    images: list[Image] = Field(default_factory=list[Image], description="Source URLs only; not collected yet.")


class WhyRelevant(BaseModel):
    """One reason the candidate is LGBTQ+ relevant, with the page it was read on."""

    text: str
    source_label: str
    source_url: str
    published_at: str | None = None
    quote: str


class AiReview(BaseModel):
    verdict: str | None = Field(default=None, description="No model judges candidates yet.")
    cons: list[str] = Field(description="What a reviewer should look at twice.")
    open_questions: list[str] = Field(default_factory=list[str])


class Scores(BaseModel):
    total: float
    parts: dict[str, float]


class SourceOut(BaseModel):
    url: str
    label: str
    provider: str
    title: str | None
    retrieved_at: datetime


class Dedupe(BaseModel):
    tg_match: str | None = Field(
        default=None, description="The matching TravelGay record, once the known set is wired."
    )
    similar: list[UUID] = Field(default_factory=list[UUID])


class History(BaseModel):
    first_seen_at: datetime
    last_seen_at: datetime
    seen_count: int
    changes: list[str] = Field(default_factory=list[str])


class ReviewState(BaseModel):
    locked_by: str | None = None
    reviewed_by: str | None
    reviewed_at: datetime | None
    note: str | None
    can: list[Literal["approve", "reject"]] = Field(description="What the reviewer may do now.")


class ReviewPacket(BaseModel):
    """Everything the review screen shows for one candidate."""

    flow: FlowName
    id: UUID
    version: int
    status: CandidateStatus
    run: RunRef | None = Field(description="The run the reviewer came from, when ``run_id`` was given.")
    position: Position | None
    event: EventBlock | None = None
    venue: VenueBlock | None = None
    lgbtq_level: str | None
    why_relevant: list[WhyRelevant]
    ai_review: AiReview
    scores: Scores
    fast_approve: bool
    safety_flag: str | None
    sources: list[SourceOut]
    dedupe: Dedupe
    history: History
    missing_fields: list[str]
    review: ReviewState


# -- decisions -----------------------------------------------------------------------------------------------


class ApproveIn(BaseModel):
    version: int = Field(description="The ``version`` of the candidate you reviewed.")
    note: str | None = Field(default=None, max_length=1000)


class RejectIn(BaseModel):
    version: int = Field(description="The ``version`` of the candidate you reviewed.")
    reason: RejectReason
    note: str | None = Field(default=None, max_length=1000)


class DecisionOut(BaseModel):
    id: UUID
    status: CandidateStatus
    version: int
    reviewed_by: str
    reviewed_at: datetime
    queued_for_publishing: bool = Field(
        description="True after an approval: a row waits in the outbox. Nothing is published."
    )
