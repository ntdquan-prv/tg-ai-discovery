"""Scores and triage for merged drafts: a number, a decision and the reasons for doubt.

A candidate is scored 0 to 1 from weighted parts. Hard gates reject outright (closed for good, outside the area,
cancelled, already over, and for events not LGBTQ+). Below ``REJECT_BELOW`` a candidate is dropped as junk; everything
else goes to a human, and at ``FAST_APPROVE_FROM`` or more it is marked as an easy approve (a label, never an automatic
approval).

Venues are not limited to LGBTQ+ places: their LGBTQ+ level is shown to the reviewer but neither scores nor rejects a
venue. Events must be LGBTQ+.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

from tg_discovery.domain.enums import EventStatus, LgbtqLevel, OperatingStatus
from tg_discovery.domain.geo import BoundingBox
from tg_discovery.domain.models import EventDraft, LgbtqAssessment, VenueDraft

REJECT_BELOW = 0.35
FAST_APPROVE_FROM = 0.75
MARGIN_RATIO = 0.1  # a venue just outside the search box is still in the city

VENUE_WEIGHTS = {
    "existence": 0.30,
    "location_match": 0.20,
    "completeness": 0.20,
    "source_trust": 0.15,
    "verifiability": 0.15,
}
EVENT_WEIGHTS = {
    "time_validity": 0.25,
    "lgbtq_relevance": 0.25,
    "location_match": 0.15,
    "verifiability": 0.15,
    "completeness": 0.10,
    "source_trust": 0.10,
}
# How far each provider's facts are trusted before anything is verified; starting values, to be tuned from the
# admins' approve and reject decisions.
PROVIDER_TRUST = {"osm_overpass": 0.7, "serpapi_maps": 0.7, "serpapi_events": 0.6, "exa": 0.5}
DEFAULT_TRUST = 0.5
FIELD_COUNT = 8  # required plus optional fields of a venue or an event draft

Decision = Literal["reject", "needs_review"]


@dataclass(frozen=True, slots=True)
class Scored:
    """What the scoring concluded about one merged draft."""

    total: float
    parts: dict[str, float]
    decision: Decision
    reason: str | None  # why it was rejected
    fast_approve: bool
    concerns: list[str]  # what a reviewer should look at twice


def _lgbtq(assessment: LgbtqAssessment | None) -> float:
    if assessment is None:
        return 0.3
    evidenced = bool(assessment.evidence)
    match assessment.level:
        case LgbtqLevel.PRIMARY:
            return 1.0 if evidenced else 0.75
        case LgbtqLevel.FRIENDLY:
            return 0.7 if evidenced else 0.5
        case LgbtqLevel.UNCLEAR:
            return 0.2  # a page that does not say is worth less than no assessment at all (0.3)
        case LgbtqLevel.NO:
            return 0.0


def _trust(providers: Sequence[str]) -> float:
    best = max((PROVIDER_TRUST.get(name, DEFAULT_TRUST) for name in providers), default=DEFAULT_TRUST)
    return min(best + (0.1 if len(set(providers)) > 1 else 0.0), 1.0)


def _verifiability(providers: Sequence[str], *, has_own_page: bool) -> float:
    if len(set(providers)) > 1:
        return 1.0
    return 0.6 if has_own_page else 0.3


def _completeness(missing: Sequence[str]) -> float:
    return 1.0 - len(missing) / FIELD_COUNT


def _place(lat: float | None, lng: float | None, area: BoundingBox | None, *, known_text: bool) -> tuple[float, bool]:
    """How well a place fits the city, and whether it lies outside the search area."""
    if lat is not None and lng is not None and area is not None:
        inside = area.contains(lat, lng, margin_ratio=MARGIN_RATIO)
        return (1.0, False) if inside else (0.0, True)
    return (0.7 if known_text else 0.4), False


def _finish(weights: dict[str, float], parts: dict[str, float], concerns: list[str], gate: str | None) -> Scored:
    total = round(sum(weights[name] * parts[name] for name in weights), 3)
    if gate is not None:
        return Scored(total, parts, "reject", gate, False, concerns)
    if total < REJECT_BELOW:
        return Scored(total, parts, "reject", "low_score", False, concerns)
    return Scored(total, parts, "needs_review", None, total >= FAST_APPROVE_FROM, concerns)


def score_venue(draft: VenueDraft, providers: Sequence[str], *, area: BoundingBox | None) -> Scored:
    """Score a merged venue; ``area`` is the search box of its city, when known."""
    concerns: list[str] = []
    status = draft.operating_status
    existence = {
        OperatingStatus.OPEN: 1.0,
        OperatingStatus.UNKNOWN: 0.6,
        OperatingStatus.TEMPORARILY_CLOSED: 0.3,
        OperatingStatus.PERMANENTLY_CLOSED: 0.0,
    }[status]
    location, outside = _place(draft.lat, draft.lng, area, known_text=bool(draft.address))
    parts = {
        "existence": existence,
        "location_match": location,
        "completeness": _completeness(draft.missing_fields),
        "source_trust": _trust(providers),
        "verifiability": _verifiability(providers, has_own_page=bool(draft.website)),
    }
    if status is OperatingStatus.UNKNOWN:
        concerns.append("It is not known whether the venue is still open.")
    if status is OperatingStatus.TEMPORARILY_CLOSED:
        concerns.append("The venue is reported as temporarily closed.")
    if len(set(providers)) < 2:
        concerns.append("Only one source lists it.")
    if not draft.website:
        concerns.append("No website to check it against.")
    if not draft.has_coordinates:
        concerns.append("No coordinates, so its position was not checked.")
    gate = _venue_gate(draft, outside=outside)
    return _finish(VENUE_WEIGHTS, parts, concerns, gate)


def _venue_gate(draft: VenueDraft, *, outside: bool) -> str | None:
    if draft.operating_status is OperatingStatus.PERMANENTLY_CLOSED:
        return "closed"
    return "outside_area" if outside else None


def _time_validity(draft: EventDraft, today: date, window_days: int) -> tuple[float, bool]:
    """How well the dates fit the window, and whether every date is already over."""
    if not draft.occurrences:
        return 0.0, False
    upcoming = [occurrence for occurrence in draft.occurrences if occurrence.last_date >= today]
    if not upcoming:
        return 0.0, True
    next_one = min(upcoming, key=lambda occurrence: occurrence.start_date)
    if next_one.start_date > today + timedelta(days=window_days):
        return 0.4, False
    return (0.5 if next_one.date_inferred else 1.0), False


def score_event(
    draft: EventDraft, providers: Sequence[str], *, today: date, window_days: int, area: BoundingBox | None
) -> Scored:
    """Score a merged event; ``area`` is the search box of its city, when known."""
    venue = draft.venue
    time_validity, over = _time_validity(draft, today, window_days)
    location, outside = _place(venue.lat, venue.lng, area, known_text=bool(venue.address or venue.name))
    own_page = bool(draft.links.official or draft.links.tickets)
    parts = {
        "time_validity": time_validity,
        "lgbtq_relevance": _lgbtq(draft.lgbtq),
        "location_match": location,
        "verifiability": _verifiability(providers, has_own_page=own_page),
        "completeness": _completeness(draft.missing_fields),
        "source_trust": _trust(providers),
    }
    concerns: list[str] = []
    if not draft.occurrences:
        concerns.append("No date was found.")
    elif any(occurrence.date_inferred for occurrence in draft.occurrences):
        concerns.append("The year was not stated, so the date was inferred.")
    if not own_page:
        concerns.append("No official or ticket page to check it against.")
    if len(set(providers)) < 2:
        concerns.append("Only one source lists it.")
    if draft.lgbtq is None or not draft.lgbtq.evidence:
        concerns.append("Nothing quoted from a page supports the LGBTQ+ level.")
    if not (venue.address or venue.name):
        concerns.append("No venue or address.")
    gate = _event_gate(draft, over=over, outside=outside)
    return _finish(EVENT_WEIGHTS, parts, concerns, gate)


def _event_gate(draft: EventDraft, *, over: bool, outside: bool) -> str | None:
    if draft.lgbtq is not None and draft.lgbtq.level is LgbtqLevel.NO:
        return "not_lgbtq"
    if draft.status is EventStatus.CANCELLED:
        return "cancelled"
    if over:
        return "expired"
    return "outside_area" if outside else None
