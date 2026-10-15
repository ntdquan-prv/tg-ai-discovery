"""Merging the drafts that several providers made of the same venue or event into one candidate.

Grouping follows the identity rules of ``domain.fingerprint``: two venue drafts are one venue when any of their keys
match (a draft without coordinates also joins a draft with the same normalised name), two event drafts are one event
when their fingerprints match or the fuzzy rule holds (similar core title and the same venue or overlapping dates).
Merging keeps the most complete draft and fills its gaps from the others; a fact is never invented.
"""

import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable, Sequence

from tg_discovery.domain.enums import Audience, EventStatus, EventType, LgbtqLevel, OperatingStatus, RecurringType
from tg_discovery.domain.fingerprint import core_title, event_fingerprint, events_match, normalize_name, venue_keys
from tg_discovery.domain.models import (
    EventDraft,
    EventLinks,
    EventOccurrence,
    EventVenue,
    Evidence,
    LgbtqAssessment,
    SourceRef,
    VenueDraft,
    union_images,
)
from tg_discovery.domain.urls import website_key

MAX_EVIDENCE = 2
MAX_SOURCES = 10

Entry = tuple[str, VenueDraft]
EventEntry = tuple[str, EventDraft]


class _Groups:
    """Union-find over the positions of a list."""

    def __init__(self, size: int) -> None:
        self._parent = list(range(size))

    def find(self, index: int) -> int:
        while self._parent[index] != index:
            self._parent[index] = self._parent[self._parent[index]]
            index = self._parent[index]
        return index

    def union(self, a: int, b: int) -> None:
        self._parent[self.find(a)] = self.find(b)

    def collect[T](self, items: Sequence[T]) -> list[list[T]]:
        groups: dict[int, list[T]] = defaultdict(list)
        for index, item in enumerate(items):
            groups[self.find(index)].append(item)
        return list(groups.values())


def group_venues(entries: Sequence[Entry], scope: str) -> list[list[Entry]]:
    """Group venue drafts of one city; ``scope`` is the country and city they were found in."""
    groups = _Groups(len(entries))
    owner: dict[str, int] = {}
    by_name: dict[str, list[int]] = defaultdict(list)
    for index, (_, draft) in enumerate(entries):
        for key in venue_keys(draft, scope):
            if key in owner:
                groups.union(index, owner[key])
            else:
                owner[key] = index
        by_name[normalize_name(draft.name)].append(index)
    for indexes in by_name.values():
        # A draft with no position cannot be placed by its coordinates, so its name is all there is to go on.
        if any(not entries[index][1].has_coordinates for index in indexes):
            for other in indexes[1:]:
                groups.union(indexes[0], other)
    return groups.collect(entries)


def group_events(entries: Sequence[EventEntry], scope: str) -> list[list[EventEntry]]:
    """Group event drafts of one city by fingerprint, then by the fuzzy rule."""
    groups = _Groups(len(entries))
    owner: dict[str, int] = {}
    for index, (_, draft) in enumerate(entries):
        key = event_fingerprint(draft, scope)
        if key in owner:
            groups.union(index, owner[key])
        else:
            owner[key] = index
    for first in range(len(entries)):
        for second in range(first + 1, len(entries)):
            if groups.find(first) != groups.find(second) and events_match(entries[first][1], entries[second][1]):
                groups.union(first, second)
    return groups.collect(entries)


def _first[T](values: Iterable[T | None]) -> T | None:
    return next((value for value in values if value), None)


def _evidence(assessments: Iterable[LgbtqAssessment]) -> list[Evidence]:
    seen: set[tuple[str, str]] = set()
    kept: list[Evidence] = []
    for assessment in assessments:
        for item in assessment.evidence:
            if (item.quote, item.url) not in seen and len(kept) < MAX_EVIDENCE:
                seen.add((item.quote, item.url))
                kept.append(item)
    return kept


_LEVEL_RANK = {LgbtqLevel.PRIMARY: 3, LgbtqLevel.FRIENDLY: 2, LgbtqLevel.UNCLEAR: 1, LgbtqLevel.NO: 0}


def _merge_lgbtq(assessments: Iterable[LgbtqAssessment | None]) -> LgbtqAssessment | None:
    present = [assessment for assessment in assessments if assessment is not None]
    if not present:
        return None
    # A "no" that is backed by a quote outweighs any guess; otherwise the strongest level wins.
    denied = [assessment for assessment in present if assessment.level is LgbtqLevel.NO and assessment.evidence]
    if denied:
        return LgbtqAssessment(level=LgbtqLevel.NO, evidence=_evidence(denied))
    others = [assessment for assessment in present if assessment.level is not LgbtqLevel.NO] or present
    level = max((assessment.level for assessment in others), key=lambda item: _LEVEL_RANK[item])
    return LgbtqAssessment(level=level, evidence=_evidence(others))


def _sources(drafts: Iterable[VenueDraft | EventDraft]) -> list[SourceRef]:
    seen: set[str] = set()
    kept: list[SourceRef] = []
    for draft in drafts:
        for source in draft.sources:
            if source.url not in seen and len(kept) < MAX_SOURCES:
                seen.add(source.url)
                kept.append(source)
    return kept


def _venue_status(statuses: Iterable[OperatingStatus]) -> OperatingStatus:
    found = set(statuses)
    for wanted in (
        OperatingStatus.PERMANENTLY_CLOSED,
        OperatingStatus.TEMPORARILY_CLOSED,
        OperatingStatus.OPEN,
    ):
        if wanted in found:
            return wanted
    return OperatingStatus.UNKNOWN


def _filled(draft: VenueDraft | EventDraft) -> int:
    return 8 - len(draft.missing_fields)


def merge_venue(entries: Sequence[Entry]) -> VenueDraft:
    """One venue from the drafts of the same place: the most complete draft, its gaps filled from the others."""
    drafts = [draft for _, draft in sorted(entries, key=lambda entry: (-_filled(entry[1]), entry[0]))]
    base = drafts[0]
    located = next((draft for draft in drafts if draft.has_coordinates), None)
    socials = {key: value for draft in reversed(drafts) for key, value in draft.socials.items()}
    external_ids = {key: value for draft in reversed(drafts) for key, value in draft.external_ids.items()}
    return base.model_copy(
        update={
            "venue_type": _first(draft.venue_type for draft in drafts),
            "address": _first(draft.address for draft in drafts),
            "city": _first(draft.city for draft in drafts),
            "country": _first(draft.country for draft in drafts),
            "lat": located.lat if located else None,
            "lng": located.lng if located else None,
            "website": _first(draft.website for draft in drafts),
            "phone": _first(draft.phone for draft in drafts),
            "opening_hours": _first(draft.opening_hours for draft in drafts),
            "price_text": _first(draft.price_text for draft in drafts),
            "description": _first(draft.description for draft in drafts),
            "socials": socials,
            "external_ids": external_ids,
            "lgbtq": _merge_lgbtq(draft.lgbtq for draft in drafts),
            "operating_status": _venue_status(draft.operating_status for draft in drafts),
            "sources": _sources(drafts),
        }
    )


def _occurrences(drafts: Iterable[EventDraft]) -> list[EventOccurrence]:
    seen: set[tuple[object, ...]] = set()
    kept: list[EventOccurrence] = []
    for draft in drafts:
        for occurrence in draft.occurrences:
            key = (occurrence.start_date, occurrence.start_time, occurrence.end_date)
            if key not in seen:
                seen.add(key)
                kept.append(occurrence)
    return sorted(kept, key=lambda occurrence: (occurrence.start_date, occurrence.start_time is None))


def _event_venue(drafts: Sequence[EventDraft]) -> EventVenue:
    venues = [draft.venue for draft in drafts]
    located = next((venue for venue in venues if venue.lat is not None and venue.lng is not None), None)
    return EventVenue(
        name=_first(venue.name for venue in venues),
        address=_first(venue.address for venue in venues),
        city=_first(venue.city for venue in venues),
        country=_first(venue.country for venue in venues),
        lat=located.lat if located else None,
        lng=located.lng if located else None,
        google_place_id=_first(venue.google_place_id for venue in venues),
    )


def merge_event(entries: Sequence[EventEntry]) -> EventDraft:
    """One event from the drafts of the same event: the most complete draft, its gaps filled from the others."""
    drafts = [draft for _, draft in sorted(entries, key=lambda entry: (-_filled(entry[1]), entry[0]))]
    base = drafts[0]
    types = {draft.event_type for draft in drafts}
    event_type = next(
        (wanted for wanted in (EventType.PRIDE, EventType.VENUE_EVENT) if wanted in types), base.event_type
    )
    recurring = _first(draft.recurring_type for draft in drafts if draft.recurring_type is not RecurringType.ONEDAY)
    audience = _first(draft.audience for draft in drafts if draft.audience is not Audience.UNKNOWN)
    cancelled = any(draft.status is EventStatus.CANCELLED for draft in drafts)
    known_status = _first(draft.status for draft in drafts if draft.status is not EventStatus.UNKNOWN)
    return base.model_copy(
        update={
            "event_type": event_type,
            "recurring_type": recurring or base.recurring_type,
            "occurrences": _occurrences(drafts),
            "venue": _event_venue(drafts),
            "organizer": _first(draft.organizer for draft in drafts),
            "audience": audience or Audience.UNKNOWN,
            "links": EventLinks(
                official=_first(draft.links.official for draft in drafts),
                tickets=_first(draft.links.tickets for draft in drafts),
                facebook=_first(draft.links.facebook for draft in drafts),
                instagram=_first(draft.links.instagram for draft in drafts),
            ),
            "price_text": _first(draft.price_text for draft in drafts),
            "description": _first(draft.description for draft in drafts),
            "lgbtq": _merge_lgbtq(draft.lgbtq for draft in drafts),
            "status": EventStatus.CANCELLED if cancelled else (known_status or EventStatus.UNKNOWN),
            "images": union_images(*(draft.images for draft in drafts)),  # the most complete draft's pictures first
            "sources": _sources(drafts),
        }
    )


def _digest(parts: Sequence[object]) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


def venue_content_hash(draft: VenueDraft) -> str:
    """Hash of the material fields; the description is left out so that rephrasing never counts as a change."""
    lat = round(draft.lat, 4) if draft.lat is not None else None
    lng = round(draft.lng, 4) if draft.lng is not None else None
    parts = [normalize_name(draft.name), (draft.address or "").casefold(), lat, lng, website_key(draft.website)]
    return _digest([*parts, draft.operating_status.value])


def event_content_hash(draft: EventDraft) -> str:
    """Hash of the material fields; the description is left out so that rephrasing never counts as a change."""
    dates = [(item.start_date.isoformat(), str(item.start_time), str(item.end_date)) for item in draft.occurrences]
    venue = [(draft.venue.name or "").casefold(), (draft.venue.address or "").casefold()]
    return _digest([core_title(draft.name), dates, venue, draft.links.official, draft.status.value])
