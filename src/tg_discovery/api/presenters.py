"""Turns stored candidates into the shapes the API returns: table rows and review packets."""

import re
from datetime import date, datetime, time
from typing import Literal, cast

from tg_discovery.api.schemas import (
    AiReview,
    CandidateRow,
    CandidateStatus,
    Contact,
    Dedupe,
    Description,
    EventBlock,
    FlowName,
    History,
    Image,
    Links,
    Occurrence,
    Place,
    Position,
    ReviewPacket,
    ReviewState,
    RunOut,
    RunRef,
    RunStatus,
    Scores,
    SourceOut,
    Tickets,
    VenueBlock,
    WhyRelevant,
)
from tg_discovery.domain.enums import Flow
from tg_discovery.domain.models import EventDraft, EventOccurrence, SourceRef, VenueDraft
from tg_discovery.domain.urls import registrable_domain
from tg_discovery.store.models import DiscoveryRun, EventCandidate, VenueCandidate

Candidate = EventCandidate | VenueCandidate

_TICKETING = frozenset(
    {"eventbrite.com", "ticketmaster.com", "dice.fm", "ra.co", "tixr.com", "posh.vip", "universe.com", "seetickets.com"}
)
_SOCIAL = frozenset({"facebook.com", "instagram.com", "x.com", "twitter.com", "tiktok.com"})
_FREE = re.compile(r"\b(?:free|no cover|gratis|complimentary)\b", re.IGNORECASE)
_PRICE = re.compile(r"[\d$€£¥฿]")


def source_label(url: str, provider: str) -> str:
    """Where a page comes from, in words a reviewer understands."""
    if provider == "osm_overpass":
        return "OpenStreetMap"
    if provider == "serpapi_maps" or "google.com/maps" in url:
        return "Google Maps"
    if provider == "serpapi_events" or url.startswith("https://www.google.com/search"):
        return "Google events results"
    domain = registrable_domain(url)
    if domain in _TICKETING:
        return "Ticketing platform"
    if domain in _SOCIAL:
        return "Social media"
    return domain or "Web page"


def ticket_kind(price_text: str | None) -> Literal["free", "paid", "unknown"]:
    """Free, paid or unknown, read from the price text; no text says nothing."""
    if not price_text:
        return "unknown"
    if _FREE.search(price_text):
        return "free"
    return "paid" if _PRICE.search(price_text) else "unknown"


def _clock(value: time | None) -> str | None:
    return value.strftime("%H:%M") if value else None


def _occurrence(item: EventOccurrence) -> Occurrence:
    return Occurrence(
        start_date=item.start_date,
        end_date=item.end_date,
        start_time=_clock(item.start_time),
        end_time=_clock(item.end_time),
        timezone=item.timezone,
        date_inferred=item.date_inferred,
    )


def _next(draft: EventDraft, today: date) -> EventOccurrence | None:
    ordered = sorted(draft.occurrences, key=lambda item: item.start_date)
    return next((item for item in ordered if item.last_date >= today), ordered[0] if ordered else None)


def _first_source(sources: list[SourceRef], providers: list[str]) -> str:
    if sources:
        return source_label(sources[0].url, sources[0].provider)
    return providers[0] if providers else "unknown"


def candidate_row(row: Candidate, flow: Flow, today: date) -> CandidateRow:
    next_date: date | None = None
    clock: str | None = None
    if flow is Flow.EVENT:
        event = EventDraft.model_validate(row.payload)
        upcoming = _next(event, today)
        if upcoming is not None:
            next_date, clock = upcoming.start_date, _clock(upcoming.start_time)
        address = event.venue.address or event.venue.name
        contact = Contact(name=event.organizer)
        label = _first_source(event.sources, row.found_by)
    else:
        venue = VenueDraft.model_validate(row.payload)
        address, contact, label = venue.address, Contact(phone=venue.phone), _first_source(venue.sources, row.found_by)
    return CandidateRow(
        id=row.id,
        flow=flow.value,
        name=row.name,
        status=cast(CandidateStatus, row.status),
        city=row.city,
        country=row.country_code,
        address=address,
        next_date=next_date,
        time=clock,
        contact=contact,
        source_label=label,
        found_by=list(row.found_by),
        score=float(row.score),
        fast_approve=row.fast_approve,
        safety_flag=row.safety_flag,
        version=row.version,
        first_seen_at=row.first_seen_at,
        last_seen_at=row.last_seen_at,
    )


def run_out(run: DiscoveryRun) -> RunOut:
    return RunOut(
        id=run.id,
        code=run.code,
        flow=cast(FlowName, run.flow),
        status=cast(RunStatus, run.status),
        filter=run.filter,
        stats=run.stats,
        cost_usd=float(run.cost_usd),
        error=run.error,
        requested_by=run.requested_by,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
        duration_s=(
            round((run.finished_at - run.started_at).total_seconds(), 1) if run.started_at and run.finished_at else None
        ),
    )


def _why(draft: EventDraft | VenueDraft) -> list[WhyRelevant]:
    reasons: list[WhyRelevant] = []
    providers = {source.url: source.provider for source in draft.sources}
    for item in draft.lgbtq.evidence if draft.lgbtq else []:
        provider = providers.get(item.url, "")
        reasons.append(
            WhyRelevant(
                text=item.quote, source_label=source_label(item.url, provider), source_url=item.url, quote=item.quote
            )
        )
    return reasons


def _sources(draft: EventDraft | VenueDraft) -> list[SourceOut]:
    return [
        SourceOut(
            url=source.url,
            label=source_label(source.url, source.provider),
            provider=source.provider,
            title=source.title,
            retrieved_at=source.retrieved_at,
        )
        for source in draft.sources
    ]


def _event_block(draft: EventDraft) -> EventBlock:
    venue = draft.venue
    return EventBlock(
        name=draft.name,
        event_type=draft.event_type.value,
        recurring_type=draft.recurring_type.value,
        audience=draft.audience.value,
        occurrences=[_occurrence(item) for item in draft.occurrences],
        venue=Place(
            name=venue.name, address=venue.address, city=venue.city, country=venue.country, lat=venue.lat, lng=venue.lng
        ),
        organizer=Contact(name=draft.organizer),
        tickets=Tickets(kind=ticket_kind(draft.price_text), price_text=draft.price_text, url=draft.links.tickets),
        links=Links(
            official=draft.links.official,
            tickets=draft.links.tickets,
            facebook=draft.links.facebook,
            instagram=draft.links.instagram,
        ),
        description=Description(text=draft.description),
        images=[Image(url=item.url, source_url=item.source_url, alt=item.alt) for item in draft.images],
    )


def _venue_block(draft: VenueDraft) -> VenueBlock:
    return VenueBlock(
        name=draft.name,
        venue_type=draft.venue_type,
        address=draft.address,
        city=draft.city,
        country=draft.country,
        lat=draft.lat,
        lng=draft.lng,
        website=draft.website,
        phone=draft.phone,
        socials=draft.socials,
        opening_hours=draft.opening_hours,
        price_text=draft.price_text,
        operating_status=draft.operating_status.value,
        description=Description(text=draft.description),
    )


def review_packet(
    row: Candidate, flow: Flow, *, run: DiscoveryRun | None, position: Position | None, now: datetime
) -> ReviewPacket:
    draft: EventDraft | VenueDraft = (
        EventDraft.model_validate(row.payload) if flow is Flow.EVENT else VenueDraft.model_validate(row.payload)
    )
    scores = row.scores
    return ReviewPacket(
        flow=flow.value,
        id=row.id,
        version=row.version,
        status=cast(CandidateStatus, row.status),
        run=RunRef(id=run.id, code=run.code) if run else None,
        position=position,
        event=_event_block(draft) if isinstance(draft, EventDraft) else None,
        venue=_venue_block(draft) if isinstance(draft, VenueDraft) else None,
        lgbtq_level=draft.lgbtq.level.value if draft.lgbtq else None,
        why_relevant=_why(draft),
        ai_review=AiReview(cons=cast(list[str], scores.get("concerns", []))),
        scores=Scores(total=float(row.score), parts=cast(dict[str, float], scores.get("parts", {}))),
        fast_approve=row.fast_approve,
        safety_flag=row.safety_flag,
        sources=_sources(draft),
        dedupe=Dedupe(),
        history=History(first_seen_at=row.first_seen_at, last_seen_at=row.last_seen_at, seen_count=row.seen_count),
        missing_fields=draft.missing_fields,
        review=ReviewState(
            reviewed_by=row.reviewed_by,
            reviewed_at=row.reviewed_at,
            note=row.review_note,
            can=["approve", "reject"] if row.status == "needs_review" else [],
        ),
    )
