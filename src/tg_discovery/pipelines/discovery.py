"""What a discovery run is: the request, the cities it covers, and how what the providers found becomes candidates.

The network side (asking providers, extracting) hides behind ``Collector``; everything here is deterministic: group the
drafts of one city by identity, merge each group, score it, drop the junk. ``execute_run`` ties it to the database.
"""

import hashlib
import json
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator

from tg_discovery.domain.enums import Flow, VenueType
from tg_discovery.domain.fingerprint import event_fingerprint, venue_fingerprint
from tg_discovery.domain.geo import BoundingBox
from tg_discovery.domain.models import EventDraft, VenueDraft
from tg_discovery.pipelines.common.budget import Budget
from tg_discovery.pipelines.common.merge import (
    Entry,
    EventEntry,
    event_content_hash,
    group_events,
    group_venues,
    merge_event,
    merge_venue,
    venue_content_hash,
)
from tg_discovery.pipelines.common.scoring import Scored, score_event, score_venue

MAX_CITIES = 10
DEFAULT_WINDOW_DAYS = 60


class AreaError(ValueError):
    """A city cannot be placed on the map, so there is nowhere to search."""


class CityRequest(BaseModel):
    """A city to search. TravelGay knows its time zone and id; coordinates spare a lookup."""

    name: str = Field(min_length=1, max_length=120)
    timezone: str = Field(description="IANA time zone of the city, for example America/New_York.")
    tg_city_id: int | None = Field(default=None, description="The city's id in TravelGay, echoed back on candidates.")
    lat: float | None = Field(default=None, ge=-90, le=90, description="City centre; looked up when left out.")
    lng: float | None = Field(default=None, ge=-180, le=180)

    @field_validator("timezone")
    @classmethod
    def _known_zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as error:
            msg = f"unknown time zone {value!r}; use an IANA name such as America/New_York"
            raise ValueError(msg) from error
        return value


class RunFilter(BaseModel):
    """Where to discover: a country and some of its cities."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "country": "US",
                    "country_name": "United States",
                    "cities": [{"name": "New York", "timezone": "America/New_York"}],
                    "window_days": 60,
                }
            ]
        }
    )

    country: str = Field(pattern=r"^[A-Za-z]{2}$", description="ISO 3166-1 alpha-2 country code.")
    country_name: str | None = Field(
        default=None, description="English name, used in the searches; defaults to the code."
    )
    cities: list[CityRequest] = Field(min_length=1, max_length=MAX_CITIES)
    window_days: int = Field(
        default=DEFAULT_WINDOW_DAYS, ge=1, le=365, description="Events only: how far ahead to look."
    )
    venue_types: list[VenueType] | None = Field(
        default=None,
        description=(
            "Venues only: the types to search for, three searches each; left out or empty means all ten. A venue of "
            "another type that comes back is kept, and venues are not limited to LGBTQ+ places."
        ),
    )

    def chosen_venue_types(self) -> list[VenueType]:
        """The venue types to search for, in a fixed order; every type when none was chosen."""
        wanted = set(self.venue_types or VenueType)
        return [venue_type for venue_type in VenueType if venue_type in wanted]

    def digest(self, flow: Flow) -> str:
        """Identifies the request, so that the same one is not run twice at once (single flight)."""
        key = {
            "flow": flow.value,
            "country": self.country.upper(),
            "cities": sorted(city.name.casefold() for city in self.cities),
            "window_days": self.window_days if flow is Flow.EVENT else None,
        }
        if flow is Flow.VENUE:  # added only for venues, so earlier requests keep their identity
            key["venue_types"] = [venue_type.value for venue_type in self.chosen_venue_types()]
        return hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class CityTarget:
    """A city with the area to search."""

    name: str
    country: str
    country_name: str
    timezone: str
    area: BoundingBox
    tg_city_id: int | None = None

    @property
    def scope(self) -> str:
        """Pairs every identity key with the country and city, so same-name places in other cities never merge."""
        return f"{self.country}|{self.name.casefold()}"


@dataclass(slots=True)
class Timing:
    """The calls of one kind in a city: how many, how long they took together and the slowest one."""

    calls: int = 0
    total_s: float = 0.0  # the calls overlap, so this can be more than the time the city took
    slowest_s: float = 0.0

    def add(self, seconds: float) -> None:
        self.calls += 1
        self.total_s += seconds
        self.slowest_s = max(self.slowest_s, seconds)


@dataclass(slots=True)
class CollectStats:
    calls: int = 0
    failed_calls: int = 0
    stopped_by_budget: bool = False
    errors: Counter[str] = field(default_factory=Counter[str])
    drafts_by_provider: Counter[str] = field(default_factory=Counter[str])
    timings: dict[str, Timing] = field(default_factory=dict[str, Timing])

    def note(self, kind: str, seconds: float) -> None:
        """Record one call of ``kind`` (``search:exa``, ``extract``, ``pictures``...) and how long it took."""
        self.timings.setdefault(kind, Timing()).add(seconds)

    def busy(self) -> dict[str, dict[str, float]]:
        """Calls, total and slowest seconds per kind, the kind that took longest first."""
        ranked = sorted(self.timings.items(), key=lambda item: -item[1].total_s)
        return {
            kind: {"calls": timing.calls, "total_s": round(timing.total_s, 1), "slowest_s": round(timing.slowest_s, 1)}
            for kind, timing in ranked
        }


@dataclass(slots=True)
class CityBatch:
    """Everything the providers found for one city, one draft per provider and mention."""

    city: CityTarget
    venues: list[Entry] = field(default_factory=list[Entry])
    events: list[EventEntry] = field(default_factory=list[EventEntry])
    stats: CollectStats = field(default_factory=CollectStats)


class Collector(Protocol):
    """Asks the providers about one city. The live one calls vendors; tests answer from a script."""

    async def collect(
        self,
        flow: Flow,
        city: CityTarget,
        *,
        window_days: int,
        today: date,
        venue_types: Sequence[VenueType] = (),
    ) -> CityBatch:
        """``venue_types`` are the types a venue run searches for; empty means all of them."""
        ...


class Geocoder(Protocol):
    async def locate(self, request: CityRequest, country: str) -> BoundingBox | None:
        """The search area of a city, or ``None`` when it cannot be found."""
        ...


@dataclass(slots=True)
class RunEnvironment:
    """What a run needs from the outside: a collector, a geocoder and the budget it spends from."""

    collector: Collector
    geocoder: Geocoder
    budget: Budget
    providers: list[str] = field(default_factory=list[str])  # the providers that took part
    skipped: dict[str, str] = field(default_factory=dict[str, str])  # the ones that did not, and why


@dataclass(frozen=True, slots=True)
class Prepared:
    """A merged, scored candidate ready to be stored."""

    flow: Flow
    fingerprint: str
    draft: VenueDraft | EventDraft
    providers: list[str]
    scored: Scored
    content_hash: str


@dataclass(slots=True)
class PreparedCity:
    candidates: list[Prepared] = field(default_factory=list[Prepared])
    rejected: Counter[str] = field(default_factory=Counter[str])  # junk dropped, by reason


def prepare_venues(batch: CityBatch) -> PreparedCity:
    """Merge the venue drafts of a city by identity and keep the ones worth a human's time."""
    result = PreparedCity()
    seen: set[str] = set()
    for group in group_venues(batch.venues, batch.city.scope):
        draft = merge_venue(group)
        providers = sorted({provider for provider, _ in group})
        scored = score_venue(draft, providers, area=batch.city.area)
        if scored.decision == "reject":
            result.rejected[scored.reason or "rejected"] += 1
            continue
        fingerprint = venue_fingerprint(draft, batch.city.scope)
        if fingerprint in seen:
            result.rejected["duplicate"] += 1
            continue
        seen.add(fingerprint)
        result.candidates.append(Prepared(Flow.VENUE, fingerprint, draft, providers, scored, venue_content_hash(draft)))
    return result


def prepare_events(batch: CityBatch, *, today: date, window_days: int) -> PreparedCity:
    """Merge the event drafts of a city by identity and keep the ones worth a human's time."""
    result = PreparedCity()
    seen: set[str] = set()
    for group in group_events(batch.events, batch.city.scope):
        draft = merge_event(group)
        providers = sorted({provider for provider, _ in group})
        scored = score_event(draft, providers, today=today, window_days=window_days, area=batch.city.area)
        if scored.decision == "reject":
            result.rejected[scored.reason or "rejected"] += 1
            continue
        fingerprint = event_fingerprint(draft, batch.city.scope)
        if fingerprint in seen:
            result.rejected["duplicate"] += 1
            continue
        seen.add(fingerprint)
        result.candidates.append(Prepared(Flow.EVENT, fingerprint, draft, providers, scored, event_content_hash(draft)))
    return result
