"""Data shapes of the bake-off: inputs supplied by the owner, what a run records and what admins label."""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from tg_discovery.domain.enums import Flow
from tg_discovery.domain.geo import BoundingBox
from tg_discovery.domain.models import EventDraft, EventLinks, EventOccurrence, EventVenue, VenueDraft
from tg_discovery.llm.bedrock_responses import LlmUsage
from tg_discovery.pipelines.common.extractor import ExtractionStats
from tg_discovery.providers.base import ProviderCall

LABELS = ("valid", "not_lgbtq", "wrong_place", "past_or_cancelled", "duplicate", "spam")
Label = Literal["valid", "not_lgbtq", "wrong_place", "past_or_cancelled", "duplicate", "spam"]


class CitySpec(BaseModel):
    """A city under test, as supplied in ``cities.csv``."""

    model_config = ConfigDict(frozen=True)

    slug: str
    name: str
    country: str  # ISO 3166-1 alpha-2, upper case
    country_name: str
    timezone: str  # IANA name
    languages: tuple[str, ...] = ("en",)
    aliases: tuple[str, ...] = ()
    tg_city_id: int | None = None
    center: tuple[float, float] | None = None  # (lat, lng)
    bbox: BoundingBox | None = None

    @property
    def names(self) -> tuple[str, ...]:
        return (self.name, *self.aliases)


class GoldenVenue(BaseModel):
    """A venue TravelGay already lists: what a provider should rediscover."""

    id: str
    city: str  # city slug
    name: str
    address: str | None = None
    lat: float | None = None
    lng: float | None = None
    website: str | None = None
    venue_type: str | None = None

    def to_draft(self) -> VenueDraft:
        return VenueDraft(
            name=self.name,
            address=self.address,
            lat=self.lat,
            lng=self.lng,
            website=self.website,
            venue_type=self.venue_type,
        )


class GoldenEvent(BaseModel):
    """An upcoming event TravelGay already lists."""

    id: str
    city: str  # city slug
    name: str
    venue_name: str | None = None
    website: str | None = None
    booking_url: str | None = None
    occurrences: list[EventOccurrence] = Field(default_factory=list[EventOccurrence])

    def to_draft(self) -> EventDraft:
        return EventDraft(
            name=self.name,
            occurrences=self.occurrences,
            venue=EventVenue(name=self.venue_name),
            links=EventLinks(official=self.website, tickets=self.booking_url),
        )


class CanaryEvent(BaseModel):
    """An event announced in the last 72 hours; the freshness probe checks how fast each provider sees it."""

    id: str
    city: str  # city slug
    name: str
    url: str
    announced_at: datetime


class ProviderInfo(BaseModel):
    """A provider as it took part in (or was skipped from) a run, with the price it was run at."""

    name: str
    kind: Literal["search", "venue"]
    flows: list[Flow]
    google_derived: bool = False
    price_unit: str
    usd_per_unit: float
    price_verified_on: date | None = None
    price_source: str = ""
    price_notes: str = ""
    terms_note: str | None = None  # what the vendor's terms say about storing results or benchmarking, if checked
    skipped_reason: str | None = None


class RunMeta(BaseModel):
    run_id: str
    created_at: datetime
    today: date
    dry_run: bool
    budget_usd: float
    cities: list[CitySpec]
    flows: list[Flow]
    providers: list[ProviderInfo]
    event_freshness_days: int
    extractor_model: str | None = None
    stopped_by_budget: bool = False


class CallLog(BaseModel):
    """One provider call: what was asked, what came back and what it cost."""

    provider: str
    flow: Flow
    city: str
    query_id: str
    language: str
    requested: int | None
    returned: int
    hit_urls: list[str] = Field(default_factory=list[str])
    hit_dates: list[datetime | None] = Field(default_factory=list[datetime | None])
    outcome: Literal["ok", "error", "timeout"]
    call: ProviderCall

    @property
    def is_canary(self) -> bool:
        """Canary probes run a different, smaller query set; they are kept out of the run's own metrics."""
        return self.query_id.startswith("canary")


class LlmLog(BaseModel):
    """One extraction call, attributed to the search provider whose hits were extracted."""

    provider: str
    flow: Flow
    city: str
    query_id: str
    model: str
    usage: LlmUsage | None = None
    call: ProviderCall | None = None
    stats: ExtractionStats | None = None
    error: str | None = None


class DraftRow(BaseModel):
    """A derived draft and where it came from. Exactly one of ``venue`` and ``event`` is set."""

    provider: str
    flow: Flow
    city: str
    query_id: str
    language: str
    venue: VenueDraft | None = None
    event: EventDraft | None = None


class CanaryObservation(BaseModel):
    checkpoint_hours: int
    provider: str
    canary_id: str
    seen: bool
    matched_url: str | None = None
    at: datetime


class UrlCheck(BaseModel):
    url: str
    status: Literal["alive", "dead", "blocked", "unsafe"]
    http_status: int | None = None
    reason: str | None = None
    checked_at: datetime


class LabelRecord(BaseModel):
    """An admin's verdict on one novel entity.

    The verdict is tied to the identity keys of the entity's drafts rather than to its numbered id, because ids shift
    when a resumed run adds drafts; any entity that shares a key with the labelled one inherits the verdict.
    """

    item_id: str
    entity_id: str
    keys: list[str]
    label: Label
    labeler: str | None = None
