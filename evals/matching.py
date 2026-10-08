"""Tolerant matching used to compare providers: golden records against drafts and drafts against each other.

This is evaluation logic. It is deliberately more forgiving than the strict identity keys in ``domain.fingerprint`` that
the production dedupe relies on: a provider that returns "Heaven Bar" must be credited for TravelGay's "Heaven", and the
same place found by three providers must count once.
"""

import re
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass

from evals.models import CitySpec, DraftRow, GoldenEvent, GoldenVenue
from tg_discovery.domain.enums import Flow
from tg_discovery.domain.fingerprint import (
    core_title,
    event_fingerprint,
    events_match,
    normalize_name,
    similarity,
    venue_keys,
)
from tg_discovery.domain.geo import haversine_m
from tg_discovery.domain.models import EventDraft, VenueDraft
from tg_discovery.domain.urls import website_key

NAME_MATCH = 0.80
NAME_MATCH_WITHOUT_COORDINATES = 0.92
NEAR_METRES = 250
FAR_METRES = 1_500
LENIENT_TITLE_MATCH = 0.95
MAX_BLOCK_SIZE = 80  # a token shared by more drafts than this says nothing about identity


def _digits(name: str) -> str:
    return " ".join(re.findall(r"\d+", name))


def same_venue(a: VenueDraft, b: VenueDraft) -> bool:
    """Whether two drafts describe the same place (both come from one city)."""
    if any(a.external_ids.get(key) and a.external_ids.get(key) == b.external_ids.get(key) for key in b.external_ids):
        return True
    distance = (
        haversine_m(a.lat or 0, a.lng or 0, b.lat or 0, b.lng or 0) if a.has_coordinates and b.has_coordinates else None
    )
    if distance is not None and distance > FAR_METRES:
        return False
    site = website_key(a.website)
    if site is not None and site == website_key(b.website):
        return True
    if _digits(a.name) != _digits(b.name):
        return False  # "Sauna 24" and "Sauna 25" are two places however alike the names look
    score = similarity(normalize_name(a.name), normalize_name(b.name))
    if distance is not None:
        return distance <= NEAR_METRES and score >= NAME_MATCH
    return score >= NAME_MATCH_WITHOUT_COORDINATES


def same_event(a: EventDraft, b: EventDraft) -> bool:
    """The production merge rule, relaxed for drafts that carry no dates at all."""
    if events_match(a, b):
        return True
    undated = not a.occurrences or not b.occurrences
    if not undated or similarity(core_title(a.name), core_title(b.name)) < LENIENT_TITLE_MATCH:
        return False
    if a.venue.name and b.venue.name:
        return similarity(normalize_name(a.venue.name), normalize_name(b.venue.name)) >= 0.5
    return True


class _Clusters:
    """Union-find that never merges two clusters that each contain a golden record: golden records are distinct."""

    def __init__(self, golden: Sequence[bool]) -> None:
        self._parent = list(range(len(golden)))
        self._has_golden = list(golden)

    def find(self, index: int) -> int:
        while self._parent[index] != index:
            self._parent[index] = self._parent[self._parent[index]]
            index = self._parent[index]
        return index

    def union(self, a: int, b: int) -> None:
        root_a, root_b = self.find(a), self.find(b)
        if root_a == root_b or (self._has_golden[root_a] and self._has_golden[root_b]):
            return
        self._parent[root_b] = root_a
        self._has_golden[root_a] = self._has_golden[root_a] or self._has_golden[root_b]

    def groups(self) -> list[list[int]]:
        grouped: dict[int, list[int]] = defaultdict(list)
        for index in range(len(self._parent)):
            grouped[self.find(index)].append(index)
        return sorted(grouped.values(), key=lambda members: members[0])


def _cluster[D](
    items: Sequence[D],
    golden: Sequence[bool],
    exact_keys: Callable[[D], Iterable[str]],
    tokens: Callable[[D], set[str]],
    same: Callable[[D, D], bool],
) -> list[list[int]]:
    clusters = _Clusters(golden)
    first_with_key: dict[str, int] = {}
    for index, item in enumerate(items):
        for key in exact_keys(item):
            if key in first_with_key:
                clusters.union(first_with_key[key], index)
            else:
                first_with_key[key] = index
    postings: dict[str, list[int]] = defaultdict(list)
    for index, item in enumerate(items):
        for token in tokens(item):
            postings[token].append(index)
    compared: set[tuple[int, int]] = set()
    for members in postings.values():
        if len(members) > MAX_BLOCK_SIZE:
            continue
        for position, i in enumerate(members):
            for j in members[position + 1 :]:
                if (i, j) in compared or clusters.find(i) == clusters.find(j):
                    continue
                compared.add((i, j))
                if same(items[i], items[j]):
                    clusters.union(i, j)
    return clusters.groups()


def cluster_venues(drafts: Sequence[VenueDraft], golden: Sequence[bool], scope: str) -> list[list[int]]:
    """Group drafts that describe the same venue; ``golden[i]`` marks TravelGay's own records."""
    return _cluster(
        drafts,
        golden,
        lambda draft: venue_keys(draft, scope),
        lambda draft: {token for token in normalize_name(draft.name).split() if len(token) > 1},
        same_venue,
    )


def cluster_events(drafts: Sequence[EventDraft], golden: Sequence[bool], scope: str) -> list[list[int]]:
    """Group drafts that describe the same event; recurring events are one cluster."""
    return _cluster(
        drafts,
        golden,
        lambda draft: [event_fingerprint(draft, scope)],
        lambda draft: {token for token in core_title(draft.name).split() if len(token) > 2},
        same_event,
    )


@dataclass(frozen=True, slots=True)
class Entity:
    """One real-world venue or event as seen by one or more providers (and, maybe, by TravelGay)."""

    id: str
    flow: Flow
    city: str
    rows: tuple[int, ...]  # indexes into the draft rows of the run
    providers: frozenset[str]
    golden_id: str | None
    representative: VenueDraft | EventDraft | None  # the most complete provider draft; None when only golden

    @property
    def is_novel(self) -> bool:
        """Found by at least one provider and unknown to TravelGay."""
        return bool(self.providers) and self.golden_id is None


def _completeness(draft: VenueDraft | EventDraft) -> int:
    return -len(draft.missing_fields)


def resolve_entities(
    rows: Sequence[DraftRow],
    cities: Sequence[CitySpec],
    golden_venues: Mapping[str, Sequence[GoldenVenue]],
    golden_events: Mapping[str, Sequence[GoldenEvent]],
) -> list[Entity]:
    """Cluster every draft of the run, together with TravelGay's golden records, into entities per flow and city."""
    entities: list[Entity] = []
    for city in cities:
        for flow in Flow:
            indexed = [(index, row) for index, row in enumerate(rows) if row.flow is flow and row.city == city.slug]
            if flow is Flow.VENUE:
                entities += _venue_entities(city.slug, indexed, golden_venues.get(city.slug, ()))
            else:
                entities += _event_entities(city.slug, indexed, golden_events.get(city.slug, ()))
    return entities


def _venue_entities(city: str, indexed: Sequence[tuple[int, DraftRow]], golden: Sequence[GoldenVenue]) -> list[Entity]:
    provider_rows = [(index, row) for index, row in indexed if row.venue is not None]
    drafts = [row.venue for _, row in provider_rows if row.venue is not None]
    everything = [*drafts, *(item.to_draft() for item in golden)]
    flags = [False] * len(drafts) + [True] * len(golden)
    groups = cluster_venues(everything, flags, city)
    return _entities(Flow.VENUE, city, groups, provider_rows, [item.id for item in golden], everything)


def _event_entities(city: str, indexed: Sequence[tuple[int, DraftRow]], golden: Sequence[GoldenEvent]) -> list[Entity]:
    provider_rows = [(index, row) for index, row in indexed if row.event is not None]
    drafts = [row.event for _, row in provider_rows if row.event is not None]
    everything = [*drafts, *(item.to_draft() for item in golden)]
    flags = [False] * len(drafts) + [True] * len(golden)
    groups = cluster_events(everything, flags, city)
    return _entities(Flow.EVENT, city, groups, provider_rows, [item.id for item in golden], everything)


def _entities(
    flow: Flow,
    city: str,
    groups: Sequence[Sequence[int]],
    provider_rows: Sequence[tuple[int, DraftRow]],
    golden_ids: Sequence[str],
    drafts: Sequence[VenueDraft | EventDraft],
) -> list[Entity]:
    """Turn clusters into entities. Members ``0..n-1`` are provider drafts and the rest are golden records."""
    entities: list[Entity] = []
    provider_count = len(provider_rows)
    for number, group in enumerate(groups, start=1):
        from_providers = [member for member in group if member < provider_count]
        known = [golden_ids[member - provider_count] for member in group if member >= provider_count]
        candidates = [drafts[member] for member in from_providers]
        entities.append(
            Entity(
                id=f"{flow.value}:{city}:{number}",
                flow=flow,
                city=city,
                rows=tuple(provider_rows[member][0] for member in from_providers),
                providers=frozenset(provider_rows[member][1].provider for member in from_providers),
                golden_id=known[0] if known else None,
                representative=max(candidates, key=_completeness) if candidates else None,
            )
        )
    return entities
